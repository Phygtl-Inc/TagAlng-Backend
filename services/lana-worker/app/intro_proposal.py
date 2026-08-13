"""Lana neighbor intro: propose_intro + ui_intent for FE."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from app.claim_search import peer_matches_identity_snippet
from app.intro_list import format_duplicate_intro_reply
from app.layer1_tier import wants_respond_intro
from app.reply_compose import compose_reply
from app.supabase_rpc import call_rpc

INTENT_PROPOSE_INTRO = "social.propose_intro"

_AFFIRMATIVE = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "sure",
        "ok",
        "okay",
        "please",
        "do it",
        "introduce",
        "connect",
        "put us together",
    }
)

_INTRO_REQUEST_RE = re.compile(
    r"\b(?:int(?:ro)?duce|introduction|connect me|put (?:us|me) together|meet (?:them|her|him)|"
    r"reach out|say hi|send (?:a )?nudge|talk to)\b",
    re.I,
)


def wants_neighbor_intro(msg: str) -> bool:
    text = str(msg or "").strip()
    if wants_respond_intro(text):
        return False
    return bool(_INTRO_REQUEST_RE.search(text))


def accepts_intro_offer(msg: str) -> bool:
    text = str(msg or "").strip().lower().rstrip(".!")
    return text in _AFFIRMATIVE


# matching_peer_label fallbacks that name proximity, not a shared trait — building
# "You both fit Near you." (or the old "Lana matched you with Near you.") from these
# produced garbled copy. Treat them as no-label.
_GENERIC_PEER_LABELS = frozenset(
    {"near you", "nearby", "close by", "neighbor", "a neighbor", "a neighbor near you"}
)


def _trait_label(peer: dict[str, Any]) -> str | None:
    label = str(peer.get("matching_peer_label") or "").strip()
    if not label or label.lower().strip(" .!") in _GENERIC_PEER_LABELS:
        return None
    return label


def build_match_reason(
    *,
    identity_snippet: str | None,
    peer: dict[str, Any],
) -> str:
    # Lingo rule 4: a person is never "a match" and Lana never "matched you with"
    # someone — say an intro / someone to meet. Persisted to intros.match_reason,
    # which the reply-path guard never scans, so this must be clean at the source.
    label = _trait_label(peer)
    snippet = str(identity_snippet or "").strip()
    # The fallback snippet can be several messages joined with "; " — only echo the
    # first clause so the reason stays a clean sentence, not a merged dump.
    first_clause = snippet.split(";")[0].strip()
    if snippet and not peer_matches_identity_snippet(peer, snippet):
        if label:
            reason = f"You both fit {label.lower()}."
        else:
            reason = "A neighbor close by — Lana thinks you two would click."
    elif first_clause and label:
        # Skip the "you mentioned …" tail when it just restates the label.
        low_clause, low_label = first_clause.lower(), label.lower()
        if low_clause in low_label or low_label in low_clause:
            reason = f"You both fit {low_label}."
        else:
            reason = f"You both fit {low_label} — you mentioned {first_clause[:120]}."
    elif first_clause:
        reason = f"You mentioned {first_clause[:160]} — strong overlap nearby."
    elif label:
        reason = f"You both fit {label.lower()}."
    else:
        reason = "A neighbor close by — Lana thinks you two would click."
    # The label/snippet echo the users' own claim words, which can carry the banned
    # lexicon ("Mom of two", "on my block"). This string is persisted, so scrub here.
    from app.lingo_guard import find_violations, naive_clean

    return naive_clean(reason) if find_violations(reason) else reason


_INTRO_NAME_RE = re.compile(
    r"\b(?:int(?:ro)?duce(?:\s+me)?\s+to|connect\s+me\s+to|meet|talk\s+to)\s+([a-z][a-z'-]{1,30})\b",
    re.I,
)


def requested_peer_name(msg: str) -> str | None:
    """Explicit neighbor name in an intro request (e.g. 'introduce me to Kashaf')."""
    m = _INTRO_NAME_RE.search(str(msg or ""))
    if not m:
        return None
    name = str(m.group(1) or "").strip().lower()
    if name in ("a", "an", "the", "my", "that", "them", "her", "him", "neighbor", "neighbour"):
        return None
    return name


def peer_index_from_message(msg: str) -> int | None:
    lower = str(msg or "").lower()
    hash_match = re.search(r"#(\d+)\b", lower)
    if hash_match:
        return int(hash_match.group(1)) - 1
    if re.search(r"\b(?:first|1st)\b", lower):
        return 0
    if re.search(r"\b(?:second|2nd)\b", lower):
        return 1
    if re.search(r"\b(?:third|3rd)\b", lower):
        return 2
    m = re.search(r"\b(?:neighbor|neighbour|person|match)\s*(\d+)\b", lower)
    if m:
        return int(m.group(1)) - 1
    return None


_peer_index_from_message = peer_index_from_message


def pick_block_log_entry_for_intro(
    entries: list[dict[str, Any]],
    *,
    msg: str,
) -> dict[str, Any] | None:
    """Pick a numbered block-log row when user says introduce me to #N."""
    if not entries:
        return None
    idx = peer_index_from_message(msg)
    if idx is not None:
        if 0 <= idx < len(entries):
            return entries[idx]
        return entries[0]
    lower = str(msg or "").lower()
    if re.search(
        r"\b(?:swap|regarding|about\s+the|(?:block|neighborhood)\s*log|neighbor\s+match|bicycle|bike)\b",
        lower,
    ):
        return entries[0]
    return None


