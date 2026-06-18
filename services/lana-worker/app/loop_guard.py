"""Phrasing-independent loop breaker.

The router (LLM + thin regex) can get stuck re-emitting the same answer when
session state goes stale — repeating a peer list or a "saved your signal" line
no matter what the user types next. This guard keys on *repetition of the reply*,
not on any wording, so it catches every loop regardless of how the user phrases
their message. When it trips we reset the sticky discovery state and let the
orchestrator (LLM) answer fresh.
"""

from __future__ import annotations

import re
from typing import Any

# Funnel/auth phases legitimately repeat a prompt (e.g. "enter the 6-digit code")
# while the user retries — never treat those as a stuck loop.
_PROTECTED_PHASES = frozenset({
    "need_zip",
    "need_identity",
    "need_display_name",
    "await_signup_phone",
    "await_signup_otp",
    "await_profile_photo",
    "await_logout",
    "await_login_phone",
    "await_login_otp",
    "gate_verify",
})

# Sticky discovery state cleared on a detected loop so the next route starts clean.
_STICKY_DISCOVERY_KEYS = (
    "peer_matches",
    "activity_previews",
    "block_summary",
    "_discovery_slots",
    "_discovery_slots_for",
)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def trailing_assistant_repeats(history: list[dict[str, Any]] | None, reply: str) -> int:
    """How many of the most-recent assistant turns already say exactly `reply`."""
    target = _norm(reply)
    if not target:
        return 0
    count = 0
    for turn in reversed(history or []):
        if str(turn.get("role") or "") != "assistant":
            continue
        if _norm(turn.get("content")) == target:
            count += 1
        else:
            break
    return count


def discovery_reply_is_stuck(
    history: list[dict[str, Any]] | None,
    reply: str,
    ctx: dict[str, Any],
    *,
    threshold: int = 2,
) -> bool:
    """True when this reply repeats the last `threshold` assistant turns verbatim.

    Gated out for auth/funnel flows and tiny acknowledgements, where an identical
    reply is expected rather than a sign of being stuck.
    """
    if len(_norm(reply)) < 20:
        return False
    if str(ctx.get("routing_phase") or "") in _PROTECTED_PHASES:
        return False
    if ctx.get("auth_action") or ctx.get("requires_phone_verification") or ctx.get(
        "pending_post_verify"
    ):
        return False
    return trailing_assistant_repeats(history, reply) >= threshold


def reset_sticky_discovery_state(session_ctx: dict[str, Any]) -> None:
    """Drop stale discovery state so the orchestrator re-routes from scratch."""
    for key in _STICKY_DISCOVERY_KEYS:
        session_ctx.pop(key, None)
    session_ctx["peer_matches"] = []
    session_ctx["signal_draft"] = None
    session_ctx["active_intent"] = None
    session_ctx["routing_phase"] = "listening"