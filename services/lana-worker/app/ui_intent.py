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
UI_INTENT_UPLOAD_PROFILE_PHOTO = "upload_profile_photo"
UI_INTENT_SIGN_OUT = "sign_out"
UI_INTENT_OFFER_NEIGHBOR_INTRO = "offer_neighbor_intro"
UI_INTENT_PROPOSE_NEIGHBOR_INTRO = "propose_neighbor_intro"
UI_INTENT_SHOW_PENDING_INTROS = "show_pending_intros"
UI_INTENT_SHOW_BLOCK_LOG = "show_block_log"
UI_INTENT_SIGNAL_SAVED = "signal_saved"

# FE may render peer cards only on these intents (not on chat / verify turns).
PEER_SURFACE_UI_INTENTS = frozenset({
    UI_INTENT_SHOW_PEER_PREVIEW,
    UI_INTENT_OFFER_NEIGHBOR_INTRO,
    UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
})

_PHASE_TO_INTENT: dict[str, str] = {
    "need_zip": UI_INTENT_COLLECT_ZIP,
    "need_identity": UI_INTENT_COLLECT_IDENTITY,
    "need_display_name": UI_INTENT_COLLECT_DISPLAY_NAME,
    "await_signup_phone": UI_INTENT_COLLECT_PHONE,
    "await_signup_otp": UI_INTENT_COLLECT_OTP,
    "await_login_phone": UI_INTENT_COLLECT_PHONE,
    "await_login_otp": UI_INTENT_COLLECT_OTP,
    "gate_verify": UI_INTENT_COLLECT_PHONE,
    "await_profile_photo": UI_INTENT_UPLOAD_PROFILE_PHOTO,
    "await_logout": UI_INTENT_SIGN_OUT,
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
    phone_verified: bool = False,
) -> str:
    """
    Map session context → FE intent.

    Use with `routing_phase` (debug) and `auth_action` (Supabase handoff).
    """
    if ctx.get("intro_proposal"):
        return UI_INTENT_PROPOSE_NEIGHBOR_INTRO

    if ctx.get("pending_intro_offer"):
        return UI_INTENT_OFFER_NEIGHBOR_INTRO

    if ctx.get("pending_intros") is not None:
        return UI_INTENT_SHOW_PENDING_INTROS

    if ctx.get("block_log_entries") is not None:
        return UI_INTENT_SHOW_BLOCK_LOG

    if ctx.get("signal_saved"):
        return UI_INTENT_SIGNAL_SAVED

    if ready_to_complete:
        return UI_INTENT_CONFIRM_PROFILE

    phase = str(ctx.get("routing_phase") or "").strip()

    if phase in _PHASE_TO_INTENT:
        return _PHASE_TO_INTENT[phase]

    raw_action = ctx.get("auth_action")
    if isinstance(raw_action, dict):
        action_type = str(raw_action.get("type") or "").strip()
        if action_type in ("verify_signup_otp", "verify_login_otp"):
            return UI_INTENT_COLLECT_OTP
        if action_type == "link_phone_signup":
            return UI_INTENT_COLLECT_OTP

    if ctx.get("requires_login_otp") or ctx.get("login_otp_token"):
        return UI_INTENT_COLLECT_OTP

    guest_step = str(ctx.get("guest_step") or "").strip()
    if guest_step in _GUEST_STEP_TO_INTENT:
        return _GUEST_STEP_TO_INTENT[guest_step]

    if not phone_verified and ctx.get("requires_phone_verification"):
        return UI_INTENT_COLLECT_PHONE

    if phase == "preview":
        if activity_count > 0:
            return UI_INTENT_SHOW_ACTIVITY_PREVIEW
        if peer_count > 0:
            return UI_INTENT_SHOW_PEER_PREVIEW
        return UI_INTENT_CHAT

    if ctx.get("requires_phone_verification"):
        return UI_INTENT_COLLECT_PHONE

    if ctx.get("auth_intent") == "logout" or phase == "await_logout":
        return UI_INTENT_SIGN_OUT

    return UI_INTENT_CHAT
