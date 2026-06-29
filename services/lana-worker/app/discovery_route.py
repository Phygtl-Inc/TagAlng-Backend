"""Discovery routing: find peers with ZIP → identity → preview → verify gate → full."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from app.auth import (
    email_has_registered_account,
    registered_user_id_for_email,
    service_client,
)
from app.db import (
    extract_host_ctx,
    log_feature_request,
    log_moderation_flag,
    stash_pending_event_draft,
    stash_pending_meet_seek,
)
from app.orchestrator.guardrails import utterance_is_unsafe
from app.claim_search import (
    heritage_terms_in_text,
    parse_claim_filters,
    peer_heritage_key,
    peer_matches_identity_snippet,
)
from app.discovery_slots import (
    ai_parse_discovery_turn,
    discovery_ai_enabled,
    discovery_slots_for_turn,
    slots_indicate_peer_discovery,
    slots_peer_name,
    slots_picking_shown_peer,
    slots_want_discovery_handling,
    slots_want_identity_profile_handling,
    slots_want_login,
    slots_want_logout,
    slots_want_preview_refetch,
    slots_want_propose_intro,
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
from app.hosting_cta import (
    handle_hosting_open_turn,
    handle_hosting_send_mom_turn,
    is_hosting_open_cta,
    is_hosting_send_mom_cta,
    is_hosting_ui_cta,
    session_has_hosting_offer,
    stamp_pending_hosting_offer,
)
from app.intro_proposal import (
    INTENT_PROPOSE_INTRO,
    accepts_intro_offer,
    block_log_peer_from_entry,
    build_match_reason,
    format_intro_offer_reply,
    format_intro_offer_turn,
    format_intro_proposed_reply,
    pick_block_log_entry_for_intro,
    propose_neighbor_intro,
    stamp_intro_offer_ctx,
    stamp_intro_proposal_ctx,
    try_propose_intro_from_preview,
    wants_neighbor_intro,
    pick_peer_for_intro,
    peer_index_from_message,
    requested_peer_name,
)
from app.intro_list import (
    INTENT_LIST_INTROS,
    attach_pending_intros_after_propose,
    clear_intro_offer_ctx,
    fetch_my_intros,
    format_duplicate_intro_reply,
    format_intros_list_reply,
    infer_intro_direction,
    stamp_pending_intros_ctx,
    stamp_duplicate_intro_sent,
    stamp_intro_respond_from_peer,
    wants_list_intros_phrase,
)
from app.local_signals import (
    INTENT_SAVE_SIGNAL,
    INTENT_SHOW_BLOCK_LOG,
    block_log_match_summary,
    block_log_take_action,
    fetch_my_block_log,
    filter_block_log_for_signal,
    format_block_log_reply,
    format_signal_saved_reply,
    normalize_block_log_row,
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
    extract_email,
    handle_guest_login,
    wants_cancel_logout,
    wants_login as wants_login_intent,
    wants_logout as wants_logout_intent,
)
from app.claims_persist import (
    claim_from_pending,
    extract_display_name_reply,
    extract_nickname_from_message,
    heritage_conflict_prompt,
    is_explicit_heritage_correction,
    message_might_assert_heritage,
    persist_profile_patch,
    scrub_negative_heritage_claims,
    try_upsert_claims_from_message,
    upsert_claims,
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
    fetch_peer_profile,
    format_attr_peers_reply,
    format_block_summary_reply,
    format_identity_profile_reply,
    format_match_list_explanation,
    format_peer_detail_reply,
    format_peer_match_explanation,
    format_peer_profile_reply,
    handle_add_or_edit_claim,
    handle_change_name,
    handle_notification_prefs,
    peers_to_match_rows,
    summarize_partial_claim_matches,
    stamp_identity_profile_ctx,
)
from app.layer1_intents import (
    LOOKING_SHARING_INTENTS,
    enrich_slots,
    intent_confidence_met,
    is_block_activity_browse,
    is_profile_acknowledgment,
    normalize_attr_filter_text,
    phrase_linear_intent,
    PHRASE_POLICY_OVERRIDES,
    slots_indicate_hosting_signal,
    slots_indicate_tip_share_signal,
    slots_linear_intent,
    utterance_indicates_tip_share,
    utterance_indicates_tip_seek,
    utterance_indicates_swap_seek,
)
from app.layer1_tier import handle_respond_nudge, parse_nudge_response, wants_respond_intro
from app.signal_capture import (
    PHASE_SIGNAL_CONFIRM,
    PHASE_SIGNAL_EXTRACT,
    PHASE_SIGNAL_LISTENING,
    advance_signal_draft,
    clear_signal_draft,
    draft_from_slots,
    is_signal_cancel,
    is_signal_lane_intent,
    should_abandon_signal_draft,
    should_abort_signal_draft,
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

# Cap consecutive unparsed name replies so the user is never trapped re-answering
# "what should I call you?" — mirrors the event-host turn cap below.
NAME_CHANGE_MAX_ATTEMPTS = 2

_MORE_DETAIL_RE = re.compile(
    r"\b(?:(?:show|see)\s+(?:me\s+)?(?:their\s+)?names?|"
    r"introduce|connect(?:\s+me)?|who are they|"
    r"details?\s+(?:on|about|for)\s+(?:neighbor|them|#?\d)|"
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
_PEER_TRAIT_QUESTION_RE = re.compile(
    r"\b(?:is\s+(?:she|he|they|that|this|it)|are\s+(?:they|those|these)|"
    r"does\s+(?:she|he|they))\b",
    re.I,
)
_ATTR_REFINE_RE = re.compile(
    r"\b(?:no|nope|not that)[,.\s!]*(?:(?:i\s+)?want|show\s+me|find|looking\s+for)\s+(.+)",
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


def _session_peer_matches_name(session_ctx: dict[str, Any], name: str) -> bool:
    requested = str(name or "").strip().lower()
    if not requested:
        return False
    stored = session_ctx.get("peer_matches")
    if not isinstance(stored, list):
        return False
    for row in stored:
        if not isinstance(row, dict):
            continue
        nick = str(row.get("nickname") or "").strip().lower()
        if nick and (nick == requested or requested in nick or nick in requested):
            return True
    return False


def fetch_neighbors_on_block_by_nickname(
    block_id: str,
    name: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Same-block lookup by first name — not limited to identity-vector matches."""
    needle = str(name or "").strip().lower()
    if not block_id or not needle:
        return []
    try:
        sb = service_client()
        users = (
            sb.table("users")
            .select("id, nickname")
            .eq("home_block_id", block_id)
            .ilike("nickname", f"%{needle}%")
            .limit(max(limit, 1) * 3)
            .execute()
        )
        out: list[dict[str, Any]] = []
        for row in users.data or []:
            uid = row.get("id")
            nick = str(row.get("nickname") or "").strip()
            if not uid or not nick:
                continue
            low = nick.lower()
            if not (low == needle or needle in low or low in needle):
                continue
            out.append(
                {
                    "peer_user_id": str(uid),
                    "nickname": nick,
                    "avatar_url": None,
                    "similarity_score": None,
                    "matching_peer_label": "on your block",
                    "matching_peer_concept": None,
                    "has_exact_concept_match": False,
                    "preview": False,
                }
            )
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _try_identity_slots_turn(
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
    """AI slots → identity profile / add claim / named neighbor lookup (not regex)."""
    if not discovery_ai_enabled() or not slots_want_identity_profile_handling(slots):
        return None
    return _try_layer1_intent_turn(
        msg=msg,
        slots=enrich_slots(dict(slots), msg=msg),
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        phase=phase,
        user_id=user_id,
    )


def _try_phrase_policy_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
    user_id: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Run phrase overrides before broad regex handlers (respond-nudge, list-intros)."""
    phrase = phrase_linear_intent(msg)
    if phrase not in PHRASE_POLICY_OVERRIDES:
        return None
    slots = enrich_slots({"linear_intent": phrase, "confidence": 0.95}, msg=msg)
    return _try_layer1_intent_turn(
        msg=msg,
        slots=slots,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        phase=phase,
        user_id=user_id,
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


# AI slot pivots that must not re-fire propose_intro from stale session cards.
_SLOTS_BLOCK_PROPOSE_INTRO = frozenset({
    "discovery.find_in_block",
    "discovery.block_log",
    "discovery.find_activities",
    "discovery.find_peers",
    "discovery.find_by_attrs",
    "social.list_intros",
    "looking.swap",
    "looking.meet",
    "looking.tip",
    "sharing.swap",
    "sharing.host",
    "sharing.tip",
})
_SLOTS_BLOCK_PROPOSE_GOALS = frozenset({
    "peers",
    "activities",
    "list_intros",
    "save_signal",
    "show_block_log",
    "chat",
})


def _ai_slots_block_propose_intro(msg: str, slots: dict[str, Any] | None) -> bool:
    """True when Flash classified a non-intro turn — skip stale intro re-propose."""
    if not slots:
        return False
    enriched = enrich_slots(dict(slots), msg=msg)
    if slots_want_propose_intro(enriched):
        return False
    linear = slots_linear_intent(enriched)
    if linear and linear in _SLOTS_BLOCK_PROPOSE_INTRO and intent_confidence_met(enriched, linear):
        return True
    goal = str(enriched.get("goal") or "").lower()
    conf = float(enriched.get("confidence", 0.0))
    return conf >= 0.5 and goal in _SLOTS_BLOCK_PROPOSE_GOALS


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


def _message_names_shown_peer(msg: str, peers: list[dict[str, Any]]) -> bool:
    """True when utterance names someone on the current peer card list."""
    lower = str(msg or "").lower()
    for p in peers:
        if not isinstance(p, dict):
            continue
        nick = str(p.get("nickname") or "").strip().lower()
        if nick and len(nick) > 2 and nick in lower:
            return True
    return False


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
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if not phone_verified:
        return None
    if _ai_slots_block_propose_intro(msg, slots):
        return None
    intro_source = str((slots or {}).get("intro_source") or "").strip().lower()
    if _intro_should_use_block_log(msg, session_ctx, history) and intro_source != "peer_preview":
        return None
    pending = session_ctx.get("pending_intro_offer")
    slot_peer = slots_peer_name(slots)
    wants_intro = goal == "propose_intro"
    if not wants_intro and slots and slots_want_propose_intro(slots):
        wants_intro = True
    if not wants_intro and slots and slots_picking_shown_peer(slots, session_ctx):
        wants_intro = True
    if not wants_intro and pending and accepts_intro_offer(msg):
        wants_intro = True
    if not wants_intro and discovery_ai_enabled():
        return None
    if not wants_intro and wants_neighbor_intro(msg):
        wants_intro = True
    if not wants_intro:
        return None

    peers = _preview_peers_with_ids(
        user_jwt=user_jwt,
        session_ctx=session_ctx,
        block_id=block_id,
        phone_verified=phone_verified,
        home_block_id=ctx_base.get("home_block_id"),
    )
    intro_source = str((slots or {}).get("intro_source") or "").strip().lower()
    if intro_source == "peer_preview" or slots_picking_shown_peer(slots, session_ctx):
        stored = session_ctx.get("peer_matches")
        if isinstance(stored, list):
            session_peers = [
                p for p in stored if isinstance(p, dict) and p.get("peer_user_id")
            ]
            if session_peers:
                peers = session_peers
    if slot_peer or _message_names_shown_peer(msg, peers):
        ctx_base = dict(ctx_base)
        clear_intro_offer_ctx(ctx_base)
        pending = None
        session_ctx = ctx_base
    identity = str(ctx_base.get("identity_snippet") or session_ctx.get("identity_snippet") or "").strip()
    requested = slot_peer or requested_peer_name(msg)
    if requested and isinstance(pending, dict):
        pend_nick = str(pending.get("candidate_nickname") or "").lower()
        if pend_nick and requested not in pend_nick and pend_nick not in requested:
            ctx_base = dict(ctx_base)
            clear_intro_offer_ctx(ctx_base)
            pending = None
            session_ctx = ctx_base
    requested = slot_peer or requested_peer_name(msg)
    if requested and block_id and not _session_peer_matches_name({"peer_matches": peers}, requested):
        block_hits = fetch_neighbors_on_block_by_nickname(block_id, requested)
        if block_hits:
            hit_id = str(block_hits[0].get("peer_user_id") or "")
            peers = block_hits + [
                p for p in peers if str(p.get("peer_user_id") or "") != hit_id
            ]
    slot_force = bool(
        slots
        and (
            slots_want_propose_intro(slots)
            or slots_picking_shown_peer(slots, session_ctx)
        )
    )
    result = try_propose_intro_from_preview(
        msg=msg,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        peers=peers,
        identity_snippet=identity or None,
        force=slot_force,
        peer_name=slot_peer,
        list_index=(slots or {}).get("intro_list_index") if isinstance(slots, dict) else None,
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
        requested = slot_peer or requested_peer_name(msg)
        if wants_intro and requested and any(p.get("peer_user_id") for p in peers):
            known = [
                str(p.get("nickname") or p.get("matching_peer_label") or "").strip()
                for p in peers
                if isinstance(p, dict)
            ]
            known = [n for n in known if n]
            hint = f" I see: {', '.join(known[:5])}." if known else ""
            return (
                f"I don't see {requested.title()} in your neighbor matches.{hint} "
                "Try a first name from the list, or say neighbor 1.",
                _routing_ctx(
                    ctx_base,
                    phase=PHASE_PREVIEW,
                    preview_block_id=block_id,
                    active_intent=INTENT_PROPOSE_INTRO,
                ),
                _discovery_routing_stub(PHASE_PREVIEW, "intro_name_not_found"),
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
        attach_pending_intros_after_propose(
            ctx, user_jwt=user_jwt, intro=intro, peer=peer
        )
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "lana_propose_neighbor_intro")
    else:
        if str(intro.get("status") or "") == "duplicate":
            peer_for_respond = selected_peer or {
                "peer_user_id": intro.get("candidate_user_id"),
                "nickname": "that neighbor",
                "matching_peer_label": "",
            }
            if not stamp_intro_respond_from_peer(
                ctx, user_jwt=user_jwt, peer=peer_for_respond
            ):
                stamp_duplicate_intro_sent(
                    ctx,
                    peer=peer_for_respond,
                    match_reason=str(
                        (selected_peer or {}).get("matching_peer_label") or ""
                    ).strip(),
                )
        else:
            ctx.pop("recent_intro_duplicate", None)
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, str(intro.get("status") or "intro_skipped"))
    ctx.pop("activity_previews", None)
    if selected_peer and str(intro.get("status") or "") != "duplicate":
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
    if str(ctx.get("active_intent") or "") in (INTENT_SHOW_BLOCK_LOG, "discovery.block_log"):
        return reply
    if ctx.get("intro_offer_shown") or ctx.get("intro_proposal") or ctx.get("pending_intro_offer"):
        return reply
    if msg and (is_profile_acknowledgment(msg) or (_is_affirmative(msg) and not wants_peer_find(msg))):
        return reply
    active = str(ctx.get("active_intent") or "")
    if active == "discovery.find_by_attrs" and msg and not _ATTR_REFINE_RE.search(str(msg)):
        return reply
    peer = next((p for p in peers if p.get("peer_user_id")), None)
    if not peer:
        return reply
    if not peer_matches_identity_snippet(peer, identity_snippet):
        return reply
    reason = build_match_reason(identity_snippet=identity_snippet, peer=peer)
    stamp_intro_offer_ctx(ctx, peer=peer, match_reason=reason)
    ctx["peer_matches"] = [dict(peer)]
    ctx["intro_offer_shown"] = True
    return format_intro_offer_turn(peer, reason)


def _confirms_heritage_change(msg: str, pending: dict[str, Any]) -> bool:
    low = str(msg or "").strip().lower()
    if not low:
        return False
    label = str(pending.get("label") or "").strip().lower()
    if label and label in low:
        return True
    return low in {"yes", "yeah", "yep", "sure", "please", "do it", "change it", "update it"}


def _try_change_name_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if phrase_linear_intent(msg) != "settings.change_name":
        return None
    reply, nick = handle_change_name(user_id, msg)
    if nick:
        # Success — release the flow so the next turn classifies fresh.
        ctx = _routing_ctx(dict(session_ctx), phase="listening", active_intent=None)
        ctx["display_name_saved"] = True
        ctx["nickname"] = nick
        ctx.pop("awaiting_name_change", None)
        ctx.pop("name_change_attempts", None)
    else:
        # Couldn't parse a name — await one (the continuation handler caps retries).
        ctx = _routing_ctx(
            dict(session_ctx),
            phase=PHASE_NEED_DISPLAY_NAME,
            active_intent="settings.change_name",
        )
        ctx["awaiting_name_change"] = True
    ctx["last_routing"] = _discovery_routing_stub(ctx["routing_phase"], "change_display_name")
    return reply, ctx, ctx["last_routing"], []


def _message_bypasses_heritage_pending(
    msg: str,
    slots: dict[str, Any] | None = None,
) -> bool:
    """Social/settings/discovery turns must not be trapped in heritage yes/no."""
    enriched = enrich_slots(dict(slots or {}), msg=msg) if slots else {}
    if is_signal_lane_intent(enriched):
        linear = slots_linear_intent(enriched)
        if linear and intent_confidence_met(enriched, linear):
            return True
    if str(enriched.get("goal") or (slots or {}).get("goal") or "") == "save_signal":
        return True
    if utterance_indicates_tip_seek(msg) or utterance_indicates_swap_seek(msg):
        return True
    if slots_indicate_peer_discovery(slots):
        return True
    if wants_neighbor_intro(msg) or wants_list_intros_phrase(msg):
        return True
    if phrase_linear_intent(msg) == "settings.change_name":
        return True
    linear = slots_linear_intent(slots) if slots else None
    if linear in (
        "social.propose_intro",
        "social.list_intros",
        "settings.change_name",
        "tier.send_nudge",
        "tier.respond_nudge",
        "discovery.find_peers",
        "discovery.find_by_attrs",
        "discovery.find_in_block",
        "discovery.find_activities",
        "discovery.explain_peer_match",
    ):
        return True
    goal = str((slots or {}).get("goal") or "")
    if goal in ("propose_intro", "list_intros", "peers", "both", "activities"):
        return True
    if str((slots or {}).get("attr_filter") or "").strip():
        return True
    return False


def _try_pending_heritage_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_id: str | None,
    user_jwt: str,
    phone_verified: bool,
    phase: str,
    slots: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    pending = session_ctx.get("pending_heritage_change")
    if not pending or not user_id:
        return None
    if _message_bypasses_heritage_pending(msg, slots):
        return None

    ctx_base = dict(session_ctx)
    from_label = str(pending.get("from_label") or "your prior heritage")
    to_label = str(pending.get("label") or "that")

    if _confirms_heritage_change(msg, pending) or _is_affirmative(msg):
        claim = claim_from_pending(pending)
        upsert_claims(user_id, [claim])
        ctx_base.pop("pending_heritage_change", None)
        ctx_base.pop("skip_heritage_background_extract", None)
        reply = f"Got it — I'll show your heritage as {to_label} from here."
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="identity.add_claim",
        )
        if phone_verified:
            try:
                dashboard = fetch_identity_dashboard(user_jwt)
                stamp_identity_profile_ctx(ctx, dashboard)
            except HTTPException:
                pass
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "heritage_confirmed")
        return reply, ctx, ctx["last_routing"], []

    if _is_negative(msg) or re.search(r"\bkeep\b", msg, re.I):
        ctx_base.pop("pending_heritage_change", None)
        ctx_base.pop("skip_heritage_background_extract", None)
        reply = f"No problem — I'll keep {from_label} on your profile."
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="identity.add_claim",
        )
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "heritage_kept")
        return reply, ctx, ctx["last_routing"], []

    pending_claim = claim_from_pending(pending)
    reply = heritage_conflict_prompt(from_label, pending_claim)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or "listening",
        active_intent="identity.add_claim",
    )
    ctx["pending_heritage_change"] = pending
    ctx["skip_heritage_background_extract"] = True
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "heritage_confirm_pending")
    return reply, ctx, ctx["last_routing"], []


def _try_heritage_conflict_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_id: str | None,
    user_jwt: str,
    phone_verified: bool,
    phase: str,
    slots: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if session_ctx.get("pending_heritage_change") or not user_id:
        return None
    if _message_bypasses_heritage_pending(msg, slots):
        return None
    if slots_indicate_peer_discovery(slots):
        return None
    if is_explicit_heritage_correction(msg):
        return None
    if not message_might_assert_heritage(msg):
        return None

    result = try_upsert_claims_from_message(user_id, msg)
    if result.heritage_conflict:
        ctx_base = dict(session_ctx)
        pending = result.heritage_conflict
        pending_claim = claim_from_pending(pending)
        from_label = str(pending.get("from_label") or "your prior heritage")
        reply = heritage_conflict_prompt(from_label, pending_claim)
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent="identity.add_claim",
        )
        ctx["pending_heritage_change"] = pending
        ctx["skip_heritage_background_extract"] = True
        ctx["skip_claims_background_extract"] = True
        ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "heritage_confirm_ask")
        return reply, ctx, ctx["last_routing"], []
    if result.saved > 0:
        session_ctx["skip_claims_background_extract"] = True
    return None


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
    if nick:
        # Success — release the flow (neutral phase, cleared intent) so the next
        # turn is classified fresh instead of re-entering the name gate.
        ctx = _routing_ctx(session_ctx, phase="listening", active_intent=None)
        ctx["display_name_saved"] = True
        ctx["nickname"] = nick
        ctx.pop("awaiting_name_change", None)
        ctx.pop("name_change_attempts", None)
        ctx["last_routing"] = _discovery_routing_stub("listening", "update_user_name")
        return reply, ctx, ctx["last_routing"], []

    # No name parsed — cap retries so a non-name reply can't trap the user here.
    attempts = int(session_ctx.get("name_change_attempts") or 0) + 1
    if attempts >= NAME_CHANGE_MAX_ATTEMPTS:
        ctx = _routing_ctx(session_ctx, phase="listening", active_intent=None)
        ctx.pop("awaiting_name_change", None)
        ctx.pop("name_change_attempts", None)
        ctx["last_routing"] = _discovery_routing_stub("listening", "update_user_name")
        return (
            "No problem — I'll keep your current name. What would you like to do next?",
            ctx,
            ctx["last_routing"],
            [],
        )
    ctx = _routing_ctx(
        session_ctx,
        phase=PHASE_NEED_DISPLAY_NAME,
        active_intent="settings.change_name",
    )
    ctx["awaiting_name_change"] = True
    ctx["name_change_attempts"] = attempts
    ctx["last_routing"] = _discovery_routing_stub(PHASE_NEED_DISPLAY_NAME, "update_user_name")
    return reply, ctx, ctx["last_routing"], []


def _try_dismiss_intro_pass_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Pass on intro offer or duplicate ack — not tier.respond_nudge (received inbox)."""
    if not phone_verified:
        return None
    if session_ctx.get("pending_intro_respond"):
        return None
    if parse_nudge_response(msg) != "decline":
        return None
    has_offer = isinstance(session_ctx.get("pending_intro_offer"), dict)
    has_dup = isinstance(session_ctx.get("recent_intro_duplicate"), dict)
    if not has_offer and not has_dup:
        return None
    ctx = _routing_ctx(
        dict(session_ctx),
        phase=phase or "listening",
        active_intent=str(session_ctx.get("active_intent") or "listening"),
    )
    clear_intro_offer_ctx(ctx)
    ctx["recent_intro_duplicate"] = None
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "intro_pass_dismissed")
    return (
        "No problem — just tell me when you're ready.",
        ctx,
        ctx["last_routing"],
        [],
    )


