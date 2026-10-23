"""One honest routing record per Lana chat turn.

Every chat turn passes through main.py's single call to run_lana_unified_pipeline, so the
record is written there — including the turns that return early from a capture and never
reach the orchestrator's own log_turn. The model's verdict is read from the fields
discovery_slots freezes before any reconciler runs (ai_linear_intent / ai_confidence), so
the record shows what the AI said next to what Lana did and who made the call.

decided_by:
  button        — the client sent an intent_hint (a tapped chip / CTA)
  no_classifier — no classifier read for this message (a sticky flow answered first)
  keyword       — a keyword match picked the lane because the model did not
  ai_signal     — the model's own signal field overrode its own label
  ai            — the model's label stood
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


def build_routing_record(
    ctx: dict[str, Any],
    *,
    user_message: str,
    intent_hint: str | None,
    prev_routing: Any,
    pending_kind: str | None = None,
) -> dict[str, Any]:
    text = str(user_message or "").strip()
    slots = ctx.get("_discovery_slots")
    classified = (
        isinstance(slots, dict)
        and bool(text)
        and str(ctx.get("_discovery_slots_for") or "") == text
    )
    slots = slots if classified else {}

    hint = str(intent_hint or "").strip() or None
    if hint:
        decided_by = "button"
    elif not classified:
        decided_by = "no_classifier"
    else:
        decided_by = str(slots.get("decided_by") or "ai")

    # last_routing is only replaced by paths that set it; an untouched dict is last turn's.
    routing = ctx.get("last_routing")
    fresh = isinstance(routing, dict) and routing is not prev_routing
    routing = routing if fresh else {}

    def _conf(v: Any) -> float | None:
        try:
            return None if v is None else round(float(v), 3)
        except (TypeError, ValueError):
            return None

    return {
        "decided_by": decided_by,
        "intent_hint": hint,
        "ai_label": slots.get("ai_linear_intent", slots.get("linear_intent")) if classified else None,
        "ai_confidence": _conf(slots.get("ai_confidence", slots.get("confidence"))) if classified else None,
        "ai_alternative": slots.get("alternative_intent") if classified else None,
        "ai_clarify": (slots.get("clarify") or None) if classified else None,
        "final_label": slots.get("linear_intent") if classified else None,
        "final_goal": slots.get("goal") if classified else None,
        "signal_intent": slots.get("signal_intent") if classified else None,
        "flow": routing.get("outcome"),
        "intent_class": routing.get("intent_class"),
        "tool": routing.get("tool_to_call") or routing.get("tool_called"),
        # What Lana was waiting on when the message arrived (app/pending_ask.py) and the
        # classifier's read of how the message relates to it.
        "pending_kind": pending_kind,
        "reply_to_pending": (slots.get("reply_to_pending") or None) if classified else None,
        "routing_phase": ctx.get("routing_phase"),
        "active_intent": ctx.get("active_intent"),
    }


def log_routing_turn(
    *,
    session_id: str,
    user_id: str,
    ctx: dict[str, Any],
    user_message: str,
    reply: str | None,
    intent_hint: str | None,
    prev_routing: Any,
    pending_kind: str | None = None,
) -> None:
    """Build now (the ctx keeps changing after the turn), write on a daemon thread."""
    try:
        record = build_routing_record(
            ctx, user_message=user_message, intent_hint=intent_hint,
            prev_routing=prev_routing, pending_kind=pending_kind,
        )
    except Exception:  # noqa: BLE001 — logging must never break a turn
        logger.exception("routing_log_build_failed")
        return

    def _write() -> None:
        from app.orchestrator.audit import log_turn

        log_turn(
            session_id=session_id,
            user_id=user_id,
            event_type="route",
            module=str(record.get("final_label") or record.get("flow") or "")[:64] or None,
            utterance=user_message,
            response=reply,
            routing=record,
        )

    threading.Thread(target=_write, daemon=True).start()