def block_log_peer_from_entry(row: dict[str, Any]) -> dict[str, Any]:
    nick = str(row.get("peer_preview_label") or "A neighbor").strip()
    if nick == "A neighbor on your block":
        nick = "A neighbor"
    return {
        "peer_user_id": row.get("peer_user_id"),
        "nickname": None if nick == "A neighbor" else nick,
        "matching_peer_label": str(row.get("match_summary") or "").strip()
        or str((row.get("match_reasons") or [""])[0] or "").strip()
        or "swap match near you",
    }


def pick_peer_for_intro(
    peers: list[dict[str, Any]],
    *,
    msg: str,
    peer_name: str | None = None,
    pending: dict[str, Any] | None = None,
    list_index: int | None = None,
) -> dict[str, Any] | None:
    requested = str(peer_name or "").strip().lower() or None
    if not requested:
        requested = requested_peer_name(msg)

    identified = [p for p in peers if p.get("peer_user_id")]
    if not identified:
        return None

    lower = str(msg or "").lower()

    if list_index is not None:
        try:
            pick_idx = int(list_index) - 1
            if 0 <= pick_idx < len(identified):
                return identified[pick_idx]
        except (TypeError, ValueError):
            pass

    idx = _peer_index_from_message(msg)
    if idx is not None and 0 <= idx < len(identified):
        return identified[idx]

    if requested:
        for p in identified:
            nick = str(p.get("nickname") or "").lower()
            if nick and (nick == requested or requested in nick or nick in requested):
                return p
        return None

    # Name from shown cards in message beats stale pending_intro_offer (e.g. "send intro to Kashaf").
    for p in identified:
        nick = str(p.get("nickname") or "").lower()
        if nick and len(nick) > 2 and nick in lower:
            return p

    if pending:
        pid = str(pending.get("candidate_user_id") or "").strip()
        if pid:
            for p in identified:
                if str(p.get("peer_user_id") or "") == pid:
                    return p
            return {
                "peer_user_id": pid,
                "nickname": pending.get("candidate_nickname"),
                "matching_peer_label": pending.get("matching_peer_label"),
                "similarity_score": pending.get("match_score"),
                "matching_peer_concept": pending.get("matching_peer_concept"),
            }

    for p in identified:
        label = str(p.get("matching_peer_label") or "").lower()
        if label and len(label) > 3 and label in lower:
            return p
    # Nothing above says WHO — the message named no shown peer, no pending offer,
    # no list index. Auto-picking the first card here sent a real intro on a
    # misclassified turn (a bare "Dana" answering the name ask introduced
    # peers[0]). An intro is outward-facing: only fall back to the top card when
    # the message itself asks for one; otherwise return None so the caller
    # clarifies instead of guessing.
    if wants_neighbor_intro(msg) or accepts_intro_offer(msg):
        return identified[0]
    return None


