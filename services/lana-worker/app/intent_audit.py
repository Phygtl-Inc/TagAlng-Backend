"""One `intent_decision` row per Lana turn — what the classifier REALLY decided.

The row answers four questions the existing audit rows cannot:
  1. What did the model pick, and how sure was it — before any reconciler bumped it?
  2. Did a keyword rule change that pick — which rule, from what, to what?
  3. Did the classifier run at all (a chip/CTA forces the slots; a sticky flow may skip it)?
  4. Where did the turn end up (`last_routing`)?

It exists so the 0.85 confidence bump can be removed against a measured baseline
instead of a guess. Observation only: nothing here changes a routing decision, the
write runs on a daemon thread, and every failure is swallowed.

Rollout knob: LANA_INTENT_AUDIT=on (default off).
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Per-turn override list. A contextvar, not a module global: concurrent turns on the
# worker's threadpool must not see each other's overrides.
_TURN_OVERRIDES: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "lana_intent_overrides", default=None
)


def intent_audit_enabled() -> bool:
    flag = os.environ.get("LANA_INTENT_AUDIT", "off").strip().lower()
    return flag in ("1", "true", "on")


def begin_turn() -> contextvars.Token:
    return _TURN_OVERRIDES.set([])


def end_turn(token: contextvars.Token) -> list[dict[str, Any]]:
    overrides = list(_TURN_OVERRIDES.get() or [])
    _TURN_OVERRIDES.reset(token)
    return overrides


def record_override(
    rule: str,
    *,
    from_label: Any,
    from_conf: Any,
    to_label: Any,
    to_conf: Any,
) -> None:
    """A keyword rule replaced or bumped the model's pick. No-op outside a turn, and when
    nothing actually changed. Deduped: enrich_slots runs several times per turn on the
    same message and would otherwise report the same rewrite each time."""
    overrides = _TURN_OVERRIDES.get()
    if overrides is None:
        return
    try:
        entry = {
            "rule": rule,
            "from": str(from_label or "") or None,
            "from_conf": round(float(from_conf or 0.0), 3),
            "to": str(to_label or "") or None,
            "to_conf": round(float(to_conf or 0.0), 3),
        }
    except (TypeError, ValueError):
        return
    if entry["from"] == entry["to"] and entry["from_conf"] == entry["to_conf"]:
        return
    if entry not in overrides:
        overrides.append(entry)


def _classifier_read(session_ctx: dict[str, Any], user_message: str) -> dict[str, Any]:
    """The cached classifier slots — only when they belong to THIS message. The cache
    outlives the turn, so a stale read from the previous message must not be logged."""
    slots = session_ctx.get("_discovery_slots")
    slots_for = str(session_ctx.get("_discovery_slots_for") or "")
    if not isinstance(slots, dict) or slots_for != str(user_message or "").strip():
        return {"ran": False}
    if slots.get("_classifier_error"):
        # The call FAILED (bad key, outage, timeout): not an unsure model, and not to be
        # counted in the confidence spread.
        return {"ran": False, "error": slots["_classifier_error"]}
    forced = slots.get("_forced_kind")
    out: dict[str, Any] = {
        # A chip/CTA tap stamps forced slots: the label is ours, not the model's.
        "ran": not forced,
        "linear_intent": slots.get("linear_intent") or None,
        "goal": slots.get("goal") or None,
        "clarify": bool(str(slots.get("clarify") or "").strip()),
    }
    if forced:
        out["forced_kind"] = forced
    try:
        out["confidence"] = round(float(slots.get("confidence")), 3)
    except (TypeError, ValueError):
        out["confidence"] = None
    return out


def build_record(
    *,
    session_ctx: dict[str, Any],
    user_message: str,
    routing_phase_in: str,
    overrides: list[dict[str, Any]],
    elapsed_ms: int | None,
    error: str | None = None,
) -> dict[str, Any]:
    last = session_ctx.get("last_routing")
    last = last if isinstance(last, dict) else {}
    record: dict[str, Any] = {
        "classifier": _classifier_read(session_ctx, user_message),
        "overrides": overrides,
        "routing_phase_in": routing_phase_in or None,
        "routing_phase_out": str(session_ctx.get("routing_phase") or "") or None,
        "final": {
            "outcome": last.get("outcome"),
            "intent_class": last.get("intent_class"),
            "tool_called": last.get("tool_called"),
        },
        "elapsed_ms": elapsed_ms,
    }
    if error:
        record["error"] = error
    return record


def log_intent_decision(
    *,
    session_id: str,
    user_id: str,
    user_message: str,
    record: dict[str, Any],
) -> None:
    """Write the row behind the turn. Never raises, never blocks the reply."""

    def _write() -> None:
        try:
            from app.orchestrator.audit import log_turn

            log_turn(
                session_id=session_id,
                user_id=user_id,
                event_type="intent_decision",
                module="intent",
                utterance=user_message,
                response=None,
                routing=record,
            )
        except Exception:  # noqa: BLE001
            logger.exception("intent_audit_write_failed")

    try:
        threading.Thread(target=_write, daemon=True, name="intent-audit").start()
    except Exception:  # noqa: BLE001
        logger.exception("intent_audit_spawn_failed")
