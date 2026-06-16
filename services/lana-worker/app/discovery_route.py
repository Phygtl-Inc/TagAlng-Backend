"""Discovery routing: find peers with ZIP → identity → preview → verify gate → full."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.auth import phone_has_registered_account, service_client
from app.claim_search import parse_claim_filters, peer_matches_identity_snippet
from app.discovery_slots import (
    ai_parse_discovery_turn,
    discovery_ai_enabled,
    discovery_slots_for_turn,
    slots_want_discovery_handling,
    slots_want_login,
    slots_want_logout,
    slots_want_preview_refetch,
    slots_want_signup_gate,
)
from app.turn_timing import TurnTimer
from app.turn_surfaces import clear_turn_surfaces
from app.guest_capabilities import (
    fetch_peer_matches,
    format_peer_matches,
    wants_host_activity,
    wants_peer_find,
)
from app.intro_proposal import (
    INTENT_PROPOSE_INTRO,
    accepts_intro_offer,
    build_match_reason,
    format_intro_offer_reply,
    stamp_intro_offer_ctx,
    stamp_intro_proposal_ctx,
    try_propose_intro_from_preview,
    wants_neighbor_intro,
    pick_peer_for_intro,
)
from app.intro_list import (
    INTENT_LIST_INTROS,
    fetch_my_intros,
    format_intros_list_reply,
    infer_intro_direction,
    stamp_pending_intros_ctx,
)
from app.local_signals import (
    INTENT_SAVE_SIGNAL,
    INTENT_SHOW_BLOCK_LOG,
    fetch_my_block_log,
    format_block_log_reply,
    format_signal_saved_reply,
    normalize_signal_intent,
    save_local_signal,
    stamp_block_log_ctx,
    stamp_signal_saved_ctx,
)
from app.guest_login import (
    GUEST_STEP_LOGIN_OTP,
    _exit_login_ctx,
    _exit_logout_ctx,
    _login_ctx,
    _logout_ctx,
    extract_otp_code,
    extract_phone_e164,
    handle_guest_login,
    wants_cancel_logout,
    wants_login as wants_login_intent,
    wants_logout as wants_logout_intent,
)
from app.claims_persist import (
    extract_display_name_reply,
    extract_nickname_from_message,
    persist_profile_patch,
    scrub_negative_heritage_claims,
    user_needs_display_name,
)
from app.profile_photo import handle_profile_photo_turn, user_profile_photo_url
from app.supabase_rpc import call_rpc
from app.layer1_handlers import (
    HELP_WHAT_CAN_YOU_DO,
    HELP_WHO_ARE_YOU,
    fetch_block_summary,
    fetch_identity_dashboard,
    fetch_peers_by_attr_filter,
    summarize_partial_claim_matches,
    format_attr_peers_reply,
    format_block_summary_reply,
    format_identity_profile_reply,
    format_peer_detail_reply,
    handle_add_or_edit_claim,
    handle_change_name,
    handle_notification_prefs,
    peers_to_match_rows,
    stamp_identity_profile_ctx,
)
from app.layer1_intents import (
    LOOKING_SHARING_INTENTS,
    intent_confidence_met,
    is_block_activity_browse,
    is_profile_acknowledgment,
    normalize_attr_filter_text,
    phrase_linear_intent,
    slots_linear_intent,
)
from app.layer1_tier import handle_respond_nudge
from app.signal_capture import (
    PHASE_SIGNAL_CONFIRM,
    PHASE_SIGNAL_EXTRACT,
    PHASE_SIGNAL_LISTENING,
    advance_signal_draft,
    clear_signal_draft,
    draft_from_slots,
    is_signal_lane_intent,
    should_abandon_signal_draft,
)

PHASE_NEED_ZIP = "need_zip"
PHASE_NEED_IDENTITY = "need_identity"
PHASE_NEED_DISPLAY_NAME = "need_display_name"
PHASE_PREVIEW = "preview"
PHASE_GATE_VERIFY = "gate_verify"
PHASE_AWAIT_SIGNUP_PHONE = "await_signup_phone"
PHASE_AWAIT_SIGNUP_OTP = "await_signup_otp"
PHASE_AWAIT_PROFILE_PHOTO = "await_profile_photo"
PHASE_AWAIT_LOGOUT = "await_logout"

INTENT_FIND_PEERS = "discovery.find_peers"
INTENT_FIND_ACTIVITIES = "discovery.find_activities"
_DISCOVERY_GOALS = frozenset({"peers", "activities", "both"})

_FUNNEL_PHASES = frozenset(
    {PHASE_NEED_ZIP, PHASE_NEED_IDENTITY, PHASE_NEED_DISPLAY_NAME}
)

_MORE_DETAIL_RE = re.compile(
    r"\b(more|names?|introduce|connect|who are they|show me|full|details?|"
    r"see them|meet them|talk to)\b",
    re.I,
)
_PEER_DRILLDOWN_RE = re.compile(
    r"\b(?:show me|tell me about|details? (?:on|about|for)|more about)\b.*"
    r"\b(?:first|second|third|\d+(?:st|nd|rd)?|neighbor|neighbour)\b"
    r"|\b(?:first|second|third|\d+(?:st|nd|rd)?)\b.*"
    r"\b(?:neighbor|neighbour|mom|dad|peer|match)\b.*\b(?:detail|details|more|who)\b",
    re.I,
)
_VERIFY_HELP_RE = re.compile(
    r"\b(how (?:do|can) i verify|verify (?:my |me|a )?phone|phone verif|get verified|"
    r"unlock (?:names|matches)|need to verify)\b",
    re.I,
)
_RSVP_RE = re.compile(
    r"\b(rsvp|sign up for|join|take part in|attend|going to|i want to go|count me in)\b",
    re.I,
)

# "sign me up" / account creation intent (not RSVP/event intent).
_SIGNUP_INTENT_RE = re.compile(
    # Note: exclude "sign up for ..." (events) so RSVP gating keeps working.
    r"\b(sign\s*(?:me\s*)?up(?!\s*for\b)|signup(?!\s*for\b)|create\s+(?:an?\s+)?account|complete\s+(?:registration|signup)|finish\s+signing\s+up)\b",
    re.I,
)
_ACTIVITIES_RE = re.compile(
    r"\b(activit\w*|events?|what'?s (?:happening|going on)|things to do)\b",
    re.I,
)
_ZIP_RE = re.compile(r"\b(\d{5})\b")
_META_CHAT_RE = re.compile(
    r"\b(are you (?:real|ai|a bot|human|dumb|stupid)|who are you|what are you)\b|^\s*what\?+\s*$",
    re.I,
)
_NOT_IDENTITY_REPLIES = frozenset(
    {"hello", "hi", "hey", "ok", "okay", "yes", "no", "thanks", "thank you", "yep", "nope"}
)
_AFFIRMATIVE_REPLIES = frozenset(
    {"ok", "okay", "yes", "yeah", "yep", "sure", "done", "ready", "go", "great", "perfect"}
)


def _clear_peer_surface(ctx: dict[str, Any]) -> None:
    """Drop stale peer cards when this turn is not a peer-match response."""
    ctx["peer_matches"] = []


def _wants_block_log(msg: str, slots: dict[str, Any]) -> bool:
    if is_block_activity_browse(msg):
        return False
    if phrase_linear_intent(msg) == "discovery.block_log":
        return True
    linear = slots_linear_intent(slots)
    if linear == "discovery.block_log":
        return intent_confidence_met(slots, linear)
    goal = str(slots.get("goal") or "none")
    return goal == "show_block_log" and float(slots.get("confidence", 0.0)) >= 0.5


def _preview_peers_with_ids(
    *,
    user_jwt: str,
    session_ctx: dict[str, Any],
    block_id: str,
    phone_verified: bool,
    home_block_id: str | None = None,
) -> list[dict[str, Any]]:
    stored = session_ctx.get("peer_matches")
    if isinstance(stored, list) and stored:
        if any(p.get("peer_user_id") for p in stored if isinstance(p, dict)):
            return [p for p in stored if isinstance(p, dict)]
    if phone_verified:
        _try_assign_home_block(
            user_jwt,
            session_ctx=session_ctx,
            home_block_id=home_block_id,
        )
        try:
            peers = fetch_peer_matches(user_jwt, limit=5)
            if peers:
                return peers
        except Exception:
            pass
        return fetch_preview_peers_on_block(block_id, limit=5, include_peer_ids=True)
    return fetch_preview_peers_on_block(block_id, limit=3)


def _try_neighbor_intro_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    ctx_base: dict[str, Any],
    user_jwt: str,
    block_id: str,
    phone_verified: bool,
    goal: str,
    slots: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if not phone_verified:
        return None
    pending = session_ctx.get("pending_intro_offer")
    wants_intro = goal == "propose_intro" or wants_neighbor_intro(msg)
    if not wants_intro and pending and accepts_intro_offer(msg):
        wants_intro = True
    if not wants_intro and slots and str(slots.get("goal") or "") == "propose_intro":
        wants_intro = float(slots.get("confidence", 0.0)) >= 0.5
    if not wants_intro:
        return None

    peers = _preview_peers_with_ids(
        user_jwt=user_jwt,
        session_ctx=session_ctx,
        block_id=block_id,
        phone_verified=phone_verified,
        home_block_id=ctx_base.get("home_block_id"),
    )
    identity = str(ctx_base.get("identity_snippet") or session_ctx.get("identity_snippet") or "").strip()
    result = try_propose_intro_from_preview(
        msg=msg,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        peers=peers,
        identity_snippet=identity or None,
        force=True,
    )
    if result is None:
        if wants_intro and not any(p.get("peer_user_id") for p in peers):
            snippet = str(session_ctx.get("identity_snippet") or "").strip()
            if not snippet:
                return (
                    "Tell me one thing about you — life stage, heritage, or what you're looking for — "
                    "then I can introduce you to someone on your block.",
                    _routing_ctx(
                        ctx_base,
                        phase=PHASE_NEED_IDENTITY,
                        preview_block_id=block_id,
                        active_intent=INTENT_PROPOSE_INTRO,
                    ),
                    _discovery_routing_stub(PHASE_NEED_IDENTITY, "intro_need_identity"),
                    peers,
                )
            return (
                "I'm still loading named matches for your block — say your first name, "
                "or tell me which neighbor (e.g. first one or Neighbor 1).",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_PREVIEW,
                    preview_block_id=block_id,
                    active_intent=INTENT_PROPOSE_INTRO,
                ),
                _discovery_routing_stub(PHASE_PREVIEW, "intro_need_verified_peers"),
                peers,
            )
        return None

    reply, intro = result
    selected_peer = next(
        (
            p
            for p in peers
            if str(p.get("peer_user_id") or "") == str(intro.get("candidate_user_id") or "")
        ),
        None,
    )
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_PREVIEW,
        preview_block_id=block_id,
        active_intent=INTENT_PROPOSE_INTRO,
    )
    if intro.get("intro_id"):
        peer = selected_peer or {
            "peer_user_id": intro.get("candidate_user_id"),
            "matching_peer_label": intro.get("match_reason"),
        }
        stamp_intro_proposal_ctx(ctx, intro=intro, peer=peer)
        ctx.pop("recent_intro_duplicate", None)
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "lana_propose_neighbor_intro")
    else:
        if str(intro.get("status") or "") == "duplicate":
            ctx["recent_intro_duplicate"] = {
                "candidate_user_id": intro.get("candidate_user_id"),
                "candidate_nickname": (
                    (selected_peer or {}).get("nickname")
                    or (selected_peer or {}).get("matching_peer_label")
                    or "that neighbor"
                ),
                "match_reason": str((selected_peer or {}).get("matching_peer_label") or "").strip(),
            }
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, str(intro.get("status") or "intro_skipped"))
    ctx.pop("activity_previews", None)
    if selected_peer:
        ctx["peer_matches"] = [selected_peer]
        return reply, ctx, ctx["last_routing"], [selected_peer]
    ctx["peer_matches"] = []
    return reply, ctx, ctx["last_routing"], []


def _maybe_attach_intro_offer(
    *,
    reply: str,
    peers: list[dict[str, Any]],
    ctx: dict[str, Any],
    identity_snippet: str | None,
    msg: str | None = None,
) -> str:
    if ctx.get("intro_offer_shown") or ctx.get("intro_proposal") or ctx.get("pending_intro_offer"):
        return reply
    if msg and (is_profile_acknowledgment(msg) or (_is_affirmative(msg) and not wants_peer_find(msg))):
        return reply
    peer = next((p for p in peers if p.get("peer_user_id")), None)
    if not peer:
        return reply
    if not peer_matches_identity_snippet(peer, identity_snippet):
        return reply
    reason = build_match_reason(identity_snippet=identity_snippet, peer=peer)
    stamp_intro_offer_ctx(ctx, peer=peer, match_reason=reason)
    ctx["intro_offer_shown"] = True
    return f"{reply}\n\n{format_intro_offer_reply(peer, reason)}"


def _try_awaiting_name_change_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Finish settings.change_name when user replies with just their name."""
    awaiting = bool(session_ctx.get("awaiting_name_change"))
    rename_flow = (
        str(session_ctx.get("active_intent") or "") == "settings.change_name"
        and str(session_ctx.get("routing_phase") or "") == PHASE_NEED_DISPLAY_NAME
    )
    if not awaiting and not rename_flow:
        return None
    reply, nick = handle_change_name(user_id, msg)
    ctx = _routing_ctx(
        session_ctx,
        phase=(phase or "listening") if nick else PHASE_NEED_DISPLAY_NAME,
        active_intent="settings.change_name",
    )
    if nick:
        ctx["display_name_saved"] = True
        ctx["nickname"] = nick
        ctx.pop("awaiting_name_change", None)
    else:
        ctx["awaiting_name_change"] = True
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "update_user_name")
    return reply, ctx, ctx["last_routing"], []


