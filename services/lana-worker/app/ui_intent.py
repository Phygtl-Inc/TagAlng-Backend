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
UI_INTENT_RESPOND_PENDING_INTRO = "respond_pending_intro"
UI_INTENT_SHOW_BLOCK_LOG = "show_block_log"
UI_INTENT_SIGNAL_SAVED = "signal_saved"
UI_INTENT_SHOW_IDENTITY_PROFILE = "show_identity_profile"
UI_INTENT_COLLECT_SIGNAL_DETAIL = "collect_signal_detail"
UI_INTENT_COLLECT_EVENT_DETAIL = "collect_event_detail"
UI_INTENT_EVENT_CREATED = "event_created"
UI_INTENT_COLLECT_ITEM_DETAIL = "collect_item_detail"
UI_INTENT_ITEM_LISTED = "item_listed"
UI_INTENT_COLLECT_TIP_DETAIL = "collect_tip_detail"
UI_INTENT_TIP_LISTED = "tip_listed"

_INTENT_BLOCK_LOG = "discovery.block_log"
_INTENT_LIST_INTROS = "social.list_intros"
_INTENT_SHOW_PROFILE = "identity.show_my_profile"
_SIGNAL_ACTIVE_PREFIXES = ("looking.", "sharing.")

# FE may render peer cards only on these intents (not on chat / verify turns).
PEER_SURFACE_UI_INTENTS = frozenset({
    UI_INTENT_SHOW_PEER_PREVIEW,
    UI_INTENT_OFFER_NEIGHBOR_INTRO,
    UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
})

# Peer-match turns — FE should render cards when peer_matches is populated.
PEER_DISCOVERY_ACTIVE_INTENTS = frozenset({
    "discovery.find_peers",
    "discovery.find_by_attrs",
    "discovery.find_in_block",
    "discovery.show_peer_profile",
    "discovery.explain_peer_match",
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
    "signal_extract": UI_INTENT_COLLECT_SIGNAL_DETAIL,
    "signal_confirm_missing": UI_INTENT_COLLECT_SIGNAL_DETAIL,
    "signal_listening": UI_INTENT_COLLECT_SIGNAL_DETAIL,
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

    # In-chat event hosting — the draft card while capturing, the created card on publish.
    if ctx.get("event_published_now"):
        return UI_INTENT_EVENT_CREATED
    if ctx.get("event_host_active"):
        return UI_INTENT_COLLECT_EVENT_DETAIL

    # In-chat "pass along an item" — the item card while capturing, listed card on save.
    if ctx.get("item_listed_now"):
        return UI_INTENT_ITEM_LISTED
    if ctx.get("pass_along_active"):
        return UI_INTENT_COLLECT_ITEM_DETAIL

    # In-chat "share a tip" — the tip card while capturing, listed card once passed along.
    if ctx.get("tip_listed_now"):
        return UI_INTENT_TIP_LISTED
    if ctx.get("tip_share_active"):
        return UI_INTENT_COLLECT_TIP_DETAIL

    active = str(ctx.get("active_intent") or "").strip()

    if ctx.get("signal_saved") and (
        active.startswith(_SIGNAL_ACTIVE_PREFIXES) or active == "signal.capture"
    ):
        return UI_INTENT_SIGNAL_SAVED

    if active == _INTENT_BLOCK_LOG and ctx.get("block_log_entries") is not None:
        return UI_INTENT_SHOW_BLOCK_LOG

    if active == _INTENT_LIST_INTROS and ctx.get("pending_intros") is not None:
        return UI_INTENT_SHOW_PENDING_INTROS

    dup = ctx.get("recent_intro_duplicate")
    if isinstance(dup, dict) and dup.get("candidate_user_id"):
        return UI_INTENT_SHOW_BLOCK_LOG

    # Respond beats stale offer — only while an intro is actually waiting on the user.
    if ctx.get("pending_intro_respond"):
        return UI_INTENT_RESPOND_PENDING_INTRO

    if ctx.get("pending_intro_offer"):
        return UI_INTENT_OFFER_NEIGHBOR_INTRO

    if active == _INTENT_SHOW_PROFILE and ctx.get("identity_profile") is not None:
        return UI_INTENT_SHOW_IDENTITY_PROFILE

    if ctx.get("signal_draft"):
        return UI_INTENT_COLLECT_SIGNAL_DETAIL

    # confirm_profile is profile-intake only — never leak it onto a hosted event.
    if ready_to_complete and not ctx.get("event_host_active") and not ctx.get("event_published_now"):
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
        if peer_count > 0 and active in PEER_DISCOVERY_ACTIVE_INTENTS:
            return UI_INTENT_SHOW_PEER_PREVIEW
        if activity_count > 0 and active == "discovery.find_activities":
            return UI_INTENT_SHOW_ACTIVITY_PREVIEW
        if peer_count > 0:
            return UI_INTENT_SHOW_PEER_PREVIEW
        if activity_count > 0:
            return UI_INTENT_SHOW_ACTIVITY_PREVIEW
        return UI_INTENT_CHAT

    if ctx.get("requires_phone_verification"):
        return UI_INTENT_COLLECT_PHONE

    if ctx.get("auth_intent") == "logout" or phase == "await_logout":
        return UI_INTENT_SIGN_OUT

    return UI_INTENT_CHAT