def propose_neighbor_intro(
    user_jwt: str,
    *,
    candidate_user_id: str,
    match_reason: str,
    shared_dimensions: list[str] | None = None,
    match_score: float | None = None,
    nudge_opener: str | None = None,
    joint_moment_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "p_candidate_id": candidate_user_id,
        "p_match_reason": match_reason[:280],
        "p_shared_dimensions": shared_dimensions or [],
    }
    if match_score is not None:
        payload["p_match_score"] = match_score
    if nudge_opener:
        payload["p_nudge_opener"] = nudge_opener[:280]
    if joint_moment_id:
        payload["p_joint_moment_id"] = joint_moment_id
    raw = call_rpc(user_jwt, "lana_propose_neighbor_intro", payload)
    return raw if isinstance(raw, dict) else {}


def format_intro_proposed_reply(peer: dict[str, Any], match_reason: str) -> str:
    nick = str(peer.get("nickname") or peer.get("matching_peer_label") or "your neighbor").strip()
    reason = str(match_reason or "").strip()
    if reason:
        fallback = (
            f"Done — I introduced you to {nick}. {reason} "
            f"They'll get the intro and can accept when ready."
        )
    else:
        fallback = (
            f"Done — I introduced you to {nick}. They'll see why you might click when they're ready."
        )
    return compose_reply(
        goal=(
            "Confirm you just sent the intro to this neighbor, mention why they "
            "fit if a reason is given, and note they'll get the intro and can "
            "accept when they're ready."
        ),
        facts=[f"Neighbor the intro went to: {nick}"]
        + ([f"Why they fit: {reason}"] if reason else []),
        fallback=fallback,
    )


def format_intro_offer_reply(peer: dict[str, Any], match_reason: str) -> str:
    label = str(peer.get("matching_peer_label") or peer.get("nickname") or "a neighbor").strip()
    return compose_reply(
        goal=(
            "Tell the user this neighbor looks like a strong fit, give the reason, "
            "and ask if they want you to introduce the two of them."
        ),
        facts=[
            f"The neighbor: {label}",
            f"Why they fit: {match_reason}",
        ],
        fallback=(
            f"{label} looks like a strong fit — {match_reason} "
            f"Want me to introduce you two?"
        ),
    )


def format_intro_offer_turn(peer: dict[str, Any], match_reason: str) -> str:
    """C-8 single featured match — replaces the multi-neighbor preview list in copy."""
    nick = str(peer.get("nickname") or "").strip()
    label = str(peer.get("matching_peer_label") or "a neighbor near you").strip()
    who = nick or label
    reason = str(match_reason or "").strip().rstrip(".")
    lines = [f"I think I found a fit — {who}."]
    # `reason` already names the shared trait (build_match_reason embeds the label),
    # so don't echo the label a second time as its own sentence.
    if reason:
        lines.append(f"{reason}.")
    lines.append("Want me to introduce you two?")
    return compose_reply(
        goal=(
            "Tell the user you think you found one neighbor who fits them, share "
            "the reason exactly once (don't repeat the shared trait twice), and "
            "ask if they want you to introduce the two of them."
        ),
        facts=[f"The neighbor: {who}"]
        + ([f"Why they fit: {reason}"] if reason else []),
        fallback=" ".join(lines),
        max_sentences=3,
    )


def stamp_intro_proposal_ctx(
    ctx: dict[str, Any],
    *,
    intro: dict[str, Any],
    peer: dict[str, Any],
) -> None:
    ctx["intro_proposal"] = {
        "intro_id": intro.get("intro_id"),
        "nudge_id": intro.get("nudge_id"),
        "candidate_user_id": intro.get("candidate_user_id") or peer.get("peer_user_id"),
        "candidate_nickname": peer.get("nickname"),
        "matching_peer_label": peer.get("matching_peer_label"),
        "match_reason": intro.get("match_reason"),
        "shared_dimensions": intro.get("shared_dimensions") or [],
        "status": intro.get("status") or "proposed",
    }
    # The intro CARD belongs to this turn only. `intro_proposal` itself must persist —
    # two "don't re-propose" guards in discovery_route read it on later turns — so the
    # one-turn signal is separate, matching event_published_now / item_listed_now.
    ctx["intro_proposed_now"] = True
    ctx["active_intent"] = INTENT_PROPOSE_INTRO
    from app.intro_list import clear_intro_offer_ctx

    clear_intro_offer_ctx(ctx)


