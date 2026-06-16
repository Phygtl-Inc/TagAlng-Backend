"""Lana neighbor intro: propose_intro + ui_intent for FE."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

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
    r"\b(introduce|introduction|connect me|put (?:us|me) together|meet (?:them|her|him)|"
    r"reach out|say hi|send (?:a )?nudge|talk to)\b",
    re.I,
)


def wants_neighbor_intro(msg: str) -> bool:
    return bool(_INTRO_REQUEST_RE.search(str(msg or "").strip()))


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
    if snippet and label:
        return f"You both fit {label.lower()} — you mentioned {snippet[:120]}."
    if snippet:
        return f"You mentioned {snippet[:160]} — strong overlap on your block."
    return f"Lana matched you with {label} on your block."


def _peer_index_from_message(msg: str) -> int | None:
    lower = str(msg or "").lower()
    if re.search(r"\b(?:first|1st|#1)\b", lower):
        return 0
    if re.search(r"\b(?:second|2nd|#2)\b", lower):
        return 1
    if re.search(r"\b(?:third|3rd|#3)\b", lower):
        return 2
    m = re.search(r"\b(?:neighbor|neighbour|person|match|#)\s*(\d+)\b", lower)
    if m:
        return int(m.group(1)) - 1
    return None


def pick_peer_for_intro(
    peers: list[dict[str, Any]],
    *,
    msg: str,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if pending:
        pid = str(pending.get("candidate_user_id") or "").strip()
        if pid:
            for p in peers:
                if str(p.get("peer_user_id") or "") == pid:
                    return p
            return pending

    identified = [p for p in peers if p.get("peer_user_id")]
    if not identified:
        return None

    idx = _peer_index_from_message(msg)
    if idx is not None and 0 <= idx < len(identified):
        return identified[idx]

    lower = str(msg or "").lower()
    for p in identified:
        label = str(p.get("matching_peer_label") or "").lower()
        nick = str(p.get("nickname") or "").lower()
        if label and label in lower:
            return p
        if nick and nick in lower:
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
) -> tuple[str, dict[str, Any]] | None:
    """Return (reply, intro_payload) or None if cannot propose."""
    pending = session_ctx.get("pending_intro_offer")
    if not force and not wants_neighbor_intro(msg) and not (pending and accepts_intro_offer(msg)):
        return None

    peer = pick_peer_for_intro(peers, msg=msg, pending=pending if isinstance(pending, dict) else None)
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
            nick = str(peer.get("nickname") or peer.get("matching_peer_label") or "them")
            return (
                f"You already have a recent intro out to {nick} — give them a little time to respond.",
                {"status": "duplicate", "candidate_user_id": peer.get("peer_user_id")},
            )
        if detail == "phone_not_verified":
            return (
                "Verify your phone first — then I can introduce you to neighbors.",
                {"status": "need_verify"},
            )
        raise

    reply = format_intro_proposed_reply(peer, reason)
    return reply, intro
