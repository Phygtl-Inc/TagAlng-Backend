"""Turn-level telemetry — full outcome names + the north-star "secured step".

The orchestrator's internal routing protocol uses single-letter outcome codes
(R / A / T / C — see prompts/orchestrator_router.md). Those letters are fine as an
LLM wire format but must never reach analytics or the API payload: nothing
downstream can tell "T" (tool call) from a dead-end without this file's mapping.
Everything here is pure (dict in → value out) so the same definitions serve the
per-turn analytics event, the response payload, and unit tests.
"""

from __future__ import annotations

from typing import Any

# Router letter codes → full outcome names (prompts/orchestrator_router.md §Four outcomes).
OUTCOME_NAMES: dict[str, str] = {
    "R": "reply",       # pure conversation, no tool
    "A": "ask",         # in-scope but a required slot is missing — ask one slot
    "T": "tool_call",   # confident + slots filled — exactly one tool ran
    "C": "capture",     # out-of-scope ask — capture_inquiry fired
}


def full_outcome(value: Any) -> str | None:
    """Full outcome name for a routing outcome.

    Letters map via OUTCOME_NAMES; already-full values ("activity_browse",
    "pass_along", …) pass through unchanged so existing dashboards keep working.
    A single letter NEVER leaks out — an unknown one becomes "unknown".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 1:
        return OUTCOME_NAMES.get(text.upper(), "unknown")
    return text


# ── North-star: did an ask end in a secured next step? ──────────────────────────

# ui_intents that mean a signal row was persisted this turn (listen-alert saved,
# item passed along, tip shared). All four write local_signals server-side.
_SECURED_SIGNAL_UI_INTENTS = frozenset(
    {"signal_saved", "look_meet_saved", "item_listed", "tip_listed"}
)
# Per-turn ctx flags stamped by the flows on the save turn (turn-scoped surfaces —
# they cannot leak in from a prior turn, see app/turn_surfaces.py).
_SECURED_SIGNAL_CTX_FLAGS = ("look_meet_saved_now", "item_listed_now", "tip_listed_now")

# Tools whose successful run means an intro/nudge actually went out.
_INTRO_TOOLS = frozenset({"lana_propose_neighbor_intro", "send_nudge"})
# intro_proposal statuses that mean the intro did NOT go out this turn.
_INTRO_NOT_SENT = frozenset({"duplicate", "need_verify", "intro_skipped"})


def north_star_secured(turn: dict[str, Any]) -> str | None:
    """Return the secured next step this turn produced, or None for a dead end.

    One of: "rsvp" | "published" | "intro_sent" | "signal_saved" | "waitlist" | None.

    `turn` is the merged session context of the turn plus the derived "ui_intent"
    key — i.e. exactly what `_run_lana_message` has in hand after a turn. Pure:
    no I/O, no clock, safe to call from tests with a literal dict.
    """
    ui = str(turn.get("ui_intent") or "")

    # A meet went live this turn (event_published_now is turn-scoped).
    if turn.get("event_published_now") or ui == "event_created":
        return "published"

    # An RSVP / event join confirmed server-side this turn.
    if turn.get("event_joined_now") or turn.get("rsvp_confirmed_now"):
        return "rsvp"

    # An intro/nudge actually went out (not a duplicate or a verify-gated attempt).
    routing = turn.get("last_routing")
    routing = routing if isinstance(routing, dict) else {}
    tool = str(routing.get("tool_to_call") or routing.get("tool_called") or "")
    intro = turn.get("intro_proposal")
    intro = intro if isinstance(intro, dict) else {}
    if tool in _INTRO_TOOLS and str(intro.get("status") or "proposed") not in _INTRO_NOT_SENT:
        return "intro_sent"

    # A listen-alert / signal row persisted (meet seek, pass-along, tip, capture).
    if ui in _SECURED_SIGNAL_UI_INTENTS:
        return "signal_saved"
    if any(turn.get(flag) for flag in _SECURED_SIGNAL_CTX_FLAGS):
        return "signal_saved"
    saved = turn.get("signal_saved")
    if isinstance(saved, dict) and saved.get("signal_id"):
        return "signal_saved"

    # Joined the waitlist for a not-yet-covered area.
    if turn.get("waitlist_joined_now"):
        return "waitlist"

    return None


# ── Gate detection (verify / signup / login walls shown instead of the answer) ──

_GATE_PHASE_TYPES: dict[str, str] = {
    "gate_verify": "verify",
    "await_signup_phone": "signup",
    "await_signup_otp": "signup_otp",
    "await_login_phone": "login",
    "await_login_otp": "login_otp",
}


def gate_info(turn: dict[str, Any]) -> tuple[bool, str | None]:
    """(gate_shown, gate_type) — did this turn show an auth/verify gate?"""
    phase = str(turn.get("routing_phase") or "")
    if phase in _GATE_PHASE_TYPES:
        return True, _GATE_PHASE_TYPES[phase]
    if turn.get("requires_login_otp") or turn.get("login_otp_token"):
        return True, "login_otp"
    if turn.get("requires_phone_verification"):
        return True, "verify"
    return False, None


def build_lana_turn_props(
    *,
    session_id: str,
    turn_index: int,
    merged_ctx: dict[str, Any],
    ui_intent: str | None,
    latency_ms: int | None,
    block_resolved: bool,
) -> dict[str, Any]:
    """Event properties for the per-turn `lana_turn` analytics event.

    Pure assembly over the turn's merged context — the north-star metric
    (secured_step) and the full-name outcome both come from this module so the
    definition lives in exactly one place.
    """
    routing = merged_ctx.get("last_routing")
    routing = routing if isinstance(routing, dict) else {}
    turn = {**merged_ctx, "ui_intent": ui_intent}
    gate_shown, gate_type = gate_info(turn)
    return {
        "session_id": session_id,
        "turn_index": turn_index,
        "ui_intent": ui_intent,
        "intent": routing.get("intent_class"),
        "outcome": full_outcome(routing.get("outcome")),
        "secured_step": north_star_secured(turn),
        "gate_shown": gate_shown,
        "gate_type": gate_type,
        "block_resolved": block_resolved,
        "lang": merged_ctx.get("lang") or merged_ctx.get("language"),
        "latency_ms": latency_ms,
    }