def _try_respond_nudge_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Accept/decline/block a pending received intro before propose-intro routing."""
    if phrase_linear_intent(msg) in (
        "identity.show_my_profile",
        "discovery.show_peer_profile",
    ):
        return None
    if not phone_verified or not wants_respond_intro(msg):
        return None
    reply, pending, _action = handle_respond_nudge(
        msg, user_jwt=user_jwt, session_ctx=session_ctx
    )
    ctx = _routing_ctx(
        dict(session_ctx),
        phase=phase or PHASE_PREVIEW,
        active_intent="tier.respond_nudge",
    )
    if pending:
        ctx["pending_intro_respond"] = pending
    else:
        ctx["pending_intro_respond"] = None
        ctx["pending_intros"] = None
        if str(ctx.get("active_intent") or "") == "tier.respond_nudge":
            ctx.pop("active_intent", None)
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "respond_nudge")
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
                "Verify your email first — then I can show your full profile and claims.",
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

    if linear in ("discovery.show_peer_profile", "discovery.explain_peer_match"):
        if not phone_verified:
            return (
                "Verify your email first — then I can show neighbor profiles and explain matches.",
                _routing_ctx(
                    ctx_base,
                    phase=phase or "listening",
                    active_intent=linear,
                ),
                _discovery_routing_stub(phase or "listening", "peer_profile_need_verify"),
                [],
            )
        block_id = _resolve_block_id_for_turn(
            session_ctx=session_ctx,
            home_block_id=home_block_id,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
        )
        peers = _preview_peers_with_ids(
            user_jwt=user_jwt,
            session_ctx=session_ctx,
            block_id=block_id or "",
            phone_verified=phone_verified,
            home_block_id=home_block_id,
        )
        peer_hint = str(slots.get("peer_name") or "").strip() or msg
        selected = pick_peer_for_intro(peers, msg=peer_hint) if peers else None
        if not selected and block_id:
            named = requested_peer_name(peer_hint) or requested_peer_name(msg)
            if named:
                block_hits = fetch_neighbors_on_block_by_nickname(block_id, named)
                if block_hits:
                    selected = block_hits[0]
                    peers = block_hits + [p for p in peers if p is not selected]
        identity = str(
            session_ctx.get("identity_snippet") or ctx_base.get("identity_snippet") or ""
        ).strip() or None
        if linear == "discovery.explain_peer_match":
            if selected:
                reply = format_peer_match_explanation(selected, identity_snippet=identity)
            else:
                reply = format_match_list_explanation(peers, identity_snippet=identity)
            ctx = _routing_ctx(
                ctx_base,
                phase=phase or PHASE_PREVIEW,
                active_intent="discovery.explain_peer_match",
                preview_block_id=block_id,
            )
            if peers:
                ctx["peer_matches"] = peers_to_match_rows(peers, phone_verified=phone_verified)
            ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "explain_peer_match")
            return reply, ctx, ctx["last_routing"], ctx.get("peer_matches") or []

        if not selected:
            named = requested_peer_name(peer_hint) or requested_peer_name(msg)
            if named:
                known = [
                    str(p.get("nickname") or p.get("matching_peer_label") or "").strip()
                    for p in peers
                    if isinstance(p, dict)
                ]
                known = [n for n in known if n]
                hint = f" Identity matches: {', '.join(known[:5])}." if known else ""
                reply = (
                    f"I don't see anyone named {named.title()} on your block.{hint} "
                    "They might use a different display name or be on another block."
                )
            else:
                reply = "Which neighbor — say their name or a number from the list?"
            return (
                reply,
                _routing_ctx(
                    ctx_base,
                    phase=phase or PHASE_PREVIEW,
                    active_intent="discovery.show_peer_profile",
                    preview_block_id=block_id,
                ),
                _discovery_routing_stub(PHASE_PREVIEW, "peer_profile_need_name"),
                peers_to_match_rows(peers, phone_verified=phone_verified) if peers else [],
            )
        peer_id = str(selected.get("peer_user_id") or "").strip()
        if not peer_id:
            return (
                format_peer_detail_reply(
                    selected,
                    index=next(
                        (i for i, p in enumerate(peers) if p is selected),
                        None,
                    ),
                ),
                _routing_ctx(
                    ctx_base,
                    phase=phase or PHASE_PREVIEW,
                    active_intent="discovery.show_peer_profile",
                    preview_block_id=block_id,
                ),
                _discovery_routing_stub(PHASE_PREVIEW, "peer_profile_preview_only"),
                peers_to_match_rows([selected], phone_verified=phone_verified),
            )
        try:
            profile = fetch_peer_profile(user_jwt, peer_id)
        except HTTPException:
            profile = {}
        match_label = str(selected.get("matching_peer_label") or "").strip() or None
        reply = format_peer_profile_reply(profile, match_label=match_label)
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or PHASE_PREVIEW,
            active_intent="discovery.show_peer_profile",
            preview_block_id=block_id,
        )
        ctx["peer_matches"] = peers_to_match_rows([selected], phone_verified=phone_verified)
        ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "get_peer_profile")
        return reply, ctx, ctx["last_routing"], ctx["peer_matches"]

    if linear in ("identity.add_claim", "identity.edit_claim"):
        if wants_neighbor_intro(msg) or wants_list_intros_phrase(msg):
            return None
        reply, saved, pending = handle_add_or_edit_claim(
            user_id, msg, linear_intent=linear
        )
        ctx = _routing_ctx(
            ctx_base,
            phase=phase or "listening",
            active_intent=linear,
        )
        if pending:
            ctx["pending_heritage_change"] = pending
            ctx["skip_heritage_background_extract"] = True
            ctx["skip_claims_background_extract"] = True
        elif saved > 0:
            ctx.pop("pending_heritage_change", None)
            ctx.pop("skip_heritage_background_extract", None)
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
                "Verify your email first — then I can show your block log.",
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
        # Resolve a ZIP the user just typed into a block. This handler runs BEFORE the
        # main ZIP funnel and short-circuits it, so without resolving the ZIP here a guest
        # who answers "what's on my block?" with their ZIP just gets re-asked "what ZIP?"
        # every turn (the message ZIP is never read).
        if not block_id:
            zip_from_msg = extract_zip(msg) or slots.get("zip")
            if zip_from_msg:
                blocks = fetch_blocks_for_zip(user_jwt, zip_from_msg)
                if blocks:
                    block_id = str(blocks[0].get("block_id") or "")
                    ctx_base["preview_block_id"] = block_id
                    ctx_base["preview_zip"] = zip_from_msg
                    ctx_base["preview_block_label"] = str(
                        blocks[0].get("label") or blocks[0].get("name") or zip_from_msg
                    )
                elif not phone_verified:
                    return (
                        f"I couldn't find blocks for ZIP {zip_from_msg}. Try another ZIP "
                        "(e.g. 32827 for Lake Nona).",
                        _routing_ctx(
                            ctx_base,
                            phase=PHASE_NEED_ZIP,
                            active_intent="discovery.find_in_block",
                        ),
                        _discovery_routing_stub(PHASE_NEED_ZIP, "block_summary_zip_not_found"),
                        [],
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
        if slots_indicate_hosting_signal(slots):
            return None
        # At the identity onboarding step, "find by attrs" is really the user
        # answering "tell me about you" with their own traits — let it fall through
        # to identity capture (which persists the claims and matches off the snippet)
        # instead of a literal neighbor search that bounces "no matching neighbors".
        if (phase or "") == PHASE_NEED_IDENTITY:
            return None
        if not phone_verified:
            return (
                "Verify your email first — then I can search neighbors by those traits.",
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
        ctx["skip_claims_background_extract"] = True
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
        if nick:
            # Success — release the flow so the next turn classifies fresh.
            ctx = _routing_ctx(ctx_base, phase="listening", active_intent=None)
            ctx["display_name_saved"] = True
            ctx["nickname"] = nick
            ctx.pop("awaiting_name_change", None)
            ctx.pop("name_change_attempts", None)
        else:
            ctx = _routing_ctx(
                ctx_base,
                phase=PHASE_NEED_DISPLAY_NAME,
                active_intent="settings.change_name",
            )
            ctx["awaiting_name_change"] = True
        ctx["last_routing"] = _discovery_routing_stub(ctx["routing_phase"], "update_user_name")
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
            ctx["pending_intro_respond"] = None
            ctx["pending_intros"] = None
            if str(ctx.get("active_intent") or "") == "tier.respond_nudge":
                ctx.pop("active_intent", None)
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
    if _wants_block_log(msg, slots or {}):
        clear_signal_draft(ctx_base)
        return None
    if slots and (
        str(slots.get("goal") or "") == "save_signal" or is_signal_lane_intent(slots)
    ):
        session_ctx.pop("discovery_goal", None)
        ctx_base.pop("discovery_goal", None)
    draft = ctx_base.get("signal_draft")
    confirming = (
        isinstance(draft, dict)
        and str(draft.get("phase") or "") == PHASE_SIGNAL_CONFIRM
        and draft.get("confirm_field")
    )
    # Confirm phase: the AI reads the reply and decides answer vs cancel vs reroute.
    # cancel/reroute (or a confident pivot from the main classifier) escape the cascade,
    # so a reply that isn't an answer is never force-filled into the slot.
    confirm_verdict: dict[str, Any] | None = None
    if isinstance(draft, dict) and confirming:
        from app.signal_confirm_ai import interpret_signal_confirm_reply

        confirm_verdict = interpret_signal_confirm_reply(draft, msg)
        verdict = str((confirm_verdict or {}).get("verdict") or "")
        cancel = verdict == "cancel" or (confirm_verdict is None and is_signal_cancel(msg))
        reroute = verdict == "reroute" or should_abort_signal_draft(msg, draft, slots)
        if cancel or reroute:
            clear_signal_draft(ctx_base)
            # Persist the clear even if a different handler wins this re-routed turn.
            session_ctx["signal_draft"] = None
            confirm_verdict = None
            if cancel:
                return (
                    "No problem — I've dropped that. What would you like to do instead?",
                    _routing_ctx(ctx_base, phase="listening", active_intent="none"),
                    _discovery_routing_stub("listening", "signal_draft_cancelled"),
                    [],
                )
            draft = None
            confirming = False
    if isinstance(draft, dict) and not confirming and should_abandon_signal_draft(msg, draft, slots):
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
                "Verify your email first — then I can post that to your block.",
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
        updated, prompt, ready = advance_signal_draft(draft, msg=msg, ai_verdict=confirm_verdict)
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
                "signal_when_hint": updated.get("when_hint"),
                "signal_where_hint": updated.get("where_hint"),
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

    if not slots:
        return None
    goal = str(slots.get("goal") or "none")
    if not is_signal_lane_intent(slots) and goal != "save_signal":
        return None
    if linear and not intent_confidence_met(slots, linear):
        if goal != "save_signal" or float(slots.get("confidence", 0.0)) < 0.5:
            return None
    elif goal == "save_signal":
        if float(slots.get("confidence", 0.0)) < 0.5:
            return None
    else:
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
            "signal_when_hint": updated.get("when_hint"),
            "signal_where_hint": updated.get("where_hint"),
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


def _session_preview_peers(session_ctx: dict[str, Any]) -> list[dict[str, Any]]:
    peers = session_ctx.get("peer_matches")
    if isinstance(peers, list):
        return [p for p in peers if isinstance(p, dict)]
    return []


def _try_peer_trait_question_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Answer is-she-Brazilian / are-they-moms from preview labels already shown."""
    msg_s = str(msg or "").strip()
    peers = _session_preview_peers(session_ctx)
    if not peers:
        return None

    asked_heritage = heritage_terms_in_text(msg_s)
    asked_mom = bool(re.search(r"\b(?:mom|mother|mama|mums?)\b", msg_s, re.I))
    if not _PEER_TRAIT_QUESTION_RE.search(msg_s) and not asked_heritage and not asked_mom:
        return None

    selected = pick_peer_for_intro(peers, msg=msg_s) or peers[0]
    label = str(selected.get("matching_peer_label") or "shared interests").strip()
    peer_index = next(
        (i for i, p in enumerate(peers) if p is selected or p.get("peer_user_id") == selected.get("peer_user_id")),
        0,
    )
    who = f"Neighbor {peer_index + 1}"

    if asked_heritage:
        peer_h = peer_heritage_key(selected)
        want = next(iter(asked_heritage), None)
        if peer_h and want and peer_h == want:
            reply = (
                f"Yes — {who}'s preview shows {want.title()} heritage ({label}). "
                "Verify your phone if you'd like an intro by name."
            )
        elif peer_h and want:
            reply = (
                f"{who}'s preview lists {peer_h.title()}, not {want.title()} ({label}). "
                "Say another neighbor number or ask me to search again."
            )
        else:
            reply = (
                f"I don't see that heritage on {who}'s preview ({label}). "
                "Want me to search your block for Brazilian moms?"
            )
    elif asked_mom:
        if re.search(r"\b(?:mom|mother|mama|mums?)\b", label, re.I):
            reply = f"Yes — {who} is labeled as a Mom ({label})."
        else:
            reply = f"I don't see Mom on {who}'s preview ({label})."
    else:
        return None

    ctx = _routing_ctx(
        session_ctx,
        phase=PHASE_PREVIEW,
        active_intent=INTENT_FIND_PEERS,
        preview_block_id=session_ctx.get("preview_block_id"),
    )
    ctx["peer_matches"] = peers_to_match_rows(peers, phone_verified=phone_verified)
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "peer_trait_question")
    ctx.pop("activity_previews", None)
    return reply, ctx, ctx["last_routing"], ctx["peer_matches"]


