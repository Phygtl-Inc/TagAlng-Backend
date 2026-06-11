"""Frontend UI intent — what input chrome to show after each Lana turn."""

from __future__ import annotations

from typing import Any

# Stable contract for PWA / mobile — do not rename without FE coordination.
UI_INTENT_CHAT = "chat"
UI_INTENT_COLLECT_ZIP = "collect_zip"
UI_INTENT_COLLECT_IDENTITY = "collect_identity"
UI_INTENT_COLLECT_DISPLAY_NAME = "collect_display_name"
UI_INTENT_COLLECT_PHONE = "collect_phone"
UI_INTENT_COLLECT_OTP = "collect_otp"
UI_INTENT_SHOW_PEER_PREVIEW = "show_peer_preview"
UI_INTENT_SHOW_ACTIVITY_PREVIEW = "show_activity_preview"
UI_INTENT_CONFIRM_PROFILE = "confirm_profile"

_PHASE_TO_INTENT: dict[str, str] = {
    "need_zip": UI_INTENT_COLLECT_ZIP,
    "need_identity": UI_INTENT_COLLECT_IDENTITY,
    "need_display_name": UI_INTENT_COLLECT_DISPLAY_NAME,
    "await_signup_phone": UI_INTENT_COLLECT_PHONE,
    "await_signup_otp": UI_INTENT_COLLECT_OTP,
    "await_login_phone": UI_INTENT_COLLECT_PHONE,
    "await_login_otp": UI_INTENT_COLLECT_OTP,
    "gate_verify": UI_INTENT_COLLECT_PHONE,
}

_GUEST_STEP_TO_INTENT: dict[str, str] = {
    "await_phone": UI_INTENT_COLLECT_PHONE,
    "awaiting_intro_name": UI_INTENT_COLLECT_DISPLAY_NAME,
}


def derive_ui_intent(
    ctx: dict[str, Any],
    *,
    ready_to_complete: bool = False,
    peer_count: int = 0,
    activity_count: int = 0,
) -> str:
    """
    Map session context → FE intent.

    Use with `routing_phase` (debug) and `auth_action` (Supabase handoff).
    """
    if ready_to_complete:
        return UI_INTENT_CONFIRM_PROFILE

    phase = str(ctx.get("routing_phase") or "").strip()
    if phase == "preview":
        if activity_count > 0:
            return UI_INTENT_SHOW_ACTIVITY_PREVIEW
        if peer_count > 0:
            return UI_INTENT_SHOW_PEER_PREVIEW
        return UI_INTENT_CHAT

    if phase in _PHASE_TO_INTENT:
        return _PHASE_TO_INTENT[phase]

    if ctx.get("requires_login_otp") or ctx.get("login_otp_token"):
        return UI_INTENT_COLLECT_OTP

    guest_step = str(ctx.get("guest_step") or "").strip()
    if guest_step in _GUEST_STEP_TO_INTENT:
        return _GUEST_STEP_TO_INTENT[guest_step]

    if ctx.get("requires_phone_verification"):
        return UI_INTENT_COLLECT_PHONE

    return UI_INTENT_CHAT
