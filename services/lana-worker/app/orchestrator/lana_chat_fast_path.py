"""Skip redundant orchestrator router when discovery slots already said goal=chat."""

from __future__ import annotations

import os
from typing import Any

from app.guest_capabilities import wants_host_activity
from app.orchestrator.slots import has_partial_event_args

_LANA_CHAT_FAST_PHASES = frozenset({"listening", "preview", ""})


def lana_chat_fast_path_enabled() -> bool:
    flag = os.environ.get("LANA_CHAT_FAST_PATH", "1").strip().lower()
    return flag not in ("0", "false", "off")


def discovery_slots_for_message(
    session_ctx: dict[str, Any],
    utterance: str,
) -> dict[str, Any] | None:
    """Cached discovery slots from the same user message (set in handle_discovery_turn)."""
    text = str(utterance or "").strip()
    if not text:
        return None
    if str(session_ctx.get("_discovery_slots_for") or "") != text:
        return None
    slots = session_ctx.get("_discovery_slots")
    return slots if isinstance(slots, dict) else None


def lana_chat_routing_from_discovery(slots: dict[str, Any]) -> dict[str, Any]:
    """Synthetic router outcome: companionship chat, no tool — synth still writes the reply."""
    conf = float(slots.get("confidence", 0.85))
    return {
        "outcome": "R",
        "intent_class": "companionship",
        "confidence": max(0.5, min(1.0, conf)),
        "tool_to_call": None,
        "tool_args": None,
        "missing_slots": [],
        "sentiment": "neutral",
        "needs_confirmation": False,
        "thinking": "discovery_slots goal=chat",
        "enforce_notes": ["discovery_chat_fast_path"],
    }


def should_skip_lana_router(
    *,
    purpose: str,
    utterance: str,
    session_ctx: dict[str, Any],
) -> bool:
    """
    Discovery Flash already routed this turn as non-funnel chat.
    Skip Haiku/router — go straight to synth (still AI, still with memory + context).
    """
    if purpose != "lana" or not lana_chat_fast_path_enabled():
        return False
    if not session_ctx.get("unified_mode"):
        return False
    if wants_host_activity(utterance):
        return False
    if session_ctx.get("pending_confirmation"):
        return False
    if has_partial_event_args(None, session_ctx):
        return False
    prev_draft = session_ctx.get("event_draft")
    if isinstance(prev_draft, dict) and has_partial_event_args(prev_draft, session_ctx):
        return False

    phase = str(session_ctx.get("routing_phase") or "listening")
    if phase not in _LANA_CHAT_FAST_PHASES:
        return False

    slots = discovery_slots_for_message(session_ctx, utterance)
    if not slots:
        return False
    if str(slots.get("goal") or "none").lower() != "chat":
        return False
    return float(slots.get("confidence", 0.0)) >= 0.5