def _try_attr_refine_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Re-run attribute search after pushback (e.g. 'no i want brazilian moms')."""
    msg_s = str(msg or "").strip()
    linear = slots_linear_intent(slots or {})
    m = _ATTR_REFINE_RE.search(msg_s)
    if m:
        filter_text = normalize_attr_filter_text(m.group(1), slots)
    elif linear == "discovery.find_by_attrs" and intent_confidence_met(slots, linear):
        if slots_indicate_hosting_signal(slots):
            return None
        filter_text = normalize_attr_filter_text(msg, slots)
    else:
        return None
    if len(filter_text) < 3:
        return None
    if not _session_preview_peers(session_ctx) and phase != PHASE_PREVIEW:
        return None
    if not phone_verified:
        return None
    block_id = _resolve_block_id_for_turn(
        session_ctx=session_ctx,
        home_block_id=home_block_id,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
    )
    if not block_id:
        return None
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
        session_ctx,
        phase=PHASE_PREVIEW,
        active_intent="discovery.find_by_attrs",
        identity_snippet=filter_text,
        preview_block_id=block_id,
    )
    ctx["peer_matches"] = peer_rows
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "find_peers_by_attr_filter")
    ctx.pop("activity_previews", None)
    return reply, ctx, ctx["last_routing"], peer_rows


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
    if phrase_linear_intent(msg) == "identity.show_my_profile":
        return None
    if looks_like_peer_drilldown(msg):
        return None
    goal = str(slots.get("goal") or "none")
    phrase_wants_list = wants_list_intros_phrase(msg)
    slot_wants_list = goal == "list_intros" and float(slots.get("confidence", 0.0)) >= 0.5
    if wants_neighbor_intro(msg) and phrase_wants_list and not slot_wants_list:
        return None
    if not phrase_wants_list and not slot_wants_list:
        return None

    ctx_base = dict(session_ctx)
    if not phone_verified:
        return (
            "Verify your email first — then I can show your pending intros.",
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
                "Verify your email first — then I can show your pending intros.",
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
    when_hint = str(slots.get("signal_when_hint") or "").strip() or None
    where_hint = str(slots.get("signal_where_hint") or "").strip() or None

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
            "Verify your email first — then I can post that to your block.",
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

    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=active_intent,
    )

    matches_shown = int(result.get("matches_created") or 0)
    filtered_entries: list[dict[str, Any]] = []
    try:
        all_entries = fetch_my_block_log(user_jwt)
        filtered_entries = filter_block_log_for_signal(
            all_entries,
            signal_intent=str(result.get("intent") or intent or ""),
            signal_id=str(result.get("signal_id") or "") or None,
            detail_text=str(result.get("detail_text") or detail or "") or None,
        )
        if filtered_entries:
            matches_shown = len(filtered_entries)
            stamp_block_log_ctx(ctx, filtered_entries)
    except HTTPException:
        if matches_shown <= 0:
            matches_shown = 0

    reply = format_signal_saved_reply(
        result,
        detail=detail,
        matches_shown=matches_shown,
        entries=filtered_entries or None,
    )
    stamp_signal_saved_ctx(
        ctx,
        result,
        active_intent=active_intent,
        when_hint=when_hint,
        where_hint=where_hint,
        block_name=str(session_ctx.get("block_display_name") or "") or None,
    )
    if ctx.get("signal_saved") and str(ctx["signal_saved"].get("intent") or "") == "host_meet":
        stamp_pending_hosting_offer(ctx, ctx["signal_saved"])
    if ctx.get("signal_saved") and isinstance(ctx["signal_saved"], dict):
        ctx["signal_saved"]["matches_created"] = matches_shown
    ctx["last_routing"] = _discovery_routing_stub(phase or "listening", "save_local_signal")
    ctx.pop("activity_previews", None)
    clear_signal_draft(ctx)
    return reply, ctx, ctx["last_routing"], []


def _try_hosting_cta_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Bubble CTAs after hosting card — distinct from re-saving the signal."""
    if not phone_verified or not session_has_hosting_offer(session_ctx):
        return None
    if is_hosting_open_cta(msg):
        return handle_hosting_open_turn(
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
        )
    if is_hosting_send_mom_cta(msg):
        return handle_hosting_send_mom_turn(
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
        )
    return None


_SWAP_FOLLOWUP_RE = re.compile(
    r"\b(?:wanna|want to|ok\s+)?(?:swap|swapping|trade|exchange)\b|"
    r"\b(?:bicycle|bike|boots|toy|rain\s+coat|stroller)\b",
    re.I,
)
_TIP_SEEK_RE = re.compile(
    r"\b(?:restaurant|place to eat|good place to eat|where to eat|"
    r"know a good (?:place|spot|restaurant)|recommend(?:ation)?)\b",
    re.I,
)
_BLOCK_LOG_CONTEXT_INTENTS = frozenset({
    INTENT_SAVE_SIGNAL,
    "looking.swap",
    "sharing.swap",
    "looking.tip",
    "sharing.tip",
    INTENT_SHOW_BLOCK_LOG,
    "discovery.block_log",
})


def _history_recent_block_log(history: list[dict[str, Any]] | None) -> bool:
    for turn in reversed((history or [])[-4:]):
        if str(turn.get("role") or "") != "assistant":
            continue
        text = str(turn.get("content") or "").lower()
        if any(
            phrase in text
            for phrase in (
                "block log",
                "neighbor match",
                "active match",
                "i've noted you're looking",
                "introduce me to #",
                "swap or meetup",
            )
        ):
            return True
    return False


def _block_log_context_active(
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
) -> bool:
    if str(session_ctx.get("active_intent") or "") in _BLOCK_LOG_CONTEXT_INTENTS:
        return True
    if session_ctx.get("block_log_entries"):
        return True
    return _history_recent_block_log(history)


def _intro_should_use_block_log(
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
) -> bool:
    if not _block_log_context_active(session_ctx, history):
        return False
    if not wants_neighbor_intro(msg):
        return False
    # Named peer — identity peer intro, not block-log row #1.
    if requested_peer_name(msg) and peer_index_from_message(msg) is None:
        return False
    named = requested_peer_name(msg)
    if named and _session_peer_matches_name(session_ctx, named):
        return False
    if peer_index_from_message(msg) is not None:
        return True
    if re.search(
        r"\b(?:swap|regarding|about\s+the|block\s*log|neighbor\s+match|bicycle|bike)\b",
        str(msg or ""),
        re.I,
    ):
        return True
    return False


def _peer_find_turn_blocked(
    slots: dict[str, Any] | None,
    *,
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
) -> bool:
    """Don't re-run identity peer browse when user pivoted to signals or meta chat."""
    enriched = enrich_slots(dict(slots or {}), msg=msg)
    if str(enriched.get("goal") or "") == "save_signal":
        return True
    linear = slots_linear_intent(enriched)
    if linear and is_signal_lane_intent(enriched) and intent_confidence_met(enriched, linear):
        return True
    if utterance_indicates_tip_seek(msg) or utterance_indicates_swap_seek(msg):
        return True
    if _looks_like_meta_chat(msg):
        return True
    if slots_want_propose_intro(enriched):
        return True
    if slots_indicate_hosting_signal(enriched):
        return True
    if slots_picking_shown_peer(enriched, session_ctx):
        return True
    if str(enriched.get("intro_source") or "").strip():
        return True
    return False