def _try_layer1_intent_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
    user_id: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Layer 1 explicit intents — identity, block summary, settings, help."""
    linear = slots_linear_intent(slots)
    if not linear or not intent_confidence_met(slots, linear):
        return None

    ctx_base = dict(session_ctx)

    if linear == "identity.show_my_profile":
        if not phone_verified:
            return (
                "Verify your phone first — then I can show your full profile and claims.",
                _routing_ctx(
                    ctx_base,
                    phase=phase or "listening",
                    active_intent="identity.show_my_profile",
                ),
                _discovery_routing_stub(phase or "listening", "show_profile_need_verify"),
                [],
            )
        if user_id:
            scrub_negative_heritage_claims(user_id)
        dashboard = fetch_identity_dashboard(user_jwt)
        reply = format_identity_profile_reply(dashboard)
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or PHASE_PREVIEW,
            active_intent="identity.show_my_profile",
        )
        stamp_identity_profile_ctx(ctx, dashboard)
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "get_my_profile_dashboard")
        return reply, ctx, ctx["last_routing"], []

    if linear in ("identity.add_claim", "identity.edit_claim"):
        reply, saved = handle_add_or_edit_claim(user_id, msg, linear_intent=linear)
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent=linear,
        )
        if saved > 0 and phone_verified:
            try:
                dashboard = fetch_identity_dashboard(user_jwt)
                stamp_identity_profile_ctx(ctx, dashboard)
            except HTTPException:
                pass
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "extract_identity_claims")
        return reply, ctx, ctx["last_routing"], []

    if linear == "discovery.block_log":
        if not phone_verified:
            return (
                "Verify your phone first — then I can show your block log.",
                _routing_ctx(
                    ctx_base,
                    phase=phase or "listening",
                    active_intent=INTENT_SHOW_BLOCK_LOG,
                ),
                _discovery_routing_stub(phase or "listening", "block_log_need_verify"),
                [],
            )
        try:
            entries = fetch_my_block_log(user_jwt)
        except HTTPException as exc:
            detail = str(exc.detail or "").lower()
            if (
                "pgrst202" in detail
                or "get_my_block_log" in detail
                or "read-only transaction" in detail
                or "25006" in detail
            ):
                return (
                    "Your block log isn't available yet — we're still rolling it out on this environment.",
                    _routing_ctx(
                        ctx_base,
                        phase=phase or "listening",
                        active_intent=INTENT_SHOW_BLOCK_LOG,
                    ),
                    _discovery_routing_stub(phase or "listening", "block_log_unavailable"),
                    [],
                )
            raise
        reply = format_block_log_reply(entries)
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or PHASE_PREVIEW,
            active_intent=INTENT_SHOW_BLOCK_LOG,
        )
        stamp_block_log_ctx(ctx, entries)
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "get_my_block_log")
        ctx.pop("activity_previews", None)
        _clear_peer_surface(ctx)
        return reply, ctx, ctx["last_routing"], []

    if linear == "discovery.find_in_block":
        block_id = _resolve_block_id_for_turn(
            session_ctx=session_ctx,
            home_block_id=home_block_id,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
        )
        if not block_id and not phone_verified:
            return (
                "What ZIP are you in? Once I know your block I can summarize what's happening nearby.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_NEED_ZIP,
                    active_intent="discovery.find_in_block",
                ),
                _discovery_routing_stub(PHASE_NEED_ZIP, "block_summary_need_zip"),
                [],
            )
        summary = fetch_block_summary(user_jwt, block_id=block_id)
        reply = format_block_summary_reply(
            block_name=str(summary.get("block_name") or "your block"),
            neighbor_count=int(summary.get("neighbor_count") or 0),
            match_count=int(summary.get("match_count") or 0),
            block_state=summary.get("block_state"),
            active_signal_count=int(summary.get("active_signal_count") or 0),
            browse_mode=is_block_activity_browse(msg),
        )
        sig_n = int(summary.get("active_signal_count") or 0)
        if sig_n > 0 and not is_block_activity_browse(msg):
            reply += f" {sig_n} neighbor ask{'s' if sig_n != 1 else ''} or offer{'s' if sig_n != 1 else ''} active on your block."
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or PHASE_PREVIEW,
            active_intent="discovery.find_in_block",
        )
        ctx["block_summary"] = summary
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "get_block_summary")
        _clear_peer_surface(ctx)
        ctx.pop("activity_previews", None)
        return reply, ctx, ctx["last_routing"], []

    if linear == "identity.complete_profile":
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="identity.complete_profile",
        )
        if is_profile_acknowledgment(msg):
            ctx["last_routing"] = _discovery_routing_stub(phase or PHASE_PREVIEW, "profile_ack")
            return (
                "Perfect — I've got you. Want neighbors like you, to post a swap, or your block log?",
                ctx,
                ctx["last_routing"],
                [],
            )
        ctx["ready_to_complete"] = True
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "complete_profile")
        return (
            "That's you — tap Complete when you're ready and I'll lock in your profile.",
            ctx,
            ctx["last_routing"],
            [],
        )

    if linear == "discovery.find_by_attrs":
        if not phone_verified:
            return (
                "Verify your phone first — then I can search neighbors by those traits.",
                _routing_ctx(
                    ctx_base,
                    phase=phase or "listening",
                    active_intent="discovery.find_by_attrs",
                ),
                _discovery_routing_stub(phase or "listening", "find_by_attrs_need_verify"),
                [],
            )
        filter_text = normalize_attr_filter_text(msg, slots)
        if len(filter_text) < 2:
            return (
                "Who should I look for — heritage, life stage, language, interests?",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_NEED_IDENTITY,
                    active_intent="discovery.find_by_attrs",
                ),
                _discovery_routing_stub(PHASE_NEED_IDENTITY, "find_by_attrs_need_filter"),
                [],
            )
        try:
            peers = fetch_peers_by_attr_filter(user_jwt, filter_text, limit=5, slots=slots)
        except HTTPException:
            peers = []
        partial_summary = None
        if not peers:
            partial_summary = summarize_partial_claim_matches(
                user_jwt,
                parse_claim_filters(filter_text, slots),
            )
        reply = format_attr_peers_reply(
            peers,
            filter_text=filter_text,
            partial_summary=partial_summary,
        )
        peer_rows = peers_to_match_rows(peers, phone_verified=phone_verified)
        ctx = _routing_ctx(
            ctx_base,
            phase=PHASE_PREVIEW,
            active_intent="discovery.find_by_attrs",
            identity_snippet=filter_text,
        )
        ctx["peer_matches"] = peer_rows
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "find_peers_by_attr_filter")
        ctx.pop("activity_previews", None)
        return reply, ctx, ctx["last_routing"], peer_rows

    if linear == "settings.change_zip":
        return (
            "Sure — what's your new ZIP code?",
            _routing_ctx(
                ctx_base,
                phase=PHASE_NEED_ZIP,
                active_intent="settings.change_zip",
            ),
            _discovery_routing_stub(PHASE_NEED_ZIP, "settings_change_zip"),
            [],
        )

    if linear == "settings.change_name":
        reply, nick = handle_change_name(user_id, msg)
        ctx = _routing_ctx(
            ctx_base,
            phase=(phase or "listening") if nick else PHASE_NEED_DISPLAY_NAME,
            active_intent="settings.change_name",
        )
        if nick:
            ctx["display_name_saved"] = True
            ctx["nickname"] = nick
            ctx.pop("awaiting_name_change", None)
        else:
            ctx["awaiting_name_change"] = True
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "update_user_name")
        return reply, ctx, ctx["last_routing"], []

    if linear == "settings.notification_prefs":
        reply, pref = handle_notification_prefs(user_jwt, msg)
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="settings.notification_prefs",
        )
        ctx["notification_prefs"] = {"sms": pref}
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "update_user_notification_prefs")
        return reply, ctx, ctx["last_routing"], []

    if linear == "help.what_can_you_do":
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="help.what_can_you_do",
        )
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "help_capabilities")
        return HELP_WHAT_CAN_YOU_DO, ctx, ctx["last_routing"], []

    if linear == "help.who_are_you":
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="help.who_are_you",
        )
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "help_who_are_you")
        return HELP_WHO_ARE_YOU, ctx, ctx["last_routing"], []

    if linear == "tier.respond_nudge":
        reply, pending, _action = handle_respond_nudge(
            msg, user_jwt=user_jwt, session_ctx=session_ctx
        )
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or PHASE_PREVIEW,
            active_intent="tier.respond_nudge",
        )
        if pending:
            ctx["pending_intro_respond"] = pending
        else:
            ctx.pop("pending_intro_respond", None)
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "respond_nudge")
        return reply, ctx, ctx["last_routing"], []

    return None


def _try_signal_lane_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """LOOKING/SHARING 4-phase cascade → save_local_signal."""
    ctx_base = dict(session_ctx)
    draft = ctx_base.get("signal_draft")
    if isinstance(draft, dict) and should_abandon_signal_draft(msg, draft, slots):
        clear_signal_draft(ctx_base)
        draft = None
    linear = slots_linear_intent(slots) if slots else None
    active_linear = None
    if isinstance(draft, dict):
        active_linear = str(draft.get("linear_intent") or "")
    elif linear and is_signal_lane_intent(slots):
        active_linear = str(linear)

    if active_linear or isinstance(draft, dict):
        if not phone_verified:
            return (
                "Verify your phone first — then I can post that to your block.",
                _routing_ctx(
                    ctx_base,
                    phase=phase or "listening",
                    active_intent=active_linear or INTENT_SAVE_SIGNAL,
                ),
                _discovery_routing_stub(phase or "listening", "save_signal_need_verify"),
                [],
            )
        if not resolve_block_id(session_ctx, home_block_id):
            block_id = _resolve_block_id_for_turn(
                session_ctx=session_ctx,
                home_block_id=home_block_id,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
            )
            if block_id:
                ctx_base["preview_block_id"] = block_id
        if not resolve_block_id(ctx_base, home_block_id):
            return (
                "What ZIP are you in? Once I know your block I can save that for neighbors nearby.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_NEED_ZIP,
                    active_intent=active_linear or INTENT_SAVE_SIGNAL,
                ),
                _discovery_routing_stub(PHASE_NEED_ZIP, "save_signal_need_zip"),
                [],
            )

    if isinstance(draft, dict):
        updated, prompt, ready = advance_signal_draft(draft, msg=msg)
        ctx_base["signal_draft"] = updated
        route_phase = str(updated.get("phase") or PHASE_SIGNAL_EXTRACT)
        active = str(updated.get("linear_intent") or INTENT_SAVE_SIGNAL)
        if prompt and not ready:
            ctx = _routing_ctx(
                ctx_base,
                phase=route_phase,
                active_intent=active,
            )
            ctx["last_routing"] = _discovery_routing_stub(route_phase, "signal_confirm_missing")
            return prompt, ctx, ctx["last_routing"], []
        if ready:
            save_slots = {
                "goal": "save_signal",
                "confidence": 0.95,
                "signal_intent": updated.get("intent"),
                "signal_detail": updated.get("detail"),
                "signal_category": updated.get("category"),
                "linear_intent": updated.get("linear_intent"),
            }
            ctx_base.pop("signal_draft", None)
            clear_signal_draft(ctx_base)
            return _try_save_signal_turn(
                msg=msg,
                slots=save_slots,
                session_ctx=ctx_base,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
                home_block_id=home_block_id,
                phase=phase,
            )

    if not slots or not is_signal_lane_intent(slots):
        return None
    if not intent_confidence_met(slots, str(linear or "")):
        return None

    new_draft = draft_from_slots(slots, msg=msg)
    updated, prompt, ready = advance_signal_draft(new_draft, msg=msg)
    ctx_base["signal_draft"] = updated
    active = str(updated.get("linear_intent") or linear or INTENT_SAVE_SIGNAL)
    if prompt and not ready:
        ctx = _routing_ctx(
            ctx_base,
            phase=str(updated.get("phase") or PHASE_SIGNAL_CONFIRM),
            active_intent=active,
        )
        ctx["last_routing"] = _discovery_routing_stub(PHASE_SIGNAL_CONFIRM, "signal_extract")
        return prompt, ctx, ctx["last_routing"], []
    if ready:
        ctx_base.pop("signal_draft", None)
        clear_signal_draft(ctx_base)
        save_slots = {
            **slots,
            "goal": "save_signal",
            "confidence": max(float(slots.get("confidence", 0.9)), 0.9),
            "signal_intent": updated.get("intent"),
            "signal_detail": updated.get("detail"),
            "signal_category": updated.get("category"),
            "linear_intent": updated.get("linear_intent"),
        }
        return _try_save_signal_turn(
            msg=msg,
            slots=save_slots,
            session_ctx=ctx_base,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
        )
    return None


def _try_peer_detail_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if not looks_like_peer_drilldown(msg):
        return None
    if _wants_block_log(msg, slots):
        return None
    ctx_base = dict(session_ctx)
    block_id = _resolve_block_id_for_turn(
        session_ctx=session_ctx,
        home_block_id=home_block_id,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
    )
    if not block_id:
        return None
    peers = _preview_peers_with_ids(
        user_jwt=user_jwt,
        session_ctx=session_ctx,
        block_id=block_id,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
    )
    if not peers:
        return (
            "I don't have neighbor matches loaded yet — say find people like me first.",
            _routing_ctx(
                ctx_base,
                phase=phase or PHASE_PREVIEW,
                active_intent=INTENT_FIND_PEERS,
                preview_block_id=block_id,
            ),
            _discovery_routing_stub(phase or "listening", "peer_detail_empty"),
            [],
        )
    selected = pick_peer_for_intro(peers, msg=msg)
    if not selected:
        return None
    peer_index = None
    for i, peer in enumerate(peers):
        if not isinstance(peer, dict):
            continue
        if peer is selected:
            peer_index = i
            break
        if str(peer.get("peer_user_id") or "") and str(peer.get("peer_user_id") or "") == str(
            selected.get("peer_user_id") or ""
        ):
            peer_index = i
            break
    reply = format_peer_detail_reply(selected, index=peer_index)
    peer_rows = peers_to_match_rows([selected], phone_verified=phone_verified)
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_PREVIEW,
        preview_block_id=block_id,
        active_intent=INTENT_FIND_PEERS,
    )
    ctx["peer_matches"] = peer_rows
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "peer_detail")
    ctx.pop("activity_previews", None)
    return reply, ctx, ctx["last_routing"], peer_rows


def _try_list_intros_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if _wants_block_log(msg, slots):
        return None
    if looks_like_peer_drilldown(msg):
        return None
    goal = str(slots.get("goal") or "none")
    if goal != "list_intros" or float(slots.get("confidence", 0.0)) < 0.5:
        return None

    ctx_base = dict(session_ctx)
    if not phone_verified:
        return (
            "Verify your phone first — then I can show your pending intros.",
            _routing_ctx(ctx_base, phase=phase or "listening", active_intent=INTENT_LIST_INTROS),
            _discovery_routing_stub(phase or "listening", "list_intros_need_verify"),
            [],
        )

    direction = infer_intro_direction(msg, slots)
    try:
        intros = fetch_my_intros(user_jwt, direction=direction)
        if not intros and direction in ("sent", "received"):
            intros = fetch_my_intros(user_jwt, direction="all")
    except HTTPException as exc:
        if exc.detail == "phone_not_verified":
            return (
                "Verify your phone first — then I can show your pending intros.",
                _routing_ctx(ctx_base, phase=phase or "listening", active_intent=INTENT_LIST_INTROS),
                _discovery_routing_stub(phase or "listening", "list_intros_need_verify"),
                [],
            )
        raise

    reply = format_intros_list_reply(intros)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=INTENT_LIST_INTROS,
    )
    stamp_pending_intros_ctx(ctx, intros)
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "get_my_intros")
    ctx.pop("activity_previews", None)
    _clear_peer_surface(ctx)
    return reply, ctx, ctx["last_routing"], []


def _try_save_signal_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    linear = slots_linear_intent(slots)
    goal = str(slots.get("goal") or "none")
    is_signal = goal == "save_signal" or (linear in LOOKING_SHARING_INTENTS)
    if not is_signal:
        return None
    if linear and not intent_confidence_met(slots, linear):
        if goal != "save_signal" or float(slots.get("confidence", 0.0)) < 0.55:
            return None
    elif goal == "save_signal" and float(slots.get("confidence", 0.0)) < 0.55:
        return None

    ctx_base = dict(session_ctx)
    intent = normalize_signal_intent(slots.get("signal_intent"))
    active_intent = linear or INTENT_SAVE_SIGNAL
    detail = str(slots.get("signal_detail") or msg or "").strip()[:500]
    category = str(slots.get("signal_category") or "").strip() or None

    if not intent:
        return None
    if not detail:
        return (
            "Tell me a bit more — what are you looking for or offering on your block?",
            _routing_ctx(ctx_base, phase=phase or "listening", active_intent=active_intent),
            _discovery_routing_stub(phase or "listening", "save_signal_need_detail"),
            [],
        )

    if not phone_verified:
        return (
            "Verify your phone first — then I can post that to your block.",
            _routing_ctx(ctx_base, phase=phase or "listening", active_intent=active_intent),
            _discovery_routing_stub(phase or "listening", "save_signal_need_verify"),
            [],
        )

    block_id = resolve_block_id(session_ctx, home_block_id)
    if not block_id:
        return (
            "What ZIP are you in? Once I know your block I can save that for neighbors nearby.",
            _routing_ctx(ctx_base, phase=PHASE_NEED_ZIP, active_intent=active_intent),
            _discovery_routing_stub(PHASE_NEED_ZIP, "save_signal_need_zip"),
            [],
        )

    try:
        result = save_local_signal(
            user_jwt,
            intent=intent,
            detail_text=detail,
            category=category,
            block_id=block_id,
            zip_code=str(session_ctx.get("zip") or "") or None,
        )
    except HTTPException as exc:
        detail_err = str(exc.detail or "").lower()
        if "block_required" in detail_err:
            return (
                "What ZIP are you in? Once I know your block I can save that for neighbors nearby.",
                _routing_ctx(ctx_base, phase=PHASE_NEED_ZIP, active_intent=active_intent),
                _discovery_routing_stub(PHASE_NEED_ZIP, "save_signal_need_zip"),
                [],
            )
        raise

    reply = format_signal_saved_reply(result, detail=detail)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=active_intent,
    )
    stamp_signal_saved_ctx(ctx, result, active_intent=active_intent)
    if int(result.get("matches_created") or 0) > 0:
        try:
            entries = fetch_my_block_log(user_jwt)
            stamp_block_log_ctx(ctx, entries)
        except HTTPException:
            pass
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "save_local_signal")
    ctx.pop("activity_previews", None)
    clear_signal_draft(ctx)
    return reply, ctx, ctx["last_routing"], []


def _try_show_block_log_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if not _wants_block_log(msg, slots):
        return None

    ctx_base = dict(session_ctx)
    if not phone_verified:
        return (
            "Verify your phone first — then I can show your block log.",
            _routing_ctx(ctx_base, phase=phase or "listening", active_intent=INTENT_SHOW_BLOCK_LOG),
            _discovery_routing_stub(phase or "listening", "block_log_need_verify"),
            [],
        )

    try:
        entries = fetch_my_block_log(user_jwt)
    except HTTPException as exc:
        detail = str(exc.detail or "").lower()
        if (
            "pgrst202" in detail
            or "get_my_block_log" in detail
            or "read-only transaction" in detail
            or "25006" in detail
        ):
            return (
                "Your block log isn't available yet — we're still rolling it out on this environment.",
                _routing_ctx(ctx_base, phase=phase or "listening", active_intent=INTENT_SHOW_BLOCK_LOG),
                _discovery_routing_stub(phase or "listening", "block_log_unavailable"),
                [],
            )
        raise
    reply = format_block_log_reply(entries)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=INTENT_SHOW_BLOCK_LOG,
    )
    stamp_block_log_ctx(ctx, entries)
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "get_my_block_log")
    ctx.pop("activity_previews", None)
    _clear_peer_surface(ctx)
    return reply, ctx, ctx["last_routing"], []


def wants_more_peer_detail(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    # "what's my name" is profile chat, not a request to reveal neighbor names.
    if re.search(r"\b(?:what(?:'s| is)\s+my\s+name|my\s+name)\b", s, re.I):
        return False
    return bool(_MORE_DETAIL_RE.search(s))


def looks_like_peer_drilldown(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if re.search(r"\b(?:show my intros|my intros|pending intros|intro inbox)\b", s, re.I):
        return False
    if _PEER_DRILLDOWN_RE.search(s):
        return True
    return bool(
        wants_more_peer_detail(s)
        and re.search(r"\b(?:neighbor|neighbour|mom|dad|peer|match)\b", s, re.I)
    )


def wants_verify_help(text: str) -> bool:
    return bool(_VERIFY_HELP_RE.search(str(text or "").strip()))


def wants_rsvp_intent(text: str) -> bool:
    return bool(_RSVP_RE.search(str(text or "").strip()))


def wants_signup_intent(text: str) -> bool:
    """Regex fallback when discovery AI slots are off."""
    return bool(_SIGNUP_INTENT_RE.search(str(text or "").strip()))


def _login_flow_active(session_ctx: dict[str, Any]) -> bool:
    if session_ctx.get("auth_intent") == "login":
        return True
    phase = str(session_ctx.get("routing_phase") or "")
    if phase in ("await_login_phone", "await_login_otp"):
        return True
    step = session_ctx.get("guest_step")
    return step in ("await_login_phone", "await_login_otp")


def _turn_wants_login(
    msg: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    if _login_flow_active(session_ctx):
        return True
    if discovery_ai_enabled():
        return slots_want_login(slots)
    return wants_login_intent(msg)


def _turn_wants_signup_gate(
    msg: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    if discovery_ai_enabled():
        return slots_want_signup_gate(slots)
    return wants_signup_intent(msg)


def _turn_wants_logout(
    msg: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    if discovery_ai_enabled():
        return slots_want_logout(slots)
    return wants_logout_intent(msg)


def wants_activities_browse(text: str) -> bool:
    s = str(text or "").strip()
    if re.search(r"\b(?:on|in) (?:my )?block\b", s, re.I):
        return False
    return bool(_ACTIVITIES_RE.search(s))


def _active_intent_for_goal(goal: str, slots: dict[str, Any] | None = None) -> str | None:
    if slots:
        linear = slots_linear_intent(slots)
        if linear:
            return linear
    if goal == "activities":
        return INTENT_FIND_ACTIVITIES
    if goal in ("peers", "both"):
        return INTENT_FIND_PEERS
    return None


def _update_discovery_goal_from_slots(
    session_ctx: dict[str, Any],
    slots: dict[str, Any],
) -> None:
    """Persist browse goal when Flash names peers/activities/both this turn."""
    slot_goal = str(slots.get("goal") or "none").lower()
    conf = float(slots.get("confidence", 0.0))
    if slot_goal in _DISCOVERY_GOALS and conf >= 0.45:
        session_ctx["discovery_goal"] = slot_goal


def _effective_discovery_goal(
    msg: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any],
) -> str:
    """
    Browse goal for this turn: Flash when explicit; else persisted across ZIP/continue steps.
    Updates session.discovery_goal when user pivots (e.g. peers → activities).
    """
    if wants_activities_browse(msg):
        session_ctx["discovery_goal"] = "activities"
    _update_discovery_goal_from_slots(session_ctx, slots)
    stored = str(session_ctx.get("discovery_goal") or "none")
    slot_goal = str(slots.get("goal") or "none").lower()
    conf = float(slots.get("confidence", 0.0))

    if slot_goal in _DISCOVERY_GOALS and conf >= 0.45:
        return slot_goal
    if slot_goal == "continue" and stored in _DISCOVERY_GOALS:
        return stored
    if extract_zip(msg) and stored in _DISCOVERY_GOALS:
        return stored
    return stored if stored in _DISCOVERY_GOALS else slot_goal


def _zip_prompt(discovery_goal: str) -> str:
    if discovery_goal == "activities":
        return (
            "What ZIP code is your block? That helps me find activities near you."
        )
    if discovery_goal == "both":
        return (
            "What ZIP code is your block? That helps me find neighbors and activities near you."
        )
    return "What ZIP code is your block? That helps me find neighbors near you."


def _show_activities_preview(
    *,
    ctx_base: dict[str, Any],
    block_id: str,
    block_label: str,
    msg: str = "",
    phone_verified: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    weekend_only = bool(re.search(r"\bweekend\b", str(msg or ""), re.I))
    events = fetch_preview_events_on_block(block_id, weekend_only=weekend_only)
    reply = format_activities_message(events, block_label, phone_verified=phone_verified)
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_PREVIEW,
        preview_block_id=block_id,
        active_intent=INTENT_FIND_ACTIVITIES,
    )
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "browse_block_activities")
    ctx["activity_previews"] = activity_previews_from_events(events)
    _clear_peer_surface(ctx)
    return reply, ctx, ctx["last_routing"], []


def _looks_like_meta_chat(msg: str) -> bool:
    return bool(_META_CHAT_RE.search(str(msg or "").strip()))


def extract_zip(text: str) -> str | None:
    m = _ZIP_RE.search(str(text or ""))
    return m.group(1) if m else None


def invalid_zip_hint(text: str) -> str | None:
    """Explain bad ZIP attempts instead of repeating the same prompt."""
    s = str(text or "").strip()
    if not s or extract_zip(s):
        return None
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    if len(digits) < 5:
        return (
            f"That looks like {len(digits)} digits — I need a 5-digit US ZIP code "
            "(e.g. 32827 for Lake Nona). What's yours?"
        )
    if len(digits) != 5:
        return (
            "I need a 5-digit US ZIP code only (e.g. 32827), not a longer number. "
            "Which ZIP is your block?"
        )
    return None


def _is_affirmative(msg: str) -> bool:
    lower = str(msg or "").strip().lower().rstrip(".!")
    return lower in _AFFIRMATIVE_REPLIES or any(lower.startswith(f"{a} ") for a in _AFFIRMATIVE_REPLIES)


def _is_peer_find_command(msg: str) -> bool:
    """Short 'find me people' lines are intent, not identity."""
    s = str(msg or "").strip()
    return bool(s) and wants_peer_find(s) and len(s) < 60


def _user_messages_from_history(history: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for turn in history or []:
        if str(turn.get("role") or "") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if content:
            out.append(content)
    return out


def _fallback_identity_snippet(
    msg: str,
    history: list[dict[str, Any]] | None,
    *,
    phase: str,
    block_just_resolved: bool,
    has_block: bool = False,
    slots: dict[str, Any] | None = None,
) -> str | None:
    """
    Funnel fallback when Flash misses identity — reuse what the user already said.
    Runs during need_identity, right after ZIP, or when user demands peers with block set.
    """
    text = str(msg or "").strip()
    if phase == PHASE_NEED_IDENTITY and text:
        if text.lower() in _NOT_IDENTITY_REPLIES:
            return None
        if (
            not extract_zip(text)
            and not wants_login_intent(text)
            and not wants_signup_intent(text)
            and not _looks_like_meta_chat(text)
            and not _is_peer_find_command(text)
        ):
            return text[:400]

    goal = str((slots or {}).get("goal") or "none")
    late_peer_find = (
        goal in ("peers", "both")
        and float((slots or {}).get("confidence", 0.0)) >= 0.45
        and has_block
        and phase in ("listening", PHASE_PREVIEW)
    )
    if not block_just_resolved and phase != PHASE_NEED_IDENTITY and not late_peer_find:
        return None

    long_parts: list[str] = []
    short_parts: list[str] = []
    for content in _user_messages_from_history(history):
        if extract_zip(content) and len(content.strip()) <= 8:
            continue
        if _is_peer_find_command(content):
            continue
        if wants_activities_browse(content):
            continue
        if _looks_like_meta_chat(content) or wants_login_intent(content) or wants_signup_intent(content):
            continue
        stripped = content.strip()
        if len(stripped) >= 12:
            long_parts.append(stripped[:200])
        elif len(stripped) >= 2:
            short_parts.append(stripped[:80])
    if long_parts:
        merged = long_parts[-3:]
        if short_parts and (block_just_resolved or late_peer_find):
            merged = merged + short_parts[-5:]
        return "; ".join(merged)[:400]
    if short_parts and (block_just_resolved or late_peer_find):
        return "; ".join(short_parts[-6:])[:400]
    return None


def _explicit_funnel_input(
    msg: str,
    *,
    slots: dict[str, Any] | None = None,
    session_ctx: dict[str, Any] | None = None,
) -> bool:
    """Code-owned structural signals only — peer-find intent is Flash slots, not regex."""
    if extract_zip(msg) or invalid_zip_hint(msg):
        return True
    if session_ctx and session_ctx.get("pending_signup_gate"):
        return True
    if _turn_wants_signup_gate(msg, slots, session_ctx or {}):
        return True
    if wants_verify_help(msg) or wants_more_peer_detail(msg):
        return True
    if wants_rsvp_intent(msg) or wants_activities_browse(msg):
        return True
    return False


def wants_discovery_turn(
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    slots: dict[str, Any] | None = None,
) -> bool:
    """
    Should discovery code handle this turn?
    Code: explicit funnel signals only (ZIP digits, find-peers phrasing).
    AI: every other message — slots decide discovery vs orchestrator.
    """
    if _turn_wants_login(msg, slots, session_ctx):
        return False

    if session_ctx.get("signal_draft"):
        return True

    if _explicit_funnel_input(msg, slots=slots, session_ctx=session_ctx):
        return True

    phase = str(session_ctx.get("routing_phase") or "")
    if phase in _FUNNEL_PHASES:
        if _turn_wants_login(msg, slots, session_ctx):
            return False
        if _looks_like_meta_chat(msg):
            return False
        return True

    if session_ctx.get("pending_post_verify"):
        if _turn_wants_login(msg, slots, session_ctx):
            return False
        if _looks_like_meta_chat(msg):
            return False
        return True

    if discovery_ai_enabled():
        if slots is None:
            slots = discovery_slots_for_turn(
                session_ctx,
                msg,
                routing_phase=phase or "listening",
                history=history,
                has_block=bool(resolve_block_id(session_ctx, None)),
                has_identity=bool(session_ctx.get("identity_snippet")),
                phone_verified=bool(session_ctx.get("phone_verified")),
            )
        if slots.get("identity_snippet") and phase == PHASE_NEED_IDENTITY:
            return True
        return slots_want_discovery_handling(slots, routing_phase=phase)

    # AI off (dev/tests): minimal legacy fallback
    if phase == PHASE_NEED_IDENTITY:
        s = str(msg or "").strip()
        if (
            s
            and not extract_zip(s)
            and not wants_login_intent(s)
            and not _looks_like_meta_chat(s)
        ):
            return True
    return wants_peer_find(msg) or wants_activities_browse(msg)


def _identity_refinement(
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> str | None:
    """New identity line in preview phase (user refined who they want)."""
    if not slots:
        return None
    raw = slots.get("identity_snippet")
    if not raw:
        return None
    new_sn = str(raw).strip()[:400]
    if not new_sn:
        return None
    stored = str(session_ctx.get("identity_snippet") or "").strip()
    if stored and new_sn.lower() == stored.lower():
        return None
    return new_sn


def _user_nickname(user_id: str | None) -> str | None:
    if not user_id:
        return None
    try:
        res = (
            service_client()
            .table("users")
            .select("nickname, full_name")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not isinstance(row, dict):
            return None
        nick = str(row.get("nickname") or row.get("full_name") or "").strip()
        return nick or None
    except Exception:
        return None


def _try_assign_home_block(
    user_jwt: str,
    *,
    session_ctx: dict[str, Any],
    home_block_id: str | None,
) -> str | None:
    """Persist preview block on user after phone verify (required for vector match)."""
    if home_block_id:
        return home_block_id
    bid = resolve_block_id(session_ctx, None)
    if not bid:
        return None
    payload: dict[str, Any] = {"p_block_id": bid}
    zip5 = session_ctx.get("preview_zip")
    if zip5:
        payload["p_home_zip"] = str(zip5)
    try:
        call_rpc(user_jwt, "assign_home_block", payload)
    except HTTPException:
        pass
    return bid


def _should_skip_preview_refetch(
    *,
    phase: str,
    msg: str,
    goal: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    """After first preview, let orchestrator handle pushback unless explicit refresh."""
    if phase != PHASE_PREVIEW:
        return False
    if is_profile_acknowledgment(msg):
        return True
    if wants_more_peer_detail(msg) or goal in ("verify", "rsvp"):
        return False
    if slots_want_preview_refetch(slots or {}, session_ctx):
        return False
    return True


def resolve_identity_for_turn(
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
    phase: str,
    *,
    block_just_resolved: bool,
    slots: dict[str, Any] | None = None,
) -> str | None:
    stored = str(session_ctx.get("identity_snippet") or "").strip() or None
    if discovery_ai_enabled():
        parsed = slots or discovery_slots_for_turn(
            session_ctx,
            msg,
            routing_phase=PHASE_NEED_IDENTITY if block_just_resolved else phase,
            history=history,
            has_block=True,
            has_identity=bool(stored),
            phone_verified=bool(session_ctx.get("phone_verified")),
        )
        sn = parsed.get("identity_snippet")
        if sn:
            sn_s = str(sn).strip()[:400]
            if sn_s:
                if phase == PHASE_PREVIEW and stored and sn_s.lower() != stored.lower():
                    return sn_s
                if not stored:
                    return sn_s
        if stored:
            return stored
        fallback = _fallback_identity_snippet(
            msg,
            history,
            phase=PHASE_NEED_IDENTITY if block_just_resolved else phase,
            block_just_resolved=block_just_resolved,
            has_block=True,
            slots=slots,
        )
        if fallback:
            return fallback
        return None
    if stored:
        return stored
    fallback = _fallback_identity_snippet(
        msg,
        history,
        phase=phase,
        block_just_resolved=block_just_resolved,
        has_block=bool(resolve_block_id(session_ctx, None)),
        slots=slots,
    )
    if fallback:
        return fallback
    if phase == PHASE_NEED_IDENTITY:
        s = str(msg or "").strip()
        if (
            s
            and s.lower() not in _NOT_IDENTITY_REPLIES
            and not extract_zip(s)
            and not wants_login_intent(s)
            and not _looks_like_meta_chat(s)
            and not _is_peer_find_command(s)
        ):
            return s[:400]
    return None


def _routing_ctx(
    session_ctx: dict[str, Any],
    *,
    phase: str,
    active_intent: str | None = INTENT_FIND_PEERS,
    **extra: Any,
) -> dict[str, Any]:
    out = {
        **session_ctx,
        "unified_mode": True,
        "active_intent": active_intent,
        "routing_phase": phase,
    }
    clear_turn_surfaces(out)
    out.update(extra)
    return out


def _auth_action(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def resolve_block_id(
    session_ctx: dict[str, Any],
    home_block_id: str | None,
) -> str | None:
    if home_block_id:
        return home_block_id
    bid = session_ctx.get("preview_block_id") or session_ctx.get("home_block_id")
    return str(bid) if bid else None


def _resolve_block_id_for_turn(
    *,
    session_ctx: dict[str, Any],
    home_block_id: str | None,
    user_jwt: str,
    phone_verified: bool,
) -> str | None:
    block_id = resolve_block_id(session_ctx, home_block_id)
    if block_id or not phone_verified:
        return block_id
    try:
        summary = fetch_block_summary(user_jwt)
    except Exception:
        return block_id
    bid = summary.get("block_id")
    return str(bid) if bid else block_id


def fetch_blocks_for_zip(user_jwt: str, zip5: str) -> list[dict[str, Any]]:
    try:
        raw = call_rpc(
            user_jwt,
            "get_blocks_near_zip",
            {"p_zip": zip5, "p_cluster_id": "lake-nona", "p_limit": 5},
        )
    except HTTPException as exc:
        detail = str(exc.detail or "").lower()
        if "zip_not_found" in detail or "invalid_zip" in detail:
            return []
        raise
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def fetch_preview_peers_on_block(
    block_id: str,
    *,
    limit: int = 3,
    include_peer_ids: bool = False,
) -> list[dict[str, Any]]:
    """Anonymous-safe preview by default; verified users may get peer_user_id for intros."""
    try:
        sb = service_client()
        users = (
            sb.table("users")
            .select("id, nickname")
            .eq("home_block_id", block_id)
            .limit(15)
            .execute()
        )
        rows = users.data or []
        out: list[dict[str, Any]] = []
        for u in rows[:limit]:
            uid = u.get("id")
            if not uid:
                continue
            claims = (
                sb.table("user_identity_claims")
                .select("label, bucket")
                .eq("user_id", uid)
                .eq("disclosure", "public")
                .is_("dismissed_at", "null")
                .order("confidence", desc=True)
                .limit(1)
                .execute()
            )
            label = "shared interests on your block"
            if claims.data:
                label = str(claims.data[0].get("label") or label)
            nick = str(u.get("nickname") or "").strip() or None
            out.append(
                {
                    "peer_user_id": str(uid) if include_peer_ids else None,
                    "nickname": nick if include_peer_ids else None,
                    "avatar_url": None,
                    "similarity_score": None,
                    "matching_peer_label": label,
                    "matching_peer_concept": None,
                    "has_exact_concept_match": False,
                    "preview": not include_peer_ids,
                }
            )
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def redact_peers_for_preview(peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in peers[:5]:
        out.append(
            {
                **p,
                "peer_user_id": None,
                "nickname": None,
                "avatar_url": None,
                "preview": True,
            }
        )
    return out


def _format_event_when(raw: Any) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%a %b %d").replace(" 0", " ")
    except ValueError:
        return s[:10] if len(s) >= 10 else s


def activity_previews_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in events[:5]:
        if not isinstance(ev, dict):
            continue
        out.append(
            {
                "title": str(ev.get("title") or "Activity"),
                "starts_at": str(ev.get("starts_at") or "") or None,
                "starts_label": _format_event_when(ev.get("starts_at")),
                "venue_name": str(ev.get("venue_name") or "").strip() or None,
                "preview": True,
            }
        )
    return out


def fetch_preview_events_on_block(
    block_id: str,
    *,
    limit: int = 5,
    weekend_only: bool = False,
) -> list[dict[str, Any]]:
    """Upcoming open events on preview block (service role)."""
    try:
        sb = service_client()
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        res = (
            sb.table("events")
            .select("title, starts_at, venue_name, cohort_tags")
            .eq("block_id", block_id)
            .eq("status", "open")
            .gte("starts_at", now_iso)
            .order("starts_at")
            .limit(limit * 3)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
        if weekend_only:
            filtered: list[dict[str, Any]] = []
            for row in rows:
                when = str(row.get("starts_at") or "")
                try:
                    dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
                    if dt.weekday() in (5, 6):
                        filtered.append(row)
                except ValueError:
                    continue
            rows = filtered
        return rows[:limit]
    except Exception:
        return []


def format_activities_message(
    events: list[dict[str, Any]],
    block_label: str | None,
    *,
    phone_verified: bool = False,
) -> str:
    where = block_label or "your block"
    if not events:
        return (
            f"I don't see open activities on {where} in the next couple weeks yet. "
            "You can host something, or tell me what you're looking for."
        )
    lines = [f"Here's what's coming up near {where}:"]
    for ev in events[:5]:
        title = str(ev.get("title") or "Activity")
        venue = str(ev.get("venue_name") or "").strip()
        when = _format_event_when(ev.get("starts_at"))
        line = f"• {title}"
        if venue:
            line += f" at {venue}"
        if when:
            line += f" ({when})"
        lines.append(line)
    if phone_verified:
        lines.append("Want to RSVP to one of these, or should I find neighbors like you?")
    else:
        lines.append("Verify your phone to RSVP — or ask me to find neighbors like you.")
    return "\n".join(lines)


def _match_event_title(events: list[dict[str, Any]], msg: str) -> str | None:
    msg_l = str(msg or "").lower()
    for ev in events:
        title = str(ev.get("title") or "").strip()
        if not title:
            continue
        if title.lower() in msg_l:
            return title
        words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3]
        if len(words) >= 2 and all(w in msg_l for w in words[:2]):
            return title
    return None


def _signup_verify_in_flight(session_ctx: dict[str, Any], phase: str) -> bool:
    """User is mid signup phone/OTP or waiting for JWT to catch up after OTP."""
    return (
        phase in (PHASE_AWAIT_SIGNUP_PHONE, PHASE_AWAIT_SIGNUP_OTP)
        or bool(session_ctx.get("pending_post_verify"))
    )

def _verify_gate_reply(
    *,
    session_ctx: dict[str, Any],
    ctx_base: dict[str, Any],
    block_id: str,
    event_label: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if event_label:
        lead = f"To join {event_label}, verify your phone first — I'll text you a code."
    else:
        lead = "I can see neighbors nearby — to show names and connect you, verify your phone first."
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_AWAIT_SIGNUP_PHONE,
        preview_block_id=block_id,
    )
    ctx["requires_phone_verification"] = True
    ctx["peer_matches"] = []
    return (
        f"{lead} What's your number?",
        ctx,
        _discovery_routing_stub(PHASE_GATE_VERIFY),
        [],
    )


def format_preview_message(
    peers: list[dict[str, Any]],
    block_label: str | None,
    *,
    phone_verified: bool = False,
) -> str:
    where = block_label or "your block"
    if not peers:
        return (
            f"I looked around {where} — no strong matches yet. "
            "Tell me a bit more about yourself, or try a nearby ZIP."
        )
    lines = [f"I found {len(peers)} neighbor{'s' if len(peers) != 1 else ''} near {where}:"]
    for i, p in enumerate(peers[:3], 1):
        label = str(p.get("matching_peer_label") or "shared interests")
        lines.append(f"• Neighbor {i} — {label}")
    if phone_verified:
        lines.append(
            "Tell me more about you for sharper matches — or ask me to introduce you to someone."
        )
    else:
        lines.append(
            "Verify your phone to see names and connect — or tell me more about you for sharper matches."
        )
    return "\n".join(lines)


def _discovery_routing_stub(phase: str, tool: str | None = None) -> dict[str, Any]:
    return {
        "outcome": "T" if tool else "A",
        "intent_class": "discovery",
        "confidence": 1.0,
        "tool_to_call": tool,
        "capture_fired": False,
        "routing_phase": phase,
    }


def _apply_display_name_gate(
    msg: str,
    *,
    user_id: str | None,
    ctx_base: dict[str, Any],
    block_id: str,
    phase: str,
    snippet: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """After identity: collect display name before peer preview."""
    if not snippet or not user_needs_display_name(user_id, ctx_base):
        return None

    if phase == PHASE_NEED_DISPLAY_NAME:
        nick = extract_display_name_reply(msg)
        if nick and user_id:
            persist_profile_patch(user_id, {"nickname": nick})
            ctx_base["display_name_saved"] = True
            return None
        return (
            "I didn't catch that — what should neighbors call you? First name is fine.",
            _routing_ctx(
                ctx_base,
                phase=PHASE_NEED_DISPLAY_NAME,
                active_intent=INTENT_FIND_PEERS,
                preview_block_id=block_id,
            ),
            _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
            [],
        )

    nick = extract_nickname_from_message(msg)
    if nick and user_id:
        persist_profile_patch(user_id, {"nickname": nick})
        ctx_base["display_name_saved"] = True
        return None

    return (
        "Love that — what should neighbors call you? First name is fine.",
        _routing_ctx(
            ctx_base,
            phase=PHASE_NEED_DISPLAY_NAME,
            active_intent=INTENT_FIND_PEERS,
            preview_block_id=block_id,
        ),
        _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
        [],
    )


def _handle_signup_phone_message(
    msg: str,
    session_ctx: dict[str, Any],
    *,
    is_anonymous: bool = True,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Parse phone → await_signup_otp + link_phone_signup, or login OTP if phone exists."""
    phone = extract_phone_e164(msg)
    if not phone:
        return (
            "What's your phone number? I'll text you a code to verify.",
            _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_PHONE),
            _discovery_routing_stub(PHASE_AWAIT_SIGNUP_PHONE),
            [],
        )
    if is_anonymous and phone_has_registered_account(phone):
        ctx = _login_ctx(
            session_ctx,
            guest_step=GUEST_STEP_LOGIN_OTP,
            login_phone=phone,
            requires_login_otp=True,
        )
        ctx.pop("signup_phone", None)
        ctx.pop("pending_signup_gate", None)
        ctx["unified_mode"] = True
        ctx["auth_action"] = _auth_action(
            type="send_login_otp",
            phone=phone,
            verify_type="sms",
        )
        return (
            f"I found your account — I sent a login code to {phone}. Enter it when it arrives.",
            ctx,
            _discovery_routing_stub(GUEST_STEP_LOGIN_OTP),
            [],
        )
    ctx = _routing_ctx(
        session_ctx,
        phase=PHASE_AWAIT_SIGNUP_OTP,
        signup_phone=phone,
    )
    ctx["auth_action"] = _auth_action(
        type="link_phone_signup",
        phone=phone,
        verify_type="phone_change",
    )
    return (
        f"Got it — I sent a 6-digit code to {phone}. Enter it here when it arrives.",
        ctx,
        _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
        [],
    )


