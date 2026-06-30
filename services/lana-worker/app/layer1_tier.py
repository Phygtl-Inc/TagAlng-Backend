"""Layer 1 relationship-tier handlers."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from app.intro_list import fetch_my_intros
from app.supabase_rpc import call_rpc

_ACCEPT_RE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|accept|let'?s do it|sounds good)\b",
    re.I,
)
_DECLINE_RE = re.compile(
    r"\b(no|not now|not yet|maybe later|decline|pass|skip)\b",
    re.I,
)
# Geographic "my block" must not trigger block-user — only explicit block/report actions.
_BLOCK_RE = re.compile(
    r"\b(?:block|report)\s+(?:them|him|her|this|that|user)\b|"
    r"\bdon'?t contact\b|"
    r"\bblock\s+user\b",
    re.I,
)
_INTRO_PROPOSE_RE = re.compile(
    r"\b(?:int(?:ro)?duce\s+me|connect\s+me|put\s+me)\b",
    re.I,
)
_RESPOND_INTRO_US_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|sure|ok|okay|accept)\s+introduce\s+us\s*\.?\s*$",
    re.I,
)
_STANDALONE_ACCEPT = frozenset(
    {"yes", "yeah", "yep", "sure", "ok", "okay", "accept", "sounds good", "let's do it", "lets do it"}
)


def wants_respond_intro(msg: str) -> bool:
    """Accept/decline a pending received intro — not a new introduce-me request."""
    text = str(msg or "").strip()
    if not text:
        return False
    if _INTRO_PROPOSE_RE.search(text):
        return False
    if _RESPOND_INTRO_US_RE.match(text):
        return True
    action = parse_nudge_response(text)
    if action in ("decline", "block"):
        return True
    if action == "accept":
        norm = text.lower().rstrip(".!")
        if norm in _STANDALONE_ACCEPT:
            return True
        if re.search(r"\bintroduce\s+us\b", text, re.I):
            return True
    return False


def is_standalone_affirmation(msg: str) -> bool:
    """A bare "ok"/"yes" with no explicit intro reference — ambiguous on its own."""
    norm = str(msg or "").strip().lower().rstrip(".!")
    return norm in _STANDALONE_ACCEPT


def parse_nudge_response(msg: str) -> str:
    """Deterministic accept/decline/block read — fallback when AI is unavailable."""
    text = str(msg or "").strip()
    if _INTRO_PROPOSE_RE.search(text):
        return "unknown"
    if _BLOCK_RE.search(text):
        return "block"
    if _DECLINE_RE.search(text):
        return "decline"
    if _ACCEPT_RE.search(text):
        return "accept"
    return "unknown"


def resolve_nudge_action(msg: str, *, nickname: str | None = None) -> str:
    """AI reads the reply first; regex parser is the offline/fallback path.

    A new "introduce me to X" request is never an accept/decline of a pending
    intro, so keep that structural guard ahead of the model.
    """
    text = str(msg or "").strip()
    if _INTRO_PROPOSE_RE.search(text):
        return "unknown"
    from app.intro_response_ai import interpret_nudge_response

    ai = interpret_nudge_response(text, nickname=nickname)
    if ai:
        return ai
    return parse_nudge_response(text)


def handle_respond_nudge(
    msg: str,
    *,
    user_jwt: str,
    session_ctx: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, str]:
    """tier.respond_nudge — accept/decline/block pending received intro."""
    pending = session_ctx.get("pending_intro_respond")
    intro_id: str | None = None
    other_user_id: str | None = None
    nick = "your neighbor"
    received: list[dict[str, Any]] = []

    if isinstance(pending, dict):
        intro_id = str(pending.get("intro_id") or "") or None
        other_user_id = str(pending.get("other_user_id") or "") or None
        nick = str(pending.get("nickname") or nick)

    if not intro_id:
        received = fetch_my_intros(user_jwt, direction="received")
        if not received:
            return (
                "I don't see a pending intro waiting on you right now.",
                None,
                "none",
            )
        row = received[0]
        intro_id = str(row.get("id") or "")
        other_user_id = str(row.get("other_user_id") or "") or None
        nick = str(row.get("nickname") or nick)

    # AI reads accept/decline/block with the neighbor name in context.
    action = resolve_nudge_action(msg, nickname=nick if nick != "your neighbor" else None)

    if action == "unknown":
        return (
            f"{nick} sent an intro through Lana — want to accept, say not now, or block?",
            {"intro_id": intro_id, "other_user_id": other_user_id, "nickname": nick},
            "prompt",
        )

    if action == "accept":
        try:
            call_rpc(user_jwt, "accept_intro", {"p_intro_id": intro_id})
        except HTTPException as exc:
            if "intro_not_found" in str(exc.detail or "").lower():
                return "That intro expired — ask Lana for a fresh match.", None, "expired"
            raise
        return (
            f"Done — you're connected with {nick}. Your chat is open whenever you're ready.",
            None,
            "accept",
        )

    if action == "decline":
        try:
            call_rpc(user_jwt, "decline_intro", {"p_intro_id": intro_id})
        except HTTPException:
            pass
        return ("No problem — I'll let them know you're not ready yet.", None, "decline")

    if action == "block" and other_user_id:
        try:
            call_rpc(
                user_jwt,
                "block_user",
                {
                    "p_blocked_user_id": other_user_id,
                    "p_reason_category": "intro_declined",
                    "p_reason": "blocked via Lana",
                },
            )
        except HTTPException:
            pass
        return ("Understood — you won't hear from them again.", None, "block")

    return (
        "Tell me accept, not now, or block.",
        {"intro_id": intro_id, "other_user_id": other_user_id, "nickname": nick},
        "prompt",
    )


def stamp_respond_prompt_ctx(ctx: dict[str, Any], intros: list[dict[str, Any]]) -> None:
    received = [i for i in intros if str(i.get("direction") or "") == "received"]
    if received:
        row = received[0]
        ctx["pending_intro_respond"] = {
            "intro_id": row.get("intro_id") or row.get("id"),
            "other_user_id": row.get("other_user_id"),
            "nickname": row.get("nickname"),
        }
    ctx["active_intent"] = "tier.respond_nudge"