def _wants_swap_followup(msg: str) -> bool:
    return bool(_SWAP_FOLLOWUP_RE.search(str(msg or "")))


def _try_swap_block_log_followup_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
    history: list[dict[str, Any]] | None,
    slots: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """After a swap seek/offer, keep user on block log — not identity peer cards."""
    if not phone_verified or not _wants_swap_followup(msg):
        return None
    if slots and is_signal_lane_intent(slots) and str(slots.get("goal") or "") == "save_signal":
        return None
    recent = (
        str(session_ctx.get("active_intent") or "") in _BLOCK_LOG_CONTEXT_INTENTS
        or _history_recent_block_log(history)
    )
    if not recent and not _wants_block_log(msg, slots or {}):
        return None
    try:
        entries = fetch_my_block_log(user_jwt)
    except HTTPException:
        return None
    swap_entries = filter_block_log_for_signal(entries, signal_intent="swap_seek")
    if not swap_entries and filter_block_log_for_signal(entries, signal_intent="swap_offer"):
        swap_entries = filter_block_log_for_signal(entries, signal_intent="swap_offer")
    if not swap_entries:
        return None
    ctx_base = dict(session_ctx)
    reply = format_block_log_reply(swap_entries)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=INTENT_SHOW_BLOCK_LOG,
    )
    stamp_block_log_ctx(ctx, swap_entries)
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "swap_block_log_followup")
    _clear_peer_surface(ctx)
    ctx.pop("activity_previews", None)
    return reply, ctx, ctx["last_routing"], []


def _try_slots_intro_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    ctx_base: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
    history: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Route propose_intro using AI slots + RECENT TURNS context (not regex #1)."""
    if _ai_slots_block_propose_intro(msg, slots):
        return None
    enriched = enrich_slots(dict(slots), msg=msg)
    if not slots_want_propose_intro(enriched) and not slots_picking_shown_peer(
        enriched, session_ctx
    ):
        return None
    intro_source = str(enriched.get("intro_source") or "").strip().lower()
    list_index = enriched.get("intro_list_index")
    try:
        idx = int(list_index) if list_index is not None else None
        if idx is not None and idx < 1:
            idx = None
    except (TypeError, ValueError):
        idx = None

    named = slots_peer_name(enriched)
    if named and _session_peer_matches_name(session_ctx, named):
        intro_block = resolve_block_id(session_ctx, home_block_id)
        if intro_block:
            return _try_neighbor_intro_turn(
                msg=msg,
                session_ctx=session_ctx,
                ctx_base=ctx_base,
                user_jwt=user_jwt,
                block_id=intro_block,
                phone_verified=phone_verified,
                goal="propose_intro",
                slots=enriched,
                history=history,
            )

    if intro_source == "block_log" or (
        intro_source != "peer_preview"
        and str(session_ctx.get("active_intent") or "") in _BLOCK_LOG_CONTEXT_INTENTS
    ):
        if _intro_should_use_block_log(msg, session_ctx, history):
            turn = _try_block_log_intro_turn(
                msg=msg,
                session_ctx=session_ctx,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
                phase=phase,
                history=history,
                list_index=idx,
            )
            if turn is not None:
                return turn

    intro_block = resolve_block_id(session_ctx, home_block_id)
    if not intro_block:
        return None
    return _try_neighbor_intro_turn(
        msg=msg,
        session_ctx=session_ctx,
        ctx_base=ctx_base,
        user_jwt=user_jwt,
        block_id=intro_block,
        phone_verified=phone_verified,
        goal="propose_intro",
        slots=enriched,
        history=history,
    )


def _swap_entries_for_intro(
    session_ctx: dict[str, Any],
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use the numbered list the user last saw — not a fresh unfiltered fetch."""
    snapshot = session_ctx.get("block_log_intro_list")
    if isinstance(snapshot, list) and snapshot:
        return [dict(row) for row in snapshot if isinstance(row, dict)]
    return [
        row
        for row in entries
        if str(row.get("match_type") or "") in ("inbound_for_my_seek", "inbound_for_my_offer")
    ]


def _try_block_log_intro_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
    history: list[dict[str, Any]] | None,
    list_index: int | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Propose intro to a block-log swap match (index from AI slots or message fallback)."""
    if not phone_verified or not wants_neighbor_intro(msg):
        return None
    ctx_base = dict(session_ctx)
    if ctx_base.get("intro_proposal"):
        return None
    numbered = list_index is not None or peer_index_from_message(msg) is not None
    if numbered:
        for key in ("pending_intro_offer", "pending_intro_respond", "pending_intros"):
            ctx_base.pop(key, None)
    elif ctx_base.get("pending_intro_offer"):
        return None
    if list_index is None and not _intro_should_use_block_log(msg, ctx_base, history):
        return None
    try:
        entries = fetch_my_block_log(user_jwt)
    except HTTPException:
        return None
    swap_entries = _swap_entries_for_intro(ctx_base, entries)
    if not swap_entries:
        return None
    row: dict[str, Any] | None = None
    if list_index is not None:
        pick_idx = list_index - 1
        if 0 <= pick_idx < len(swap_entries):
            row = swap_entries[pick_idx]
        elif swap_entries:
            row = swap_entries[0]
    if row is None:
        row = pick_block_log_entry_for_intro(swap_entries, msg=msg)
    if not row:
        return None
    peer_id = str(row.get("peer_user_id") or "").strip()
    if not peer_id:
        return None

    peer = block_log_peer_from_entry(normalize_block_log_row(row))
    summary = block_log_match_summary(row)
    match_reason = summary or "Swap match on your block."
    if summary and not summary.lower().startswith("they"):
        match_reason = f"Swap match — {summary}"

    entry_id = str(row.get("id") or row.get("entry_id") or "").strip()
    try:
        intro = propose_neighbor_intro(
            user_jwt,
            candidate_user_id=peer_id,
            match_reason=match_reason[:280],
            shared_dimensions=["swap"],
            match_score=float(row["match_strength"])
            if row.get("match_strength") is not None
            else None,
        )
    except HTTPException as exc:
        if "duplicate_intro" in str(exc.detail or "").lower():
            reply = format_duplicate_intro_reply(
                peer=peer,
                user_jwt=user_jwt,
                attempt_summary=summary,
            )
            ctx = _routing_ctx(
                ctx_base,
                phase=phase or PHASE_PREVIEW,
                active_intent=INTENT_SHOW_BLOCK_LOG,
            )
            stamp_block_log_ctx(ctx, swap_entries)
            _clear_peer_surface(ctx)
            if not stamp_intro_respond_from_peer(ctx, user_jwt=user_jwt, peer=peer):
                stamp_duplicate_intro_sent(ctx, peer=peer, match_reason=match_reason)
            ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "block_log_intro_duplicate")
            return reply, ctx, ctx["last_routing"], []
        raise

    if not intro.get("intro_id"):
        if str(intro.get("status") or "") == "duplicate":
            reply = format_duplicate_intro_reply(
                peer=peer,
                user_jwt=user_jwt,
                attempt_summary=summary,
            )
            ctx = _routing_ctx(
                ctx_base,
                phase=phase or PHASE_PREVIEW,
                active_intent=INTENT_SHOW_BLOCK_LOG,
            )
            stamp_block_log_ctx(ctx, swap_entries)
            _clear_peer_surface(ctx)
            if not stamp_intro_respond_from_peer(ctx, user_jwt=user_jwt, peer=peer):
                stamp_duplicate_intro_sent(ctx, peer=peer, match_reason=match_reason)
            ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "block_log_intro_duplicate")
            return reply, ctx, ctx["last_routing"], []
        return (
            "I couldn't send that nudge right now — try again in a moment.",
            _routing_ctx(
                ctx_base,
                phase=phase or PHASE_PREVIEW,
                active_intent=INTENT_SHOW_BLOCK_LOG,
            ),
            _discovery_routing_stub(PHASE_PREVIEW, "block_log_intro_failed"),
            [],
        )

    if entry_id:
        try:
            block_log_take_action(user_jwt, entry_id, "nudged")
        except HTTPException:
            pass

    reply = format_intro_proposed_reply(peer, match_reason)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=INTENT_PROPOSE_INTRO,
    )
    stamp_block_log_ctx(ctx, swap_entries)
    stamp_intro_proposal_ctx(ctx, intro=intro, peer=peer)
    attach_pending_intros_after_propose(ctx, user_jwt=user_jwt, intro=intro, peer=peer)
    _clear_peer_surface(ctx)
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "block_log_propose_intro")
    return reply, ctx, ctx["last_routing"], []


