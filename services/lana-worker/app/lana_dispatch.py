"""Unified Lana dispatcher — purpose ``lana`` with rule-based routing (no FE mode)."""

from __future__ import annotations

from typing import Any

from app.discovery_route import handle_discovery_turn
from app.discovery_slots import discovery_ai_enabled
from app.guest_login import wants_login as wants_login_intent
from app.reply_compose import compose_reply

LANA_UNIFIED_OPENING = "How can I help you today?"
# Guests (not logged in) get a framing line that explains the two things Lana does;
# signed-in users go straight to the familiar prompt.
LANA_UNIFIED_OPENING_GUEST = (
    "I help you do two things · find something nearby · "
    "or share something with nearby neighbors."
)
# A signed-in mom who hasn't told us her name yet is greeted with the name-ask up front —
# it's needed anyway, and asking first avoids interrupting a topic mid-chat.
LANA_UNIFIED_OPENING_NEEDS_NAME = (
    "Before we dive in — what should neighbors call you? A first name's all I need."
)


def lana_unified_opening(
    is_anonymous: bool = False,
    needs_name: bool = False,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    ui: dict[str, Any] = {
        "bucket": None,
        "focus_phrase": None,
        "highlights": [],
    }
    ctx: dict[str, Any] = {
        "unified_mode": True,
        "active_intent": None,
        "routing_phase": "listening",
        "last_routing": {
            "outcome": "R",
            "intent_class": "companionship",
            "confidence": 1.0,
            "tool_to_call": None,
        },
    }
    # Signed-in + nameless → ask the name first. Arm the up-front-name gate so her reply
    # next turn is captured (app/discovery_route.py::_try_upfront_display_name_turn).
    if not is_anonymous and needs_name:
        ctx["awaiting_upfront_name"] = True
        ctx["upfront_name_attempts"] = 0
        ctx["routing_phase"] = "need_display_name"
        return LANA_UNIFIED_OPENING_NEEDS_NAME, "continue", ctx, ui
    if is_anonymous:
        opening = compose_reply(
            goal=(
                "Open the chat for a signed-out guest: one short warm line saying Lana helps "
                "with two things — finding something nearby, or sharing something with nearby "
                "neighbors."
            ),
            fallback=LANA_UNIFIED_OPENING_GUEST,
            cache=True,
        )
    else:
        opening = LANA_UNIFIED_OPENING
    return opening, "continue", ctx, ui


def lana_unified_turn(
    *,
    history: list[dict[str, Any]],
    user_message: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    is_anonymous: bool,
) -> tuple[str, str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Route unified session turn. Returns reply, status, ctx, ui, peer_matches."""
    discovery = handle_discovery_turn(
        user_message,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        is_anonymous=is_anonymous,
        history=history,
    )
    if discovery is not None:
        reply, ctx, routing, peers = discovery
        ctx["last_routing"] = routing
        ui = {
            "bucket": "interest" if peers else None,
            "focus_phrase": None,
            "highlights": [],
        }
        return reply, "continue", ctx, ui, peers

    msg = str(user_message or "").strip().lower()
    if not discovery_ai_enabled() and wants_login_intent(msg):
        # handle_discovery_turn already tried login; fallback
        reply = "Sure — what's the email on your account?"
        ctx = {
            **session_ctx,
            "unified_mode": True,
            "auth_intent": "login",
            "guest_step": "await_login_phone",
            "routing_phase": "await_login_phone",
        }
        return reply, "continue", ctx, {"bucket": None, "focus_phrase": None, "highlights": []}, []

    reply = compose_reply(
        goal=(
            "Gently orient the user: Lana is here for their neighborhood — she can find "
            "neighbors, log them in (keep the phrase 'log in' verbatim), or learn about them. "
            "End by asking what they'd like to do."
        ),
        fallback=(
            "I'm here for your neighborhood — find neighbors, log in, or tell me about "
            "yourself. What would you like to do?"
        ),
        cache=True,
    )
    ctx = {
        **session_ctx,
        "unified_mode": True,
        "last_routing": {
            "outcome": "R",
            "intent_class": "companionship",
            "confidence": 0.8,
            "tool_to_call": None,
        },
    }
    return reply, "continue", ctx, {"bucket": None, "focus_phrase": None, "highlights": []}, []
