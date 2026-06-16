"""Layer 1 relationship-tier handlers."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from app.intro_list import fetch_my_intros
from app.supabase_rpc import call_rpc

_ACCEPT_RE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|accept|introduce|let'?s do it|sounds good)\b",
    re.I,
)
_DECLINE_RE = re.compile(
    r"\b(no|not now|not yet|maybe later|decline|pass|skip)\b",
    re.I,
)
_BLOCK_RE = re.compile(r"\b(block|report|don'?t contact)\b", re.I)


def parse_nudge_response(msg: str) -> str:
    text = str(msg or "").strip()
    if _BLOCK_RE.search(text):
        return "block"
    if _DECLINE_RE.search(text):
        return "decline"
    if _ACCEPT_RE.search(text):
        return "accept"
    return "unknown"


def handle_respond_nudge(
    msg: str,
    *,
    user_jwt: str,
    session_ctx: dict[str, Any],
) -> tuple[str, dict[str, Any] | None, str]:
    """tier.respond_nudge — accept/decline/block pending received intro."""
    action = parse_nudge_response(msg)
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