def _try_block_log_nudge_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    phase: str,
    history: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Nudge a block-log match when user asks for intro in swap/tip context."""
    if not phone_verified or not wants_neighbor_intro(msg):
        return None
    if session_ctx.get("pending_intro_offer") or session_ctx.get("intro_proposal"):
        return None
    recent = (
        str(session_ctx.get("active_intent") or "") in _BLOCK_LOG_CONTEXT_INTENTS
        or _history_recent_block_log(history)
    )
    if not recent:
        return None
    try:
        entries = fetch_my_block_log(user_jwt)
    except HTTPException:
        return None
    swap_entries = [
        row for row in entries
        if str(row.get("match_type") or "") in ("inbound_for_my_seek", "inbound_for_my_offer")
    ]
    if not swap_entries:
        return None
    row = swap_entries[0]
    entry_id = str(row.get("id") or row.get("entry_id") or "").strip()
    nick = str(row.get("peer_preview_label") or "A neighbor").strip()
    summary = block_log_match_summary(row)
    if entry_id:
        try:
            block_log_take_action(user_jwt, entry_id, "nudged")
        except HTTPException:
            pass
    ctx_base = dict(session_ctx)
    ctx = _routing_ctx(
        ctx_base,
        phase=phase or PHASE_PREVIEW,
        active_intent=INTENT_SHOW_BLOCK_LOG,
    )
    stamp_block_log_ctx(ctx, swap_entries)
    _clear_peer_surface(ctx)
    bit = f" — {summary}" if summary else ""
    reply = (
        f"I nudged {nick}{bit}. If they're interested, they'll get a notification "
        "and can reply in Lana. Say show my block log to see all swap matches."
    )
    ctx["last_routing"] = _discovery_routing_stub(PHASE_PREVIEW, "block_log_nudge")
    return reply, ctx, ctx["last_routing"], []


def _try_signal_seek_early_turn(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    """Tip/swap seek before heritage traps and sticky peer preview."""
    seek_slots = enrich_slots(dict(slots), msg=msg)
    if _wants_block_log(msg, seek_slots):
        return None
    if slots_indicate_tip_share_signal(seek_slots) or utterance_indicates_tip_share(msg):
        return None
    if slots_indicate_hosting_signal(seek_slots):
        return None
    goal = str(seek_slots.get("goal") or "")
    signal_intent = str(seek_slots.get("signal_intent") or "")
    tip = utterance_indicates_tip_seek(msg) or (
        goal == "save_signal" and signal_intent == "tip_seek"
    )
    swap = utterance_indicates_swap_seek(msg) or (
        goal == "save_signal" and signal_intent in ("swap_seek", "swap_offer")
    )
    save_signal = goal == "save_signal" and is_signal_lane_intent(seek_slots)
    if not (tip or swap or save_signal):
        return None
    if tip:
        turn = _try_tip_seek_fast_turn(
            msg=msg,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
            slots=slots,
        )
        if turn is not None:
            return turn
    return _try_signal_lane_turn(
        msg=msg,
        slots=seek_slots,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        phase=phase,
    )


def _try_meta_chat_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    phase: str,
    phone_verified: bool,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    if not _looks_like_meta_chat(msg):
        return None
    # Verified users have live peer/discovery state, so a fall-through would loop
    # back into a peer list ("Are you dumb?" → 5 neighbors). Intercept with a
    # plain meta reply. Unverified/guest turns have no such state — let the
    # orchestrator (LLM) answer naturally instead of a canned line.
    if not phone_verified:
        return None
    ctx = _routing_ctx(
        session_ctx,
        phase=phase or PHASE_PREVIEW,
        active_intent="help.who_are_you",
    )
    _clear_peer_surface(ctx)
    ctx.pop("activity_previews", None)
    reply = (
        "I'm Lana — your block neighbor assistant. I can help you find people, "
        "borrow or swap gear, get local tips, or plan meetups. What would you like?"
    )
    ctx["last_routing"] = _discovery_routing_stub(phase or PHASE_PREVIEW, "meta_chat")
    return reply, ctx, ctx["last_routing"], []


def _try_tip_seek_fast_turn(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    phase: str,
    slots: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    enriched = enrich_slots(dict(slots or {}), msg=msg)
    if not (
        utterance_indicates_tip_seek(msg)
        or str(enriched.get("signal_intent") or "") == "tip_seek"
    ):
        return None
    if slots and is_signal_lane_intent(enriched) and str(enriched.get("signal_intent") or "") != "tip_seek":
        linear = slots_linear_intent(enriched)
        if linear and linear != "looking.tip" and intent_confidence_met(enriched, linear):
            return None
    return _try_signal_lane_turn(
        msg=msg,
        slots=enriched,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        phase=phase,
    )


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
            "Verify your email first — then I can show your block log.",
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
        # Flash classification OR the deterministic phrase backstop — so "sign me in"
        # never silently fails when Flash labels it as chat/discovery (mirrors logout).
        return slots_want_login(slots) or wants_login_intent(msg)
    return wants_login_intent(msg)


def _turn_wants_signup_gate(
    msg: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    if discovery_ai_enabled():
        return slots_want_signup_gate(slots)
    return wants_signup_intent(msg)


# Deterministic logout phrases — a backstop so "log me out" always works even when the
# Flash classifier misses it (goal≠logout). Matched as a whole-intent, not substring.
_LOGOUT_RE = re.compile(
    r"\b(?:log|sign)\s*(?:me\s+)?out\b|\b(?:logout|signout)\b",
    re.IGNORECASE,
)


def looks_like_logout(message: str) -> bool:
    """True for clear logout requests (log out / log me out / sign me out / logout)."""
    return bool(_LOGOUT_RE.search(str(message or "").strip()))


def _turn_wants_logout(
    msg: str,
    slots: dict[str, Any] | None,
    session_ctx: dict[str, Any],
) -> bool:
    # AI classification OR the deterministic phrase backstop — so logout never silently
    # fails when Flash classifies "log me out" as chat.
    if discovery_ai_enabled():
        return slots_want_logout(slots) or looks_like_logout(msg)
    return wants_logout_intent(msg) or looks_like_logout(msg)


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


_PIVOT_SLOT_GOALS = frozenset({
    "save_signal",
    "show_block_log",
    "profile_photo",
    "login",
    "logout",
    "list_intros",
    "propose_intro",
    "verify",
    "chat",
})

_PIVOT_LINEAR_INTENTS = frozenset({
    "discovery.block_log",
    "identity.show_my_profile",
    "settings.change_name",
    "discovery.show_peer_profile",
    "discovery.explain_peer_match",
    "identity.edit_claim",
    "identity.complete_profile",
    "settings.change_zip",
    "help.what_can_you_do",
    "help.who_are_you",
    "social.list_intros",
    "social.propose_intro",
    "auth.signup_phone",
    "auth.login_phone",
    "auth.logout",
})


def _should_clear_discovery_goal(slots: dict[str, Any], msg: str) -> bool:
    """Drop sticky peers/activities goal when the user pivots this turn."""
    slot_goal = str(slots.get("goal") or "none").lower()
    conf = float(slots.get("confidence", 0.0))
    if slot_goal in _PIVOT_SLOT_GOALS and conf >= 0.5:
        return True
    if slot_goal == "save_signal" and conf >= 0.5:
        return True
    enriched = enrich_slots(dict(slots), msg=msg)
    linear = slots_linear_intent(enriched)
    if linear in _PIVOT_LINEAR_INTENTS and intent_confidence_met(enriched, linear):
        return True
    if linear and linear.startswith(("looking.", "sharing.")):
        if intent_confidence_met(enriched, linear):
            return True
    return False


def _effective_discovery_goal(
    msg: str,
    session_ctx: dict[str, Any],
    slots: dict[str, Any],
) -> str:
    """
    Browse goal for this turn: Flash when explicit; else persisted across ZIP/continue steps.
    Updates session.discovery_goal when user pivots (e.g. peers → activities).
    """
    slot_goal = str(slots.get("goal") or "none").lower()
    conf = float(slots.get("confidence", 0.0))

    if wants_activities_browse(msg) or (slot_goal == "activities" and conf >= 0.45):
        session_ctx["discovery_goal"] = "activities"
        return "activities"

    if _should_clear_discovery_goal(slots, msg):
        session_ctx.pop("discovery_goal", None)

    enriched_goal = enrich_slots(dict(slots), msg=msg)
    if str(enriched_goal.get("goal") or "") == "save_signal":
        session_ctx.pop("discovery_goal", None)
        return "save_signal"

    _update_discovery_goal_from_slots(session_ctx, slots)
    stored = str(session_ctx.get("discovery_goal") or "none")

    if slot_goal in _DISCOVERY_GOALS and conf >= 0.45:
        return slot_goal
    # Answering the funnel (goal=continue) or sending a bare ZIP keeps the goal the user
    # already chose. A real pivot away from activities was already cleared above by
    # _should_clear_discovery_goal, so an activities browser must NOT be downgraded to
    # peers just because "32827" (or an identity snippet) carries no activity keyword —
    # that bug made every activities search collapse into a neighbor search after ZIP.
    if slot_goal == "continue" and stored in _DISCOVERY_GOALS:
        return stored
    if extract_zip(msg) and stored in _DISCOVERY_GOALS:
        return stored
    if stored == "activities" and not wants_activities_browse(msg):
        session_ctx.pop("discovery_goal", None)
        return slot_goal if slot_goal not in ("none",) else "peers"
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


_DECLINE_INPUT_RE = re.compile(
    r"\b(?:don'?t want to|do not want to|rather not|won'?t|will not|"
    r"not (?:right )?now|not yet|maybe later|later|skip|no thanks?|"
    r"prefer not|why do you need|none of your)\b",
    re.I,
)


def _declines_to_answer(msg: str) -> bool:
    """User is refusing the question being asked (e.g. won't give a ZIP) — off-ramp,
    don't re-prompt. Safety valve only; the AI still drives normal routing."""
    return bool(_DECLINE_INPUT_RE.search(str(msg or "").strip())) or is_signal_cancel(msg)


def _host_via_orchestrator() -> bool:
    """Hosting a full event runs through the orchestrator (OpenAI) in-chat, not the
    lightweight host_meet signal. Falls back to the signal lane when orchestrator off."""
    try:
        from app.orchestrator.pipeline import orchestrator_enabled

        return bool(orchestrator_enabled())
    except Exception:
        return False


# While hosting, ONLY an explicit pivot leaves the flow — narrow patterns so an event
# description ("weekday playground meet with kids") is never mistaken for a pivot.
_HOST_PIVOT_RE = re.compile(
    r"\b(?:find|show)\s+(?:me\s+)?(?:\w+\s+){0,3}(?:moms?|dads?|parents?|neighbou?rs?|people|families)\b|"
    r"\bshow my (?:block log|intros)\b|\bmy block log\b|\blog\s?out\b|\bsign out\b",
    re.I,
)
# Backstop so a verified user can never be trapped in host mode.
_EVENT_HOST_TURN_CAP = 12


def _pivots_out_of_host(msg: str) -> bool:
    return bool(_HOST_PIVOT_RE.search(str(msg or "").strip()))


# Lanes the host capture does NOT own — a confident classification into any is a pivot
# away from hosting (sharing.host / host_meet is THIS lane). Browsing events
# (find_activities) and looking for a meet (meet_seek) are pivots too.
# What THIS lane owns: hosting/creating an event (sharing.host / host_meet). Browsing
# events (find_activities), being matched to a meet (meet_seek), find people, swap, tip,
# out_of_scope, unsafe, auth, … are all off-lane and release. We list only what we own; the
# open-ended rest is handled generically (see is_confident_off_lane).
_HOST_NATIVE_GOALS: frozenset[str] = frozenset()
_HOST_NATIVE_LINEARS = frozenset({"sharing.host"})
_HOST_NATIVE_SIGNALS = frozenset({"host_meet"})


def _host_confident_foreign(slots: dict[str, Any] | None) -> bool:
    """AI-driven: the classifier confidently reads this turn as a DIFFERENT lane, so the
    host capture should release rather than treat it as an event detail."""
    from app.lane_decision import is_confident_off_lane

    return is_confident_off_lane(
        slots,
        native_goals=_HOST_NATIVE_GOALS,
        native_linears=_HOST_NATIVE_LINEARS,
        native_signals=_HOST_NATIVE_SIGNALS,
    )


def _host_confident_foreign_action(slots: dict[str, Any] | None) -> bool:
    """Like ``_host_confident_foreign`` but the GOAL alone never releases — only a concrete
    foreign ACTION (a foreign linear_intent / signal_intent, e.g. find_activities, meet_seek,
    find_peers) or a universal exit (out_of_scope / unsafe). Used at the naming step, where a
    legitimate title that names an activity ("Soccer in the park") reads as bare
    goal="activities" — that must stay as the title — but an explicit pivot ("I wanna search
    a meet") carries a concrete foreign action and must release, so the very first step can
    never trap the user (see [[no-sticky-flows]])."""
    from app.lane_decision import is_confident_off_lane

    return is_confident_off_lane(
        slots,
        native_linears=_HOST_NATIVE_LINEARS,
        native_signals=_HOST_NATIVE_SIGNALS,
        ignore_goal=True,
    )


def _is_host_answer(
    message: str, session_ctx: dict[str, Any], slots: dict[str, Any] | None
) -> bool:
    """Is this turn a genuine answer to the host flow's CURRENT step?

    While we're collecting a specific field, the reply IS that field — but the classifier
    mis-reads a venue name ("South Econ Community Park") as discovery.find_activities and a
    capacity chip ("Open · no limit") as off-lane noise. Taking those as confident pivots
    used to release host mode mid-build, wiping the draft + the resolved date, so the flow
    re-asked "when?" with the date still showing in the card. Step-awareness suppresses
    that false positive; an explicit pivot (``_HOST_PIVOT_RE``), an abandon, or a meta /
    question turn still releases."""
    from app.lane_decision import is_meta_or_chat

    # A question / meta turn ("what's my zip?", "who's coming?") is never a field answer —
    # let normal routing answer it instead of capturing it as an event detail.
    if is_meta_or_chat(slots):
        return False
    # out_of_scope / unsafe are UNIVERSAL exits — never a host field answer, even at the
    # place/settings steps where any reply is otherwise taken as the venue/chip. Without this,
    # "fix my sink" at the where-step is captured as the venue and the user is trapped.
    _g = str((slots or {}).get("goal") or "")
    _ln = str((slots or {}).get("linear_intent") or "")
    if _g in ("out_of_scope", "unsafe") or _ln in ("system.out_of_scope", "system.unsafe"):
        return False
    draft = session_ctx.get("event_draft")
    draft = draft if isinstance(draft, dict) else {}
    has_title = bool(str(draft.get("title") or "").strip())
    has_venue = bool(str(draft.get("venue_name") or "").strip())
    place_asked = bool(session_ctx.get("event_place_asked"))
    cap_asked = bool(session_ctx.get("event_cap_asked"))
    # Naming step (no title yet) — the reply is normally the title, BUT the AI must still be
    # able to pivot the user out: a confident read of a concrete other action ("I wanna
    # search a meet" -> find_activities / meet_seek) releases, so the first step never traps.
    # A bare title that merely reads as goal="activities" ("Soccer in the park") still stays.
    # An abandon ("I dont wanna host anything") is the AI's `abandon` flag, handled by the
    # release gate — not this predicate (see [[no-sticky-flows]]).
    if not has_title:
        return not _host_confident_foreign_action(slots)
    # Where-step (asked where, no venue pinned yet) — any reply is the place, even when the
    # classifier reads a venue name as a find_activities search.
    if place_asked and not has_venue:
        return True
    # Settings steps (capacity → approval → share, asked after place) — the chip taps
    # classify as noise, not a host detail.
    if cap_asked:
        return True
    # Otherwise (when / time, between title and the where-step) — a normal answer stays;
    # only a confident pivot to another lane releases.
    return not _host_confident_foreign(slots)


def _release_host_mode(session_ctx: dict[str, Any]) -> None:
    """Exit the sticky event-host flow and drop the in-progress draft, so a later
    'host an event' starts clean instead of resuming this abandoned one. Keys are set
    to None (falsy) rather than popped — the session merge keeps {**old, **new}, so a
    missing key would let the stale value survive; an explicit None clears it."""
    session_ctx["event_host_active"] = False
    session_ctx["event_host_turns"] = 0
    for key in (
        "host_publish_pending",
        "event_draft",
        "event_when_date",
        "event_when_time",
        "event_place_asked",
        "event_venue",
        "event_settings",
        "event_cap_asked",
        "event_approval_asked",
        "event_share_asked",
        "event_affinity_asked",
        "requires_phone_verification",
    ):
        session_ctx[key] = None


# Explicit "host/throw/plan a <event>" — a deterministic entry into the event flow that
# does NOT depend on the CTA hint or the classifier. Requires a host verb + an event
# noun so "I want to meet people" (discovery) is never caught.
_HOST_ENTRY_RE = re.compile(
    r"\bhost(?:ing)?\b.{0,30}\b(?:meet|meet-?up|event|gathering|playgroup|play\s?group|"
    r"playdate|play\s?date|brunch|coffee|breakfast|picnic|hang(?:out)?|get[- ]?together|"
    r"party|potluck|walk|stroll|playground|class|circle|club)\b|"
    r"\b(?:throw|organi[sz]e|plan|set up)\b.{0,30}\b(?:meetup|meet-?up|gathering|playgroup|"
    r"play\s?group|playdate|brunch|picnic|party|potluck|get[- ]?together|event)\b",
    re.I,
)


def looks_like_host_event_entry(msg: str) -> bool:
    """Deterministic 'host an event' entry — host/plan verb + an event noun."""
    return bool(_HOST_ENTRY_RE.search(str(msg or "").strip()))


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


def _is_negative(msg: str) -> bool:
    lower = str(msg or "").strip().lower().rstrip(".!")
    if lower in {"no", "nope", "nah", "cancel"}:
        return True
    return lower.startswith(("no ", "nope ", "nah "))


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

    # A pending out-of-scope clarifier must always resolve in the discovery handler, even
    # if the reply ('yeah, do it') reads as plain chat — otherwise the flag leaks forward.
    if session_ctx.get("out_of_scope_pending"):
        return True

    # Egregious unsafe content must reach the discovery handler so the refusal fires, even
    # if the AI router misread it as chat — the regex backstop only works if we get here.
    if utterance_is_unsafe(msg)[0]:
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


def ensure_home_block_for_verified_user(
    user_jwt: str, *, session_ctx: dict[str, Any]
) -> str | None:
    """Persist a verified user's home block from whatever block/ZIP we know in the session.
    Called at the top of every turn for a verified user with no home_block_id — so signing
    up always leaves you with a block, never stranded. Returns the assigned block id, or
    None when nothing is known yet (e.g. a guest who hasn't given a ZIP)."""
    return _try_assign_home_block(user_jwt, session_ctx=session_ctx, home_block_id=None)


def _try_assign_home_block(
    user_jwt: str,
    *,
    session_ctx: dict[str, Any],
    home_block_id: str | None,
) -> str | None:
    """Persist the user's home block after phone verify (required for vector match AND for
    the activity browse to find events). Resolves the block from, in order: an already-set
    home_block_id, the session preview block, or a known ZIP (session preview_zip/zip)
    re-resolved to a block — the last covers the case where the session preview block was
    lost across the anonymous→registered identity switch but a ZIP is still around."""
    if home_block_id:
        return home_block_id
    bid = resolve_block_id(session_ctx, None)
    zip5 = (
        session_ctx.get("preview_zip")
        or session_ctx.get("zip")
        or session_ctx.get("zip_code")
    )
    if not bid and zip5:
        try:
            blocks = fetch_blocks_for_zip(user_jwt, str(zip5))
        except HTTPException:
            blocks = []
        if blocks:
            bid = str(blocks[0].get("block_id") or "")
    if not bid:
        return None
    payload: dict[str, Any] = {"p_block_id": bid}
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
    if _looks_like_meta_chat(msg):
        return True
    enriched = enrich_slots(dict(slots or {}), msg=msg)
    if str(enriched.get("goal") or "") == "save_signal":
        return True
    linear = slots_linear_intent(enriched)
    if linear and is_signal_lane_intent(enriched) and intent_confidence_met(enriched, linear):
        return True
    if utterance_indicates_tip_seek(msg) or utterance_indicates_swap_seek(msg):
        return True
    if phase != PHASE_PREVIEW:
        return False
    if is_profile_acknowledgment(msg):
        return True
    if wants_more_peer_detail(msg) or goal in ("verify", "rsvp"):
        return False
    if slots_want_preview_refetch(slots or {}, session_ctx, msg=msg):
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
                "activity_id": str(ev.get("id") or "") or None,
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
    pool: int | None = None,
) -> list[dict[str, Any]]:
    """Upcoming open events on preview block (service role).

    `pool` overrides how many rows to pull from the DB before slicing to `limit` —
    callers that filter the result downstream (date/host/topic) pass a larger pool so
    the candidate set isn't pre-truncated to just the soonest few. `with_host_name`
    attaches each host's nickname for host-aware filtering.
    """
    try:
        sb = service_client()
        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        fetch_n = pool if pool and pool > 0 else limit * 3
        res = (
            sb.table("events")
            .select("id, title, starts_at, venue_name, cohort_tags, host_id")
            .eq("block_id", block_id)
            .eq("status", "open")
            .gte("starts_at", now_iso)
            .order("starts_at")
            .limit(fetch_n)
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
        lines.append("Verify your email to RSVP — or ask me to find neighbors like you.")
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
        lead = f"To join {event_label}, verify your email first — I'll send you a code."
    else:
        lead = "I can see neighbors nearby — to show names and connect you, verify your email first."
    ctx = _routing_ctx(
        ctx_base,
        phase=PHASE_AWAIT_SIGNUP_PHONE,
        preview_block_id=block_id,
    )
    ctx["requires_phone_verification"] = True
    ctx["peer_matches"] = []
    return (
        f"{lead} What's your email?",
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
            "Verify your email to see names and connect — or tell me more about you for sharper matches."
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
    """Parse email → await_signup_otp + link_email_signup, or login OTP if email exists."""
    email = extract_email(msg)
    if not email:
        return (
            "What's your email? I'll send you a code to verify.",
            _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_PHONE),
            _discovery_routing_stub(PHASE_AWAIT_SIGNUP_PHONE),
            [],
        )
    if is_anonymous and email_has_registered_account(email):
        # Guest is logging into an account they already have. The JWT swap + force_new
        # session reset would orphan an event/meet-seek they just built, so stash it against
        # the destination account — its next session recovers and publishes/saves it.
        if session_ctx.get("host_publish_pending"):
            host_ctx = extract_host_ctx(session_ctx)
            if host_ctx.get("event_draft"):
                dest_uid = registered_user_id_for_email(email)
                if dest_uid:
                    stash_pending_event_draft(dest_uid, host_ctx)
        pending_seek = session_ctx.get("look_seek_pending")
        if isinstance(pending_seek, dict) and pending_seek:
            dest_uid = registered_user_id_for_email(email)
            if dest_uid:
                stash_pending_meet_seek(dest_uid, pending_seek)
        ctx = _login_ctx(
            session_ctx,
            guest_step=GUEST_STEP_LOGIN_OTP,
            login_phone=email,
            requires_login_otp=True,
        )
        ctx.pop("signup_phone", None)
        ctx.pop("pending_signup_gate", None)
        ctx["unified_mode"] = True
        ctx["auth_action"] = _auth_action(
            type="send_login_otp",
            email=email,
            verify_type="email",
        )
        return (
            f"I found your account — I sent a login code to {email}. Enter it when it arrives.",
            ctx,
            _discovery_routing_stub(GUEST_STEP_LOGIN_OTP),
            [],
        )
    ctx = _routing_ctx(
        session_ctx,
        phase=PHASE_AWAIT_SIGNUP_OTP,
        signup_phone=email,
    )
    ctx["auth_action"] = _auth_action(
        type="link_email_signup",
        email=email,
        verify_type="email_change",
    )
    return (
        f"Got it — I sent a 6-digit code to {email}. Enter it here when it arrives.",
        ctx,
        _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
        [],
    )


def _browse_or_seek_decision(slots: dict[str, Any], msg: str) -> str | None:
    """AI-first router for the find-something-to-do space.

    Returns 'browse' (show the block's real events), 'seek' (look_meet capture + match),
    'clarify' (genuinely ambiguous — ask one question), or None (not this space). The AI
    owns the call: it sets clarify='browse_or_meet' when torn, and a low-confidence read in
    this space also clarifies rather than guesses. Hosting is its own lane.
    """
    if not slots:
        return None
    enriched = enrich_slots(dict(slots), msg=msg)
    if slots_indicate_hosting_signal(enriched):
        return None
    linear = slots_linear_intent(enriched) or ""
    goal = str(enriched.get("goal") or "")
    signal_intent = str(enriched.get("signal_intent") or "")
    is_browse = linear == "discovery.find_activities" or goal == "activities"
    is_seek = linear == "looking.meet" or signal_intent == "meet_seek"
    if not (is_browse or is_seek):
        return None
    if str(slots.get("clarify") or "") == "browse_or_meet":
        return "clarify"
    if is_browse and is_seek:
        return "clarify"  # both signals, model didn't disambiguate → ask
    if is_browse:
        # A browse intent → the events browse; ask if the model isn't confident.
        return "browse" if float(enriched.get("confidence", 0.0)) >= 0.55 else "clarify"
    # A clear meet_seek is owned by the existing signal-capture flow — don't divert here.
    return None


def _resolve_browse_or_meet_answer(msg: str, slots: dict[str, Any] | None) -> str:
    """Interpret the user's reply to the browse-or-meet clarifier. Always resolves to
    'browse' or 'seek' (never re-asks) — defaults to 'browse' (show what exists)."""
    low = str(msg or "").lower()
    if re.search(
        r"\b(meet|set ?up|match(?:ed)?|buddy|partner|together|with (?:other )?"
        r"(?:people|moms?|dads?|neighbou?rs?))\b",
        low,
    ):
        return "seek"
    if re.search(r"\b(see|show|what'?s|happening|going on|browse|events?|activit|list|nearby)\b", low):
        return "browse"
    return "seek" if _browse_or_seek_decision(slots or {}, msg) == "seek" else "browse"


def _ask_browse_or_meet(
    session_ctx: dict[str, Any],
    *,
    question: str = "",
    options: list[str] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """One-tap clarifier when the AI can't tell browse from seek. Uses the classifier's
    own contextual question/options (Lana's voice, grounded in what the user said) when
    present — consistent with the scope/intent clarifiers — and falls back to the template
    only when the model returned nothing."""
    session_ctx["browse_or_meet_pending"] = True
    ctx = _routing_ctx(session_ctx, phase="listening", active_intent="discovery.find_activities")
    opts = [o for o in (options or []) if str(o).strip()] or ["See what's happening", "Set up a meet"]
    ctx["suggestions"] = opts
    ctx["clarify_options"] = opts
    q = (question or "").strip() or (
        "Happy to help! Want me to show what's already happening on your block, or set you "
        "up with a meet so I can match you with neighbors who want the same?"
    )
    return (
        q,
        ctx,
        _discovery_routing_stub("listening", "clarify_browse_or_meet"),
        [],
    )


_SUPPORTED_PIVOT_GOALS = frozenset(
    {"peers", "activities", "both", "save_signal", "verify", "login", "logout",
     "propose_intro", "list_intros", "show_block_log", "profile_photo", "rsvp"}
)


def _out_of_scope_decision(slots: dict[str, Any], msg: str) -> str | None:
    """AI-driven out-of-scope router. Returns:
      'decline' — confidently an errand TagAlng can't do → refuse + log,
      'clarify' — might be unsupported, ask one question before refusing,
      None       — not an out-of-scope turn.

    Confidence IS the gate: the classifier sets goal=out_of_scope with a confidence that
    reflects how sure it is the ask is unsupported, and clarify='scope' when genuinely torn.
    A clear ask declines outright; a doubtful one asks first so Lana never guesses 'no'.
    """
    if not slots:
        return None
    enriched = enrich_slots(dict(slots), msg=msg)
    goal = str(enriched.get("goal") or "")
    linear = slots_linear_intent(enriched) or ""
    if goal != "out_of_scope" and linear != "system.out_of_scope":
        return None
    if str(slots.get("clarify") or "") == "scope":
        return "clarify"
    return "decline" if float(enriched.get("confidence", 0.0)) >= 0.9 else "clarify"


def _reply_pivots_to_supported(slots: dict[str, Any], msg: str) -> bool:
    """After Lana asked the scope-clarifier, does the reply reveal a SUPPORTED intent
    (so we fall through to its handler) rather than confirm the unsupported ask? Default is
    to confirm the decline — only a confident pivot to a real TagAlng lane escapes it."""
    if not slots:
        return False
    enriched = enrich_slots(dict(slots), msg=msg)
    goal = str(enriched.get("goal") or "")
    if goal not in _SUPPORTED_PIVOT_GOALS:
        return False
    return float(enriched.get("confidence", 0.0)) >= 0.5


def _ask_out_of_scope(
    session_ctx: dict[str, Any],
    *,
    question: str = "",
    options: list[str] | None = None,
    detail: str = "",
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Ask the clarifying question the CLASSIFIER wrote (Lana's voice, grounded in what the
    user actually said) when it can't tell a supported request from an errand Lana can't run
    — a bare want ('I want pizza') might be a neighbor activity (a pizza night) OR an errand
    (order me one). The template below is only a fallback if the model returned no question."""
    session_ctx["out_of_scope_pending"] = True
    ctx = _routing_ctx(session_ctx, phase="listening", active_intent="none")
    q = (question or "").strip()
    opts = [o for o in (options or []) if str(o).strip()]
    if not q:
        thing = (detail or "").strip()
        if thing:
            q = (
                f"Ooh, {thing}! Do you want to get neighbors together for that on your block, "
                f"or are you asking me to order/handle it for you?"
            )
        else:
            q = (
                "Just so I get this right — do you want to get neighbors together for that on "
                "your block, or are you asking me to handle it for you directly?"
            )
    ctx["suggestions"] = opts or ["Get neighbors together", "Just handle it for me"]
    ctx["clarify_options"] = ctx["suggestions"]
    return (
        q,
        ctx,
        _discovery_routing_stub("listening", "clarify_out_of_scope"),
        [],
    )


def _ask_general_clarify(
    session_ctx: dict[str, Any],
    *,
    question: str = "",
    options: list[str] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """ASK-WHEN-UNSURE gate: the classifier could not confidently place this turn in a
    supported lane (clarify='intent'), so ask the AI-written question grounded in what
    TagAlng can do instead of guessing. The next turn re-classifies the answer normally —
    the chips post a clear label that routes to the real lane (no pending interpretation
    needed). The template below is only a fallback if the model returned no question."""
    session_ctx["clarify_pending"] = True
    ctx = _routing_ctx(session_ctx, phase="listening", active_intent="none")
    q = (question or "").strip() or (
        "I want to make sure I help with the right thing — are you hoping to meet neighbors, "
        "find something happening nearby, or share or ask for a local tip?"
    )
    opts = [o for o in (options or []) if str(o).strip()]
    ctx["suggestions"] = opts or ["Meet neighbors", "What's happening nearby", "Share a tip"]
    ctx["clarify_options"] = ctx["suggestions"]
    return (
        q,
        ctx,
        _discovery_routing_stub("listening", "clarify_intent"),
        [],
    )


def _decline_out_of_scope(
    *,
    msg: str,
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
    user_id: str | None,
    home_block_id: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Gracefully decline an unsupported ask, log the demand, and steer back to what
    TagAlng does. The log is what lets the 'we'll let you know' promise be kept later."""
    enriched = enrich_slots(dict(slots), msg=msg)
    detail = str(enriched.get("signal_detail") or "").strip()
    category = str(enriched.get("signal_category") or "").strip() or None
    log_feature_request(
        user_id=user_id,
        block_id=resolve_block_id(session_ctx, home_block_id),
        request_text=msg,
        category=category or (detail or None),
    )
    what = detail or "that"
    reply = (
        f"Ah, {what} isn't something I can do yet — I'm here to connect you with neighbors "
        "on your block: finding people, local activities, swapping things, and sharing tips. "
        "I've noted your request though, and we'll let you know if we add it! "
        "In the meantime, want to meet some neighbors or see what's happening nearby?"
    )
    ctx = _routing_ctx(session_ctx, phase="listening", active_intent="none")
    ctx["suggestions"] = ["Meet neighbors", "What's happening nearby"]
    return reply, ctx, _discovery_routing_stub("listening", "out_of_scope"), []


_UNSAFE_HIGH_KINDS = frozenset({"sexual", "hate", "illegal"})


def _unsafe_kind_for_turn(slots: dict[str, Any], msg: str) -> str | None:
    """Return the unsafe kind if this turn is inappropriate/abusive — from the regex backstop
    OR the AI router (goal=unsafe). Safety is NOT confidence-gated: any unsafe read refuses.
    None when the turn is fine. Crisis content (self-harm/DV) is deliberately NOT caught here
    — that is handled by the orchestrator's empathetic safety rails, not a flat refusal."""
    matched, kind = utterance_is_unsafe(msg)
    if matched:
        return kind or "other"
    if slots:
        enriched = enrich_slots(dict(slots), msg=msg)
        if str(enriched.get("goal") or "") == "unsafe" or slots_linear_intent(enriched) == "system.unsafe":
            return str(enriched.get("unsafe_kind") or "").strip() or "other"
    return None


def _refuse_unsafe(
    *,
    msg: str,
    kind: str,
    session_ctx: dict[str, Any],
    user_id: str | None,
    home_block_id: str | None,
    phase: str,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Firm, calm boundary + redirect for inappropriate/abusive content. Logged to
    moderation_flags (NEVER feature_requests — this is not product demand and gets no
    'we'll add it' promise). No escalation: the same measured refusal every time. Preserves
    any active flow's phase/intent so a one-off abusive turn doesn't derail the session."""
    log_moderation_flag(
        user_id=user_id,
        block_id=resolve_block_id(session_ctx, home_block_id),
        message=msg,
        kind=kind,
        severity="high" if kind in _UNSAFE_HIGH_KINDS else "medium",
    )
    reply = (
        "I'm not able to help with that. I'm here to connect you with neighbors on your "
        "block — finding people, local activities, swaps, and tips. Want to do any of those?"
    )
    keep_phase = phase or "listening"
    ctx = _routing_ctx(
        session_ctx, phase=keep_phase, active_intent=session_ctx.get("active_intent") or "none"
    )
    ctx["suggestions"] = ["Meet neighbors", "What's happening nearby"]
    return reply, ctx, _discovery_routing_stub(keep_phase, "unsafe_refusal"), []


def _start_activity_browse_from_discovery(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
    user_jwt: str,
    home_block_id: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Begin the agentic events-browse and return its first turn (asks the interest). The
    sticky flow continues on later turns via the pipeline's activity_browse_active gate."""
    from app.activity_browse import run_activity_browse_turn

    session_ctx["activity_browse_active"] = True
    session_ctx["browse_turns"] = 0
    session_ctx["browse_draft"] = None
    reply = run_activity_browse_turn(
        user_message=msg,
        session_ctx=session_ctx,
        history=history or [],
        user_jwt=user_jwt,
        home_block_id=home_block_id,
    )
    phase = str(session_ctx.get("routing_phase") or "listening")
    ctx = _routing_ctx(session_ctx, phase=phase, active_intent="discovery.find_activities")
    return reply, ctx, _discovery_routing_stub(phase, "activity_browse"), []


def _start_look_meet_from_discovery(
    *,
    msg: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]] | None,
    user_jwt: str,
    home_block_id: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Begin the meet_seek capture for a find-activities intent and return its first turn.

    Mirrors the deterministic entry in main.py (look_meet_active + reset flags) so the
    sticky flow continues on later turns via the pipeline's look_meet_active gate. The
    find_activities browse path is left intact for explicit entry, just not auto-triggered.
    """
    from app.look_meet import run_look_meet_turn

    session_ctx["look_meet_active"] = True
    session_ctx["look_turns"] = 0
    session_ctx["look_ready"] = None
    session_ctx["look_enrich_count"] = 0
    session_ctx["look_affinity_asked"] = None
    session_ctx["look_draft"] = None
    # Mine the user's own phrasing for a kind (a typed entry, not the generic CTA button).
    session_ctx["look_meet_skip_seed"] = False
    reply = run_look_meet_turn(
        user_message=msg,
        session_ctx=session_ctx,
        history=history or [],
        user_jwt=user_jwt,
        home_block_id=home_block_id,
    )
    phase = str(session_ctx.get("routing_phase") or "listening")
    ctx = _routing_ctx(session_ctx, phase=phase, active_intent="looking.meet")
    return reply, ctx, _discovery_routing_stub(phase, "look_meet"), []


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

    # Classify the turn once (cached for the rest of the turn) BEFORE the host-mode gate,
    # so exiting hosting is semantic — the AI's read that the user is backing out — rather
    # than a fixed cancel-keyword list that "I have mixed feelings" / "I don't wanna" slip past.
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
        if slots_indicate_peer_discovery(slots):
            # During the identity onboarding step the user is describing THEMSELVES
            # ("I'm Asian with a teenager") in answer to "tell me about you" — persist
            # those claims even though the classifier read it as peer discovery. Only
            # suppress background extraction when we're NOT collecting identity.
            if (phase or "") != PHASE_NEED_IDENTITY:
                session_ctx["skip_claims_background_extract"] = True

    # Safety FIRST — inappropriate/abusive content (NSFW, harassment, hate, illegal) is
    # refused before any intent routing, so it can never be captured as a swap/tip, logged
    # as a feature request, or funnelled into find_peers. Detected by the AI router
    # (goal=unsafe) with a regex backstop; refused + logged to moderation_flags, no
    # escalation. Crisis content (self-harm/DV) is intentionally excluded — the orchestrator
    # answers those with empathetic resources, not this flat boundary.
    _unsafe_kind = _unsafe_kind_for_turn(slots, msg)
    if _unsafe_kind is not None:
        return _refuse_unsafe(
            msg=msg, kind=_unsafe_kind, session_ctx=session_ctx,
            user_id=user_id, home_block_id=home_block_id, phase=phase,
        )

    # Sticky event-host mode: once hosting starts, the orchestrator owns the WHOLE
    # conversation so an event line ("weekday playground meet with kids") isn't hijacked
    # into a neighbour search. Release on a semantic back-out (the AI's `abandon` read,
    # not just cancel keywords), an explicit pivot, or the turn cap — so the user is never
    # trapped re-answering the same question.
    if session_ctx.get("event_host_active") and _host_via_orchestrator():
        # While the finished event waits on email verification, the email/OTP turns must
        # reach the signup handlers below (the orchestrator can't parse them) — don't defer.
        host_verifying = bool(session_ctx.get("host_publish_pending")) and phase in (
            PHASE_AWAIT_SIGNUP_PHONE,
            PHASE_AWAIT_SIGNUP_OTP,
        )
        # A pivot is an explicit cross-lane phrase OR the classifier confidently reading the
        # turn as a different intent (AI-driven, not just keywords) — either falls through to
        # the new intent's handler. abandon/cancel with no replacement gets an acknowledgement.
        # The confident-foreign read is suppressed when the message is a valid answer to the
        # host step we're on (a venue name reads as find_activities, a capacity chip as
        # off-lane), so a normal answer no longer releases the flow + drops the draft.
        confident_foreign = _host_confident_foreign(slots) and not _is_host_answer(
            msg, session_ctx, slots
        )
        # An abandon that ALSO surfaces a new request in the same breath ("don't host —
        # find me fun activities instead") is a PIVOT, not a dead stop: fall through so the
        # real handler answers it with a contextual (AI) reply, instead of the canned
        # "no worries" acknowledgement. A bare abandon ("nah forget it") has no follow-on
        # and still gets the acknowledgement. The classifier decides "is there a new
        # intent?" — any clarify signal or a non-vague goal in the same turn.
        _ab_slots = enrich_slots(dict(slots), msg=msg) if slots.get("abandon") else {}
        abandon_with_followon = bool(slots.get("abandon")) and (
            bool(str(_ab_slots.get("clarify") or "").strip())
            or str(_ab_slots.get("goal") or "none") not in ("none", "chat")
        )
        pivoted_away = _pivots_out_of_host(msg) or confident_foreign or abandon_with_followon
        # Seed turn: the "A meet to host" button (or the host-entry regex) JUST entered this
        # flow deterministically, with payload "I want to host a meet" — that 'meet' noun can
        # read as find_activities, so re-classifying the button's own payload would release the
        # flow on turn 1 (the look_meet seed bug, mirrored). The entry is an explicit choice;
        # never release on turn 0 — run the flow. Later turns re-decide intent normally.
        seed_turn = int(session_ctx.get("event_host_turns") or 0) == 0 and not host_verifying
        # Back out when the AI reads the turn as an abandon ("I dont wanna host anything" — no
        # replacement), on a hard cancel word, or on a pivot to another lane. No keyword
        # matching for the back-out — the AI's `abandon` flag is what decides it.
        wants_out = not seed_turn and (
            bool(slots.get("abandon")) or is_signal_cancel(msg) or pivoted_away
        )
        if wants_out:
            _release_host_mode(session_ctx)
            session_ctx["host_released_this_turn"] = True
            # A pivot ("find people") falls through so its target handler answers; a plain
            # back-out gets an explicit acknowledgement, not a silent topic switch.
            if not pivoted_away:
                return (
                    "No worries — we don't have to set up an event. Want to find neighbors, "
                    "see what's happening on your block, or something else?",
                    _routing_ctx(session_ctx, phase="listening", active_intent="none"),
                    _discovery_routing_stub("listening"),
                    [],
                )
        elif host_verifying:
            pass  # fall through to the signup/verify sub-flow handlers
        else:
            turns = int(session_ctx.get("event_host_turns") or 0) + 1
            session_ctx["event_host_turns"] = turns
            if turns <= _EVENT_HOST_TURN_CAP:
                return None  # defer the entire turn to the orchestrator's event flow
            # Cap reached without publishing — release rather than loop.
            _release_host_mode(session_ctx)

    # A guest who hit "Start listening" while building a meet was gated into verify; the
    # moment they come back verified, save the stashed seek (mirrors host publish-after-
    # verify) so they don't have to re-confirm.
    if phone_verified and session_ctx.get("look_seek_pending"):
        from app.look_meet import save_pending_meet_seek

        # Persist the home block at this verify boundary too (same reason as post-verify).
        if not home_block_id:
            _try_assign_home_block(user_jwt, session_ctx=session_ctx, home_block_id=home_block_id)
        zip_code = str(session_ctx.get("zip") or session_ctx.get("zip_code") or "").strip() or None
        pending_reply = save_pending_meet_seek(
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            block_id=resolve_block_id(session_ctx, home_block_id),
            zip_code=zip_code,
        )
        if pending_reply is not None:
            ctx = _routing_ctx(session_ctx, phase="listening", active_intent="looking.meet")
            ctx["look_meet_saved_now"] = session_ctx.get("look_meet_saved_now") or None
            return pending_reply, ctx, _discovery_routing_stub("listening", "look_meet"), []

    # Resolve a pending out-of-scope clarifier BEFORE the lane handlers below, so a reply
    # that pivots to a real intent ("organize it with neighbors") reaches its handler and
    # the pending flag never leaks into a later turn. The reply either confirms the
    # unsupported ask (decline + log) or surfaces a supported intent (fall through).
    if discovery_ai_enabled() and slots and session_ctx.get("out_of_scope_pending"):
        session_ctx["out_of_scope_pending"] = None
        if not _reply_pivots_to_supported(slots, msg):
            return _decline_out_of_scope(
                msg=msg, slots=slots, session_ctx=session_ctx,
                user_id=user_id, home_block_id=home_block_id,
            )
        # else: a supported intent surfaced — fall through to its handler below.

    # Resolve a pending GENERAL clarify (clarify='intent'). The answer re-classifies and
    # routes normally below; we only clear the flag and remember it so we never ask the
    # same question twice in a row (no clarify loops).
    clarify_was_pending = bool(session_ctx.get("clarify_pending"))
    if clarify_was_pending:
        session_ctx["clarify_pending"] = None

    hosting_cta_turn = _try_hosting_cta_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        phase=phase,
    )
    if hosting_cta_turn is not None:
        reply, ctx, routing, peers = hosting_cta_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    if discovery_ai_enabled() and slots and not is_hosting_ui_cta(msg):
        tip_slots = enrich_slots(dict(slots), msg=msg)
        if slots_indicate_tip_share_signal(tip_slots) or utterance_indicates_tip_share(msg):
            tip_turn = _try_signal_lane_turn(
                msg=msg,
                slots=tip_slots,
                session_ctx=session_ctx,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
                home_block_id=home_block_id,
                phase=phase,
            )
            if tip_turn is not None:
                reply, ctx, routing, peers = tip_turn
                ctx["unified_mode"] = True
                return reply, ctx, routing, peers

    if discovery_ai_enabled() and slots and not is_hosting_ui_cta(msg):
        hosting_slots = enrich_slots(dict(slots), msg=msg)
        # AI is the arbiter: enrich_slots already folds in the hosting regex as a
        # low-confidence fallback (reconcile_hosting_peer_slot_conflict). Do NOT OR a
        # raw utterance regex here — that lets "wanna find ... event" hijack a confident
        # discovery classification into the create-event flow.
        if slots_indicate_hosting_signal(hosting_slots):
            # Hosting an event is a full create_event flow (what/where/when/affinity →
            # publish). When the orchestrator is on it owns this in-chat (OpenAI), so
            # defer to it and pin host mode so the follow-up turns stay with it.
            if _host_via_orchestrator():
                session_ctx["event_host_active"] = True
                session_ctx["event_host_turns"] = int(session_ctx.get("event_host_turns") or 0) + 1
                return None
            hosting_turn = _try_signal_lane_turn(
                msg=msg,
                slots=hosting_slots,
                session_ctx=session_ctx,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
                home_block_id=home_block_id,
                phase=phase,
            )
            if hosting_turn is not None:
                reply, ctx, routing, peers = hosting_turn
                ctx["unified_mode"] = True
                return reply, ctx, routing, peers

    if discovery_ai_enabled() and slots and not is_hosting_ui_cta(msg):
        block_log_slots = enrich_slots(dict(slots), msg=msg)
        block_log_turn = _try_show_block_log_turn(
            msg=msg,
            slots=block_log_slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
        )
        if block_log_turn is not None:
            reply, ctx, routing, peers = block_log_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

    # Out-of-scope fresh detection (AI-driven): the user asked Lana to do something TagAlng
    # has no feature for (deliver food, book a taxi). Confidence-gated — a clear ask is
    # declined + logged immediately; an ambiguous one asks one clarifying question. This
    # stops errands silently funnelling into find_peers. (The reply to that clarifier is
    # resolved earlier, right after the funnel guards, so a pivot reaches its real handler.)
    if discovery_ai_enabled() and slots and not is_hosting_ui_cta(msg):
        _oos = _out_of_scope_decision(slots, msg)
        if _oos == "clarify":
            _oos_enriched = enrich_slots(dict(slots), msg=msg)
            return _ask_out_of_scope(
                session_ctx,
                question=str(_oos_enriched.get("clarify_question") or ""),
                options=list(_oos_enriched.get("clarify_options") or []),
                detail=str(_oos_enriched.get("signal_detail") or "").strip(),
            )
        if _oos == "decline":
            return _decline_out_of_scope(
                msg=msg, slots=slots, session_ctx=session_ctx,
                user_id=user_id, home_block_id=home_block_id,
            )

    # General uncertainty gate (ASK-WHEN-UNSURE) — the classifier could not confidently
    # place this turn in a supported lane, so ask the one AI-written question (grounded in
    # what TagAlng can do) instead of guessing or silently funnelling into find_peers. Never
    # on the turn that just answered a clarify (no loops); never for hosting CTAs.
    if (
        discovery_ai_enabled()
        and slots
        and not is_hosting_ui_cta(msg)
        and not clarify_was_pending
        and str(slots.get("clarify") or "") == "intent"
    ):
        _clar = enrich_slots(dict(slots), msg=msg)
        return _ask_general_clarify(
            session_ctx,
            question=str(_clar.get("clarify_question") or ""),
            options=list(_clar.get("clarify_options") or []),
        )

    # Browse-vs-seek router (AI-driven), before the ZIP funnel:
    #   browse  → agentic "what's happening" events browse (show real events, refine)
    #   seek    → look_meet capture ("what kind of meet?" → match to a host)
    #   clarify → one-tap question when the AI genuinely can't tell
    if discovery_ai_enabled() and slots and not is_hosting_ui_cta(msg):
        # Resolve a pending clarifier answer first (always lands on browse or seek).
        if session_ctx.get("browse_or_meet_pending"):
            session_ctx["browse_or_meet_pending"] = None
            if _resolve_browse_or_meet_answer(msg, slots) == "seek":
                return _start_look_meet_from_discovery(
                    msg=msg, session_ctx=session_ctx, history=history,
                    user_jwt=user_jwt, home_block_id=home_block_id,
                )
            return _start_activity_browse_from_discovery(
                msg=msg, session_ctx=session_ctx, history=history,
                user_jwt=user_jwt, home_block_id=home_block_id,
            )
        _decision = _browse_or_seek_decision(slots, msg)
        if _decision == "browse":
            return _start_activity_browse_from_discovery(
                msg=msg, session_ctx=session_ctx, history=history,
                user_jwt=user_jwt, home_block_id=home_block_id,
            )
        if _decision == "clarify":
            _bm = enrich_slots(dict(slots), msg=msg)
            return _ask_browse_or_meet(
                session_ctx,
                question=str(_bm.get("clarify_question") or ""),
                options=list(_bm.get("clarify_options") or []),
            )
        # A clear meet_seek falls through to the existing signal-capture flow below.

    if discovery_ai_enabled() and slots and not is_hosting_ui_cta(msg):
        seek_turn = _try_signal_seek_early_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
        )
        if seek_turn is not None:
            reply, ctx, routing, peers = seek_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

    meta_turn = _try_meta_chat_turn(
        msg=msg, session_ctx=session_ctx, phase=phase, phone_verified=phone_verified
    )
    if meta_turn is not None:
        reply, ctx, routing, peers = meta_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    if session_ctx.get("pending_heritage_change") and _message_bypasses_heritage_pending(
        msg, slots
    ):
        session_ctx.pop("pending_heritage_change", None)
        session_ctx.pop("skip_heritage_background_extract", None)

    # If the user asked to sign up while phone is unverified, latch it
    # so the next step that needs ZIP can still switch into the phone-gate UI.
    if not phone_verified and _turn_wants_signup_gate(msg, slots, session_ctx):
        session_ctx["pending_signup_gate"] = True

    # ── Mid-signup escape: never silently pin a user at the email/OTP step. A bare answer
    #    (the code at the OTP step, an email at the email step) is consumed by the step
    #    handler below. A login pivot is CONFIRMED before we abandon signup; an abandon
    #    ("forget it") releases gracefully. ──
    if _signup_verify_in_flight(session_ctx, phase):
        answering_step = (
            (phase == PHASE_AWAIT_SIGNUP_OTP and bool(extract_otp_code(msg)))
            or (phase == PHASE_AWAIT_SIGNUP_PHONE and bool(extract_email(msg)))
        )
        pending_switch = str(session_ctx.get("pending_lane_switch") or "")
        if pending_switch == "login":
            session_ctx["pending_lane_switch"] = None
            if not answering_step and _is_affirmative(msg):
                for _k in ("signup_phone", "pending_signup_gate",
                           "requires_phone_verification", "pending_post_verify"):
                    session_ctx[_k] = None
                login = handle_guest_login(msg, step="early_chat", session_ctx=session_ctx)
                if login:
                    reply, ctx = login
                    ctx["unified_mode"] = True
                    return reply, ctx, {"outcome": "A", "intent_class": "auth", "confidence": 1.0}, []
            # Declined the switch (or just answered the step) → resume signup below.
        elif not answering_step and slots and slots.get("abandon"):
            for _k in ("signup_phone", "pending_signup_gate",
                       "requires_phone_verification", "pending_post_verify",
                       "pending_lane_switch"):
                session_ctx[_k] = None
            return (
                "No problem — we can finish signing up anytime. What would you like to do?",
                _routing_ctx(session_ctx, phase="listening", active_intent="none"),
                _discovery_routing_stub("listening"),
                [],
            )
        elif not answering_step and _turn_wants_login(msg, slots, session_ctx):
            session_ctx["pending_lane_switch"] = "login"
            keep = (
                "enter the code to keep signing up"
                if phase == PHASE_AWAIT_SIGNUP_OTP
                else "send your email to keep signing up"
            )
            return (
                f"You're in the middle of signing up — switch to signing in instead? "
                f"Say yes, or {keep}.",
                _routing_ctx(
                    session_ctx, phase=phase, signup_phone=session_ctx.get("signup_phone")
                ),
                _discovery_routing_stub(phase),
                [],
            )

    # Continue signup verify sub-flow
    if phase == PHASE_AWAIT_SIGNUP_PHONE:
        return _handle_signup_phone_message(msg, session_ctx, is_anonymous=is_anonymous)

    # Sessions stuck on preview with requires_phone_verification (orchestrator lag).
    if (
        not phone_verified
        and session_ctx.get("requires_phone_verification")
        and extract_email(msg)
    ):
        return _handle_signup_phone_message(msg, session_ctx, is_anonymous=is_anonymous)

    if phase == PHASE_AWAIT_SIGNUP_OTP:
        otp = extract_otp_code(msg)
        email = str(session_ctx.get("signup_phone") or "")
        if not otp:
            return (
                f"Enter the 6-digit code we sent to {email or 'your email'}.",
                _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_OTP, signup_phone=email or None),
                _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
                [],
            )
        ctx = _routing_ctx(session_ctx, phase=PHASE_PREVIEW, signup_phone=email)
        ctx["pending_post_verify"] = True
        ctx["requires_phone_verification"] = False
        ctx.pop("pending_signup_gate", None)
        ctx["auth_action"] = _auth_action(
            type="verify_signup_otp",
            email=email,
            token=otp,
            verify_type="email_change",
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

    change_name_turn = _try_change_name_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_id=user_id,
        phase=phase,
    )
    if change_name_turn is not None:
        reply, ctx, routing, peers = change_name_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    identity_slots_turn = _try_identity_slots_turn(
        msg=msg,
        slots=slots,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        phase=phase,
        user_id=user_id,
    )
    if identity_slots_turn is not None:
        reply, ctx, routing, peers = identity_slots_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    phrase_turn = _try_phrase_policy_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        phase=phase,
        user_id=user_id,
    )
    if phrase_turn is not None:
        reply, ctx, routing, peers = phrase_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    respond_turn = _try_dismiss_intro_pass_turn(
        msg=msg,
        session_ctx=session_ctx,
        phone_verified=phone_verified,
        phase=phase,
    )
    if respond_turn is not None:
        reply, ctx, routing, peers = respond_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    respond_turn = _try_respond_nudge_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        phase=phase,
    )
    if respond_turn is not None:
        reply, ctx, routing, peers = respond_turn
        ctx["unified_mode"] = True
        return reply, ctx, routing, peers

    if phone_verified and discovery_ai_enabled() and slots:
        slots_intro_turn = _try_slots_intro_turn(
            msg=msg,
            slots=slots,
            session_ctx=session_ctx,
            ctx_base=dict(session_ctx),
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
            history=history,
        )
        if slots_intro_turn is not None:
            reply, ctx, routing, peers = slots_intro_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

    if phone_verified and wants_neighbor_intro(msg) and not discovery_ai_enabled():
        intro_block = resolve_block_id(session_ctx, home_block_id)
        block_intro_turn = _try_block_log_intro_turn(
            msg=msg,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
            history=history,
        )
        if block_intro_turn is not None:
            reply, ctx, routing, peers = block_intro_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers
        if intro_block:
            intro_turn = _try_neighbor_intro_turn(
                msg=msg,
                session_ctx=session_ctx,
                ctx_base=dict(session_ctx),
                user_jwt=user_jwt,
                block_id=intro_block,
                phone_verified=phone_verified,
                goal=str((slots or {}).get("goal") or "none"),
                slots=slots,
                history=history,
            )
            if intro_turn is not None:
                reply, ctx, routing, peers = intro_turn
                ctx["unified_mode"] = True
                return reply, ctx, routing, peers

    list_turn = _try_list_intros_turn(
        msg=msg,
        slots=slots or {},
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

    if discovery_ai_enabled() and slots:
        enriched_slots = enrich_slots(dict(slots), msg=msg)
        signal_turn = _try_signal_lane_turn(
            msg=msg,
            slots=enriched_slots,
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

        tip_turn = _try_tip_seek_fast_turn(
            msg=msg,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
            slots=slots,
        )
        if tip_turn is not None:
            reply, ctx, routing, peers = tip_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        swap_followup_turn = _try_swap_block_log_followup_turn(
            msg=msg,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
            history=history,
            slots=enriched_slots,
        )
        if swap_followup_turn is not None:
            reply, ctx, routing, peers = swap_followup_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        layer1_turn = _try_layer1_intent_turn(
            msg=msg,
            slots=enriched_slots,
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

        attr_refine_turn = _try_attr_refine_turn(
            msg=msg,
            slots=enriched_slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
        )
        if attr_refine_turn is not None:
            reply, ctx, routing, peers = attr_refine_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        trait_turn = _try_peer_trait_question_turn(
            msg=msg,
            session_ctx=session_ctx,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            phase=phase,
        )
        if trait_turn is not None:
            reply, ctx, routing, peers = trait_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        peer_detail_turn = _try_peer_detail_turn(
            msg=msg,
            slots=enriched_slots,
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

        block_nudge_turn = _try_block_log_nudge_turn(
            msg=msg,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
            history=history,
        )
        if block_nudge_turn is not None:
            reply, ctx, routing, peers = block_nudge_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

        block_log_turn = _try_show_block_log_turn(
            msg=msg,
            slots=enriched_slots,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            phase=phase,
        )
        if block_log_turn is not None:
            reply, ctx, routing, peers = block_log_turn
            ctx["unified_mode"] = True
            return reply, ctx, routing, peers

    heritage_pending_turn = _try_pending_heritage_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_id=user_id,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        phase=phase,
        slots=slots,
    )
    if heritage_pending_turn is not None:
        reply, ctx, routing, peers = heritage_pending_turn
        ctx["unified_mode"] = True
        ctx["skip_claims_background_extract"] = True
        return reply, ctx, routing, peers

    heritage_conflict_turn = _try_heritage_conflict_turn(
        msg=msg,
        session_ctx=session_ctx,
        user_id=user_id,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        phase=phase,
        slots=slots,
    )
    if heritage_conflict_turn is not None:
        reply, ctx, routing, peers = heritage_conflict_turn
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
        email = str(session_ctx.get("signup_phone") or "your email")
        if phase == PHASE_AWAIT_SIGNUP_OTP:
            return (
                f"You're signing up — enter the 6-digit code I sent to {email}.",
                _routing_ctx(
                    session_ctx,
                    phase=PHASE_AWAIT_SIGNUP_OTP,
                    signup_phone=session_ctx.get("signup_phone"),
                ),
                _discovery_routing_stub(PHASE_AWAIT_SIGNUP_OTP),
                [],
            )
        return (
            "You're in the middle of signing up — what's the email for your account?",
            _routing_ctx(session_ctx, phase=PHASE_AWAIT_SIGNUP_PHONE),
            _discovery_routing_stub(PHASE_AWAIT_SIGNUP_PHONE),
            [],
        )

    # ── Mid-login escape (mirror of the signup case): if the user is in the login sub-flow
    #    and asks to CREATE a new account, confirm before abandoning sign-in. A bare email /
    #    OTP answering the current login step is consumed normally, never treated as a pivot. ──
    if _login_flow_active(session_ctx):
        login_answering = bool(extract_email(msg)) or bool(extract_otp_code(msg))
        pending_switch = str(session_ctx.get("pending_lane_switch") or "")
        if pending_switch == "signup":
            session_ctx["pending_lane_switch"] = None
            if not login_answering and _is_affirmative(msg):
                ctx = _exit_login_ctx(session_ctx)
                ctx["routing_phase"] = PHASE_AWAIT_SIGNUP_PHONE
                ctx["requires_phone_verification"] = True
                ctx["pending_signup_gate"] = True
                return (
                    "Great — let's create your account. What's your email?",
                    ctx,
                    _discovery_routing_stub(PHASE_AWAIT_SIGNUP_PHONE),
                    [],
                )
            # Declined the switch (or just answered the login step) → resume login below.
        elif (
            not login_answering
            and not phone_verified
            and _turn_wants_signup_gate(msg, slots, session_ctx)
        ):
            session_ctx["pending_lane_switch"] = "signup"
            login_phase = str(session_ctx.get("routing_phase") or "await_login_phone")
            return (
                "You're signing in right now — switch to creating a NEW account instead? "
                "Say yes, or enter your login details to keep signing in.",
                _routing_ctx(session_ctx, phase=login_phase),
                _discovery_routing_stub(login_phase),
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

    hosting_guard = enrich_slots(dict(slots or {}), msg=msg)
    if slots_indicate_hosting_signal(hosting_guard):
        return None

    if wants_host_activity(msg) and not wants_peer_find(msg) and str(slots.get("goal") or "") not in (
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
        # Off-ramp: user is declining the ZIP — don't re-prompt the same question forever.
        # Skip the off-ramp when the AI says they still want discovery (e.g. "find me
        # people, stop asking questions" is frustration, not a refusal to proceed).
        _decline_goal = str((slots or {}).get("goal") or "")
        if _declines_to_answer(msg) and _decline_goal not in (
            "peers",
            "activities",
            "both",
            "continue",
            "save_signal",
            "verify",
        ):
            off = _routing_ctx(session_ctx, phase="listening", active_intent="none")
            off["discovery_goal"] = None
            return (
                "No problem — we can skip that for now. Tell me what you're looking for, "
                "or share your ZIP whenever you're ready and I'll find your block.",
                off,
                _discovery_routing_stub("listening", "zip_declined"),
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

    if goal == "activities":
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
        if discovery_ai_enabled() and slots:
            slots_intro_turn = _try_slots_intro_turn(
                msg=msg,
                slots=slots,
                session_ctx=session_ctx,
                ctx_base=ctx_base,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
                home_block_id=home_block_id,
                phase=phase,
                history=history,
            )
            if slots_intro_turn is not None:
                return slots_intro_turn
        elif wants_neighbor_intro(msg):
            block_intro_turn = _try_block_log_intro_turn(
                msg=msg,
                session_ctx=session_ctx,
                user_jwt=user_jwt,
                phone_verified=phone_verified,
                phase=phase,
                history=history,
            )
            if block_intro_turn is not None:
                return block_intro_turn
            intro_turn = _try_neighbor_intro_turn(
                msg=msg,
                session_ctx=session_ctx,
                ctx_base=ctx_base,
                user_jwt=user_jwt,
                block_id=block_id,
                phone_verified=phone_verified,
                goal=effective_goal,
                slots=slots,
                history=history,
            )
            if intro_turn is not None:
                return intro_turn

    # Post-verify funnel before verify gate — JWT may lag one turn after OTP.
    if ctx_base.get("pending_post_verify") or phase == PHASE_NEED_DISPLAY_NAME:
        # Persist the home block the MOMENT the user is verified — before the name/identity
        # sub-steps — so someone who pivots away mid-onboarding (e.g. to browse activities)
        # still has a block, instead of "Nothing on your block" forever. Idempotent + cheap
        # (no-op once home_block_id exists).
        if phone_verified and not home_block_id:
            _try_assign_home_block(user_jwt, session_ctx=ctx_base, home_block_id=home_block_id)
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
    if phase == PHASE_PREVIEW and slots_want_preview_refetch(slots, session_ctx, msg=msg):
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

    if goal in ("chat", "none", "save_signal"):
        return None

    if phase != PHASE_PREVIEW or wants_peers or wants_more_peer_detail(msg):
        if _peer_find_turn_blocked(slots, msg=msg, session_ctx=session_ctx, history=history):
            return None
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