def stamp_intro_offer_ctx(
    ctx: dict[str, Any],
    *,
    peer: dict[str, Any],
    match_reason: str,
) -> None:
    ctx["pending_intro_offer"] = {
        "candidate_user_id": peer.get("peer_user_id"),
        "candidate_nickname": peer.get("nickname"),
        "matching_peer_label": peer.get("matching_peer_label"),
        "match_reason": match_reason[:280],
        "match_score": peer.get("similarity_score"),
        "matching_peer_concept": peer.get("matching_peer_concept"),
    }
    ctx["active_intent"] = INTENT_PROPOSE_INTRO


def try_propose_intro_from_preview(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    peers: list[dict[str, Any]],
    identity_snippet: str | None,
    force: bool = False,
    peer_name: str | None = None,
    list_index: int | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return (reply, intro_payload) or None if cannot propose."""
    pending = session_ctx.get("pending_intro_offer")
    if (
        not force
        and not wants_neighbor_intro(msg)
        and not (pending and accepts_intro_offer(msg))
    ):
        return None

    peer = pick_peer_for_intro(
        peers,
        msg=msg,
        peer_name=peer_name,
        pending=pending if isinstance(pending, dict) else None,
        list_index=list_index,
    )
    if not peer or not peer.get("peer_user_id"):
        return None

    reason = str(
        (pending or {}).get("match_reason")
        or build_match_reason(identity_snippet=identity_snippet, peer=peer)
    )[:280]
    dims: list[str] = []
    concept = peer.get("matching_peer_concept")
    if concept:
        dims.append(str(concept))

    try:
        intro = propose_neighbor_intro(
            user_jwt,
            candidate_user_id=str(peer["peer_user_id"]),
            match_reason=reason,
            shared_dimensions=dims,
            match_score=float(peer["similarity_score"]) if peer.get("similarity_score") is not None else None,
        )
    except HTTPException as exc:
        detail = str(exc.detail or "")
        if detail == "duplicate_intro_recent":
            return (
                format_duplicate_intro_reply(peer=peer, user_jwt=user_jwt),
                {"status": "duplicate", "candidate_user_id": peer.get("peer_user_id")},
            )
        if detail == "phone_not_verified":
            return (
                compose_reply(
                    goal=(
                        "Tell the user they need to verify their email first — then "
                        "you can introduce them to neighbors."
                    ),
                    fallback="Verify your email first — then I can introduce you to neighbors.",
                    cache=True,
                ),
                {"status": "need_verify"},
            )
        if "nudge_cooldown_pair" in detail:
            # lana_propose_neighbor_intro sends the nudge itself when the pair are still
            # strangers, so the 7-day per-pair cooldown surfaces here too. Say the real
            # reason — a bare failure reads as "try again", which cannot work for a week.
            _nick = str(peer.get("nickname") or "them").strip() or "them"
            return (
                compose_reply(
                    goal=(
                        "You already nudged this neighbor within the last week, so you "
                        "can't send another yet. Say so plainly, tell them the ball is "
                        "in the neighbor's court, and make clear retrying won't help "
                        "until the week is up."
                    ),
                    facts=[f"The neighbor's name: {_nick}", "Nudges to the same person are once per 7 days"],
                    fallback=(
                        f"You already nudged {_nick} in the last week — it's with them now. "
                        "I can't send another until the week's up."
                    ),
                ),
                {"status": "nudge_cooldown", "candidate_user_id": peer.get("peer_user_id")},
            )
        raise

    if not intro.get("intro_id"):
        if str(intro.get("status") or "") == "duplicate":
            return (
                format_duplicate_intro_reply(peer=peer, user_jwt=user_jwt),
                {"status": "duplicate", "candidate_user_id": peer.get("peer_user_id")},
            )
        return None

    reply = format_intro_proposed_reply(peer, reason)
    return reply, intro
