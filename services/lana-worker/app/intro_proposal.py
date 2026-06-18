"""Lana neighbor intro: propose_intro + ui_intent for FE."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from app.claim_search import peer_matches_identity_snippet
from app.intro_list import format_duplicate_intro_reply
from app.layer1_tier import wants_respond_intro
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


def build_match_reason(
    *,
    identity_snippet: str | None,
    peer: dict[str, Any],
) -> str:
    label = str(peer.get("matching_peer_label") or "a neighbor on your block").strip()
    snippet = str(identity_snippet or "").strip()
    if snippet and not peer_matches_identity_snippet(peer, snippet):
        return f"Lana matched you with {label} on your block."
    if snippet and label:
        return f"You both fit {label.lower()} — you mentioned {snippet[:120]}."
    if snippet:
        return f"You mentioned {snippet[:160]} — strong overlap on your block."
    return f"Lana matched you with {label} on your block."


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
        r"\b(?:swap|regarding|about\s+the|block\s*log|neighbor\s+match|bicycle|bike)\b",
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
        or "swap match on your block",
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
    if idx is not None and identified:
        return identified[0]
    return identified[0]


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
        return (
            f"Done — I introduced you to {nick}. {reason} "
            f"They'll get the intro and can accept when ready."
        )
    return f"Done — I introduced you to {nick}. They'll see why you might click when they're ready."


def format_intro_offer_reply(peer: dict[str, Any], match_reason: str) -> str:
    label = str(peer.get("matching_peer_label") or peer.get("nickname") or "a neighbor").strip()
    return (
        f"{label} looks like a strong match — {match_reason} "
        f"Want me to introduce you two?"
    )


def format_intro_offer_turn(peer: dict[str, Any], match_reason: str) -> str:
    """C-8 single featured match — replaces the multi-neighbor preview list in copy."""
    nick = str(peer.get("nickname") or "").strip()
    label = str(peer.get("matching_peer_label") or "a neighbor on your block").strip()
    who = nick or label
    reason = str(match_reason or "").strip().rstrip(".")
    lines = [f"I think I found a fit — {who}."]
    if nick and label and label.lower() != nick.lower():
        lines.append(label + ".")
    if reason:
        lines.append(f"{reason}.")
    lines.append("Want me to introduce you two?")
    return " ".join(lines)


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
    ctx["active_intent"] = INTENT_PROPOSE_INTRO
    ctx.pop("pending_intro_offer", None)


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
                "Verify your phone first — then I can introduce you to neighbors.",
                {"status": "need_verify"},
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