def handle_discovery_turn(
    user_message: str,
    *,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    is_anonymous: bool,
    history: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    timer: TurnTimer | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """
    Returns (reply, ctx, routing, peer_matches) or None if not handling this turn.
    Also sets auth_action on ctx when present.
    """
    msg = str(user_message or "").strip()
    phase = str(session_ctx.get("routing_phase") or "")
    active = session_ctx.get("active_intent")

    had_block = bool(resolve_block_id(session_ctx, home_block_id))
    has_profile_photo = bool(user_profile_photo_url(user_id))
    slots: dict[str, Any] = {}
    if discovery_ai_enabled():
        slots = discovery_slots_for_turn(
            session_ctx,
            msg,
            routing_phase=phase or "listening",
            history=history,
            has_block=had_block,
            has_identity=bool(session_ctx.get("identity_snippet")),
            phone_verified=phone_verified,
            has_profile_photo=has_profile_photo,
            timer=timer,
        )

    # If the user asked to sign up while phone is unverified, latch it
    # so the next step that needs ZIP can still switch into the phone-gate UI.
    if not phone_verified and _turn_wants_signup_gate(msg, slots, session_ctx):
        session_ctx["pending_signup_gate"] = True

    # Continue signup verify sub-flow
    if phase == PHASE_AWAIT_SIGNUP_PHONE:
        return _handle_signup_phone_message(msg, session_ctx, is_anonymous=is_anonymous)

    # Sessions stuck on preview with requires_phone_verification (orchestrator lag).
    if (
        not phone_verified
        and session_ctx.get("requires_phone_verification")
        and extract_phone_e164(msg)
    ):
        return _handle_signup_phone_message(msg, session_ctx, is_anonymous=is_anonymous)

    if phase == PHASE_AWAIT_SIGNUP_OTP:
        otp = extract_otp_code(msg)
        phone = str(session_ctx.get("signup_phone") or "")
        if not otp:
            return (
                f"Enter the 6-digit code we sent to {phone or 'your phone'}.",
                _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_OTP, signup_phone=phone or None),
                _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
                [],
            )
        ctx = _routing_ctx(session_ctx, phase=PHASE_PREVIEW, signup_phone=phone)
        ctx["pending_post_verify"] = True
        ctx["requires_phone_verification"] = False
        ctx.pop("pending_signup_gate", None)
        ctx["auth_action"] = _auth_action(
            type="verify_signup_otp",
            phone=phone,
            token=otp,
            verify_type="phone_change",
        )
        return (
            "Perfect — verifying you now. Once you're verified, tell me your first name "
            "and I'll show neighbors you can connect with.",
            ctx,
            _discovery_routing_stub(PHASE_PREVIEW, "verify_signup_otp"),
            [],
        )

    photo_turn = handle_profile_photo_turn(
        msg,
        session_ctx=session_ctx,
        slots=slots,
        user_id=user_id,
        phone_verified=phone_verified,
        is_anonymous=is_anonymous,
    )
    if photo_turn:
        reply, ctx = photo_turn
        ctx["unified_mode"] = True
        return reply, ctx, {"outcome": "A", "intent_class": "profile_photo", "confidence": 1.0}, []

    name_change_turn = _try_awaiting_name_change_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_id=user_id,
        phase=phase,
    )
    if name_change_turn is not None:
        reply, ctx, routing, peers = name_change_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    if discovery_ai_enabled() and slots:
        signal_turn = _try_signal_lane_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
        )
        if signal_turn is not None:
            reply, ctx, routing, peers = signal_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        layer1_turn = _try_layer1_intent_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
            user_id=user_id,
        )
        if layer1_turn is not None:
            reply, ctx, routing, peers = layer1_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        peer_detail_turn = _try_peer_detail_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
        )
        if peer_detail_turn is not None:
            reply, ctx, routing, peers = peer_detail_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        list_turn = _try_list_intros_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
        )
        if list_turn is not None:
            reply, ctx, routing, peers = list_turn
            ctx["unified_mode"] = True
            _clear_peer_surface(ctx)
            return reply, ctx, routing, peers

        block_log_turn = _try_show_block_log_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
        )
        if block_log_turn is not None:
            reply, ctx, routing, peers = block_log_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        if phone_verified and wants_neighbor_intro(msg):
            intro_block = resolve_block_id(session_ctx, home_block_id)
            if intro_block:
                intro_turn = _try_neighbor_intro_turn(
                    msg=msg,
                    session_ctx=session_ctx,
                    ctx_base=dict(session_ctx),
                    user_jwt=user_jwt,
                    block_id=intro_block,
                    phone_verified=phone_verified,
                    goal=str(slots.get("goal") or "none"),
                    slots=slots,
                )
                if intro_turn is not None:
                    reply, ctx, routing, peers = intro_turn
                    ctx["unified_mode"] = True
                    return reply, ctx, routing, peers

    if phase == PHASE_AWAIT_LOGOUT or session_ctx.get("auth_intent") == "logout":
        if wants_cancel_logout(msg):
            nick = _user_nickname(user_id)
            ctx = _exit_logout_ctx(session_ctx)
            ctx["unified_mode"] = True
            lead = f"Understood{', ' + nick if nick else ''}"
            return (
                f"{lead} — you'll stay logged in.",
                ctx,
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
        if wants_logout_intent(msg):
            nick = _user_nickname(user_id)
            farewell = f"Take care{', ' + nick if nick else ''} — signing you out now."
            ctx = _logout_ctx(session_ctx)
            ctx["auth_action"] = _auth_action(type="logout")
            ctx["unified_mode"] = True
            return (
                farewell,
                ctx,
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )

    if _turn_wants_logout(msg, slots, session_ctx):
        if phone_verified or not is_anonymous:
            nick = _user_nickname(user_id)
            farewell = f"Take care{', ' + nick if nick else ''} — signing you out now."
            ctx = _logout_ctx(session_ctx)
            ctx["auth_action"] = _auth_action(type="logout")
            ctx["unified_mode"] = True
            return (
                farewell,
                ctx,
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
        if session_ctx.get("auth_intent") == "login":
            return (
                "No problem — what would you like to do? Find neighbors, plan something, or tell me about yourself.",
                _exit_login_ctx(session_ctx),
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
        return (
            "You're not signed in — nothing to log out of. Ask me to find neighbors or tell me about yourself.",
            _routing_ctx(session_ctx, phase="listening"),
            {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
            [],
        )

    if _signup_verify_in_flight(session_ctx, phase) and _turn_wants_login(msg, slots, session_ctx):
        phone = str(session_ctx.get("signup_phone") or "your phone")
        if phase == PHASE_AWAIT_SIGNUP_OTP:
            return (
                f"You're signing up — enter the 6-digit code I sent to {phone}.",
                _routing_ctx(
                    session_ctx,
                    phase=PHASE_AWAIT_SIGNUP_OTP,
                    signup_phone=session_ctx.get("signup_phone"),
                ),
                _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
                [],
            )
        return (
            "You're in the middle of signing up — what's the phone number for your account?",
            _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_PHONE),
            _discovery_routing_stub(PHASE_AWAIT_SIGNUP_PHONE),
            [],
        )

    # Login delegated to guest_login (maps guest_step from routing or early)
    login_step = session_ctx.get("guest_step") or (
        "await_login_phone"
        if phase == "await_login_phone"
        else "await_login_otp"
        if phase == "await_login_otp"
        else "early_chat"
    )
    if _turn_wants_login(msg, slots, session_ctx):
        if phone_verified and not is_anonymous:
            nick = _user_nickname(user_id)
            label = f" as {nick}" if nick else ""
            ctx = _exit_login_ctx(session_ctx)
            return (
                f"You're already signed in{label}! Ask me to find neighbors, plan something, "
                "or tell me what you're looking for.",
                ctx,
                {"outcome": "A", "intent_class": "auth", "confidence": 1.0},
                [],
            )
        login = handle_guest_login(msg, step=str(login_step), session_ctx=session_ctx)
        if login:
            reply, ctx = login
            if ctx.get("login_otp_token") and ctx.get("login_phone"):
                ctx["auth_action"] = _auth_action(
                    type="verify_login_otp",
                    phone=ctx.get("login_phone"),
                    token=ctx.get("login_otp_token"),
                    verify_type="sms",
                )
            elif ctx.get("requires_login_otp") and ctx.get("login_phone"):
                ctx["auth_action"] = _auth_action(
                    type="send_login_otp",
                    phone=ctx.get("login_phone"),
                    verify_type="sms",
                )
            ctx["unified_mode"] = True
            return reply, ctx, {"outcome": "A", "intent_class": "auth", "confidence": 1.0}, []

    if not wants_discovery_turn(msg, session_ctx, history, slots=slots):
        return None

    if wants_host_activity(msg) and not wants_peer_find(msg) and slots.get("goal") not in (
        "peers",
        "activities",
        "both",
    ):
        return None

    effective_goal = _effective_discovery_goal(msg, session_ctx, slots)
    active = _active_intent_for_goal(effective_goal) or INTENT_FIND_PEERS
    ctx_base = _routing_ctx(
        session_ctx,
        phase=phase or PHASE_NEED_ZIP,
        active_intent=active,
    )
    if effective_goal in _DISCOVERY_GOALS:
        ctx_base["discovery_goal"] = effective_goal

    # Slot: ZIP / block
    block_id = resolve_block_id(session_ctx, home_block_id)
    zip_from_msg = extract_zip(msg) or slots.get("zip")
    if zip_from_msg and not block_id:
        blocks = fetch_blocks_for_zip(user_jwt, zip_from_msg)
        if blocks:
            block_id = str(blocks[0].get("block_id") or "")
            ctx_base["preview_block_id"] = block_id
            ctx_base["preview_zip"] = zip_from_msg
            ctx_base["preview_block_label"] = str(blocks[0].get("label") or blocks[0].get("name") or zip_from_msg)

    if not block_id:
        if zip_from_msg:
            return (
                f"I couldn't find blocks for ZIP {zip_from_msg}. Try another ZIP (e.g. 32827 for Lake Nona).",
                _routing_ctx(
                    session_ctx,
                    phase=PHASE_NEED_ZIP,
                    active_intent=active,
                    discovery_goal=ctx_base.get("discovery_goal"),
                ),
                _discovery_routing_stub(PHASE_NEED_ZIP),
                [],
            )
        zip_hint = invalid_zip_hint(msg)
        zip_goal = str(ctx_base.get("discovery_goal") or effective_goal or "peers")
        return (
            zip_hint or _zip_prompt(zip_goal),
            _routing_ctx(
                session_ctx,
                phase=PHASE_NEED_ZIP,
                active_intent=active,
                discovery_goal=ctx_base.get("discovery_goal"),
            ),
            _discovery_routing_stub(PHASE_NEED_ZIP),
            [],
        )

    block_just_resolved = bool(zip_from_msg and not had_block)
    goal = effective_goal

    # Safety: honor explicit signup intent once the ZIP resolves into a block.
    if not phone_verified and session_ctx.get("pending_signup_gate"):
        session_ctx.pop("pending_signup_gate", None)
        ctx_base.pop("pending_signup_gate", None)
        return _verify_gate_reply(
            session_ctx=session_ctx,
            ctx_base=ctx_base,
            block_id=block_id,
        )

    # Slot: identity snippet (Flash — not chat history heuristics)
    snippet = resolve_identity_for_turn(
        msg,
        ctx_base,
        history,
        phase,
        block_just_resolved=block_just_resolved,
        slots=slots,
    )
    if snippet:
        ctx_base["identity_snippet"] = snippet

    effective_snippet = str(ctx_base.get("identity_snippet") or "").strip() or None

    block_label = str(
        ctx_base.get("preview_block_label")
        or session_ctx.get("preview_block_label")
        or "your block"
    )

    if goal == "activities" or wants_activities_browse(msg):
        return _show_activities_preview(
            ctx_base=ctx_base,
            block_id=block_id,
            block_label=block_label,
            msg=msg,
            phone_verified=phone_verified,
        )

    if not effective_snippet:
        in_funnel = phase in _FUNNEL_PHASES or block_just_resolved
        if not in_funnel:
            if goal not in ("continue", "peers", "both") or not slots.get("in_discovery"):
                return None
        return (
            "Tell me one thing about you — life stage, heritage, or what you're looking for — "
            "so I can match you better.",
            _routing_ctx(
                session_ctx,
                phase=PHASE_NEED_IDENTITY,
                active_intent=active,
                preview_block_id=block_id,
                preview_zip=ctx_base.get("preview_zip"),
                preview_block_label=ctx_base.get("preview_block_label"),
                discovery_goal=ctx_base.get("discovery_goal"),
            ),
            _discovery_routing_stub(PHASE_NEED_IDENTITY),
            [],
        )

    if wants_rsvp_intent(msg) or goal == "rsvp":
        events = fetch_preview_events_on_block(block_id)
        event_title = _match_event_title(events, msg)
        if phone_verified:
            return None
        return _verify_gate_reply(
            session_ctx=session_ctx,
            ctx_base=ctx_base,
            block_id=block_id,
            event_label=f'"{event_title}"' if event_title else "that activity",
        )

    if phase == PHASE_PREVIEW:
        intro_turn = _try_neighbor_intro_turn(
            msg=msg,
            session_ctx=session_ctx,
            ctx_base=ctx_base,
            user_jwt=user_jwt,
            block_id=block_id,
            phone_verified=phone_verified,
            goal=effective_goal,
            slots=slots,
        )
        if intro_turn is not None:
            return intro_turn

    # Post-verify funnel before verify gate — JWT may lag one turn after OTP.
    if ctx_base.get("pending_post_verify") or phase == PHASE_NEED_DISPLAY_NAME:
        if session_ctx.get("awaiting_name_change") or active == "settings.change_name":
            name_turn = _try_awaiting_name_change_turn(
                msg=msg,
                session_ctx=session_ctx,
                user_id=user_id,
                phase=phase,
            )
            if name_turn is not None:
                return name_turn
        snippet = str(
            session_ctx.get("identity_snippet") or ctx_base.get("identity_snippet") or ""
        ).strip()
        nick = extract_display_name_reply(msg) or extract_nickname_from_message(msg)
        if extract_otp_code(msg) and not nick:
            return (
                "I already have that code — use the Verify button in the code box, "
                "then tell me your first name.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_NEED_DISPLAY_NAME,
                    active_intent=INTENT_FIND_PEERS,
                    preview_block_id=block_id,
                    pending_post_verify=True,
                    signup_phone=session_ctx.get("signup_phone"),
                ),
                _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
                [],
            )
        if user_needs_display_name(user_id, ctx_base):
            if nick and user_id:
                persist_profile_patch(user_id, {"nickname": nick})
                ctx_base["display_name_saved"] = True
            elif _is_affirmative(msg) or not nick:
                return (
                    "What should neighbors call you? First name is fine.",
                    _routing_ctx(
                        ctx_base,
                        phase=PHASE_NEED_DISPLAY_NAME,
                        active_intent=INTENT_FIND_PEERS,
                        preview_block_id=block_id,
                        pending_post_verify=True,
                    ),
                    _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME),
                    [],
                )
        if not snippet and (_is_affirmative(msg) or (phase == PHASE_NEED_DISPLAY_NAME and not nick)):
            return (
                "Tell me one thing about you — life stage, heritage, or what you're looking for — "
                "so I can match you better.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_NEED_IDENTITY,
                    active_intent=INTENT_FIND_PEERS,
                    preview_block_id=block_id,
                    pending_post_verify=True,
                ),
                _discovery_routing_stub(PHASE_NEED_IDENTITY),
                [],
            )
        if not phone_verified:
            nick = str(
                (ctx_base.get("display_name_saved") and extract_display_name_reply(msg))
                or extract_nickname_from_message(msg)
                or ""
            ).strip()
            lead = f"Got it{', ' + nick if nick else ''}! "
            return (
                f"{lead}Finishing verification — send one more message and I'll show your matches.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_PREVIEW,
                    active_intent=INTENT_FIND_PEERS,
                    preview_block_id=block_id,
                    pending_post_verify=True,
                ),
                _discovery_routing_stub(PHASE_PREVIEW),
                [],
            )
        _try_assign_home_block(user_jwt, session_ctx=ctx_base, home_block_id=home_block_id)
        try:
            peers = fetch_peer_matches(user_jwt, limit=5)
        except Exception:
            peers = []
        if not peers:
            peers = fetch_preview_peers_on_block(
                block_id,
                limit=3,
                include_peer_ids=phone_verified,
            )
            reply = format_preview_message(peers, block_label, phone_verified=phone_verified)
        else:
            reply = format_peer_matches(peers)
        ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
        ctx.pop("pending_post_verify", None)
        ctx.pop("activity_previews", None)
        identity = str(ctx_base.get("identity_snippet") or session_ctx.get("identity_snippet") or "").strip()
        reply = _maybe_attach_intro_offer(
            reply=reply,
            peers=peers,
            ctx=ctx,
            identity_snippet=identity or None,
            msg=msg,
        )
        ctx["last_routing"] = _discovery_routing_stub(
            PHASE_PREVIEW, "match_peers_by_claim_vectors"
        )
        return reply, ctx, ctx["last_routing"], peers

    if (
        not phone_verified
        and not _signup_verify_in_flight(session_ctx, phase)
        and (wants_verify_help(msg) or goal == "verify" or wants_more_peer_detail(msg))
    ):
        return _verify_gate_reply(
            session_ctx=session_ctx,
            ctx_base=ctx_base,
            block_id=block_id,
        )

    # Preview re-search: AI must supply new identity_snippet + goal=peers (not questions).
    if phase == PHASE_PREVIEW and slots_want_preview_refetch(slots, session_ctx):
        refined = _identity_refinement(slots, session_ctx)
        if refined:
            ctx_base["identity_snippet"] = refined
        if phone_verified:
            _try_assign_home_block(user_jwt, session_ctx=ctx_base, home_block_id=home_block_id)
            try:
                peers = fetch_peer_matches(user_jwt, limit=5)
            except Exception:
                peers = []
            if peers:
                reply = format_peer_matches(peers)
                ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
                ctx.pop("activity_previews", None)
                identity = str(
                    ctx_base.get("identity_snippet") or session_ctx.get("identity_snippet") or ""
                ).strip()
                reply = _maybe_attach_intro_offer(
                    reply=reply,
                    peers=peers,
                    ctx=ctx,
                    identity_snippet=identity or None,
                    msg=msg,
                )
                ctx["last_routing"] = _discovery_routing_stub(
                    PHASE_PREVIEW, "match_peers_by_claim_vectors"
                )
                return reply, ctx, ctx["last_routing"], peers
        peers = fetch_preview_peers_on_block(block_id, limit=3)
        reply = format_preview_message(peers, block_label, phone_verified=phone_verified)
        ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
        ctx.pop("activity_previews", None)
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "preview_peers_on_block")
        return reply, ctx, ctx["last_routing"], peers

    wants_peers = goal in ("peers", "both")
    if not discovery_ai_enabled():
        wants_peers = wants_peers or wants_peer_find(msg)
    if _should_skip_preview_refetch(
        phase=phase,
        msg=msg,
        goal=goal,
        slots=slots,
        session_ctx=session_ctx,
    ):
        return None

    if phase != PHASE_PREVIEW or wants_peers or wants_more_peer_detail(msg):
        effective_home = home_block_id or _try_assign_home_block(
            user_jwt, session_ctx=ctx_base, home_block_id=home_block_id
        )
        if phone_verified and effective_home:
            try:
                peers = fetch_peer_matches(user_jwt, limit=5)
            except Exception:
                peers = []
            if peers:
                reply = format_peer_matches(peers)
                ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
                ctx.pop("activity_previews", None)
                identity = str(
                    ctx_base.get("identity_snippet") or session_ctx.get("identity_snippet") or ""
                ).strip()
                reply = _maybe_attach_intro_offer(
                    reply=reply,
                    peers=peers,
                    ctx=ctx,
                    identity_snippet=identity or None,
                    msg=msg,
                )
                ctx["last_routing"] = _discovery_routing_stub(
                    PHASE_PREVIEW, "match_peers_by_claim_vectors"
                )
                return reply, ctx, ctx["last_routing"], peers

        if wants_peers or phase != PHASE_PREVIEW:
            peers = fetch_preview_peers_on_block(block_id, limit=3)
            reply = format_preview_message(peers, block_label, phone_verified=phone_verified)
            ctx = _routing_ctx(ctx_base, phase=PHASE_PREVIEW, preview_block_id=block_id)
            ctx.pop("activity_previews", None)
            ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "preview_peers_on_block")
            return reply, ctx, ctx["last_routing"], peers

    return None
