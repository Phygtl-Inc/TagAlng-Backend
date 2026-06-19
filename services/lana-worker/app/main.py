import logging
import os
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.auth import (
    AuthSession,
    require_home_block_for_purpose,
    service_client,
    verify_auth,
    verify_jwt,
)
from app.context import (
    format_event_draft_context,
    format_user_context,
    load_event_draft_context,
    load_user_context,
)
from app.event_context import host_display_name
from app.db import (
    complete_session,
    create_session,
    embed_message_by_id,
    get_session_for_user,
    insert_message,
    list_messages,
    transcript_text,
    update_session_context,
    merge_session_context,
)
from app.local_signals import block_log_take_action, fetch_my_block_log, normalize_block_log_row
from app.lana_paths import (
    event_fast_path_enabled,
    profile_fast_path_enabled,
    use_orchestrator_for_purpose,
)
from app.profile_intake import (
    format_profile_intake_context,
    lana_profile_guest_opening,
    lana_profile_opening,
    lana_profile_turn,
)
from app.turn_timing import TurnTimer
from app.event_publish import publish_event
from app.guest_intake import lana_profile_guest_turn
from app.lana_dispatch import lana_unified_opening, lana_unified_turn
from app.lana_unified_pipeline import run_lana_unified_pipeline
from app.discovery_route import looks_like_host_event_entry
from app.pass_along import looks_like_pass_along_entry
from app.tip_share import looks_like_tip_share_entry
from app.models import (
    ActivityPreviewRow,
    AuthActionPayload,
    BlockLogActionRequest,
    BlockLogEntryRow,
    BlockLogListResponse,
    CompleteSessionRequest,
    CompleteSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    DiscoverySurfacePayload,
    DiscoveryWeakPeerRow,
    EventDraft,
    ItemDraft,
    ProfilePhotoUploadResponse,
    SignalPhotoUploadResponse,
    TipDraft,
    ExtractedClaim,
    HighlightSpan,
    JointMomentCandidate,
    JointMomentPayload,
    IdentityClaimRow,
    IdentityProfilePayload,
    IntroProposalPayload,
    PendingIntroRow,
    SignalSavedPayload,
    HostingDraftPayload,
    TipDraftPayload,
    UiActionRow,
    LanaTurnUi,
    PeerMatchRow,
    SendMessageRequest,
    SendMessageResponse,
    SessionDetailResponse,
    TurnDebug,
    TurnRouting,
)
from app.orchestrator import orchestrator_enabled, run_opening, run_turn
from app.orchestrator.llm import llm_configured, openai_configured, provider, router_model, synthesizer_model, vertex_configured
from app.orchestrator.extract import (
    claude_extract_event_from_transcript,
    claude_extract_profile_from_transcript,
)
from app.vertex_event import lana_event_opening, lana_event_turn
from app.vertex_event_extract import vertex_extract_event_from_transcript
from app.claims_persist import (
    extract_and_upsert_claims_from_message,
    persist_nickname_if_stated,
    replace_all_claims,
    should_extract_claims_from_message,
)
from app.profile_photo import upload_profile_photo_bytes
from app.signal_photo import upload_signal_photo_bytes
from app.ui_actions import derive_ui_actions
from app.ui_intent import (
    PEER_DISCOVERY_ACTIVE_INTENTS,
    PEER_SURFACE_UI_INTENTS,
    UI_INTENT_OFFER_NEIGHBOR_INTRO,
    UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
    UI_INTENT_RESPOND_PENDING_INTRO,
    UI_INTENT_SHOW_ACTIVITY_PREVIEW,
    UI_INTENT_SHOW_BLOCK_LOG,
    UI_INTENT_SHOW_IDENTITY_PROFILE,
    UI_INTENT_SHOW_PENDING_INTROS,
    UI_INTENT_SIGNAL_SAVED,
    derive_ui_intent,
)
from app.vertex_extract import vertex_extract_from_transcript
from app.vertex_lana import lana_opening, lana_turn

_LOG = logging.getLogger(__name__)

app = FastAPI(title="TagAlng lana-worker", version="0.5.4")

_cors_raw = os.environ.get("CORS_ALLOW_ORIGINS", "*").strip()
_cors_origins = ["*"] if _cors_raw == "*" else [o.strip() for o in _cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _vertex_configured() -> bool:
    return vertex_configured()


def _vertex_required() -> None:
    if not _vertex_configured():
        raise HTTPException(
            status_code=503,
            detail="vertex_not_configured_set_GCP_VERTEX_PROJECT",
        )


def _vertex_error_detail(prefix: str, exc: Exception) -> str:
    msg = str(exc).replace("\n", " ")[:500]
    return f"{prefix}:{type(exc).__name__}:{msg}"


def _timing_total_ms(timing: dict[str, int]) -> int:
    return sum(
        v for k, v in timing.items() if k != "total_ms" and not k.endswith("_attempts")
    )


def _event_routing_stub() -> dict[str, Any]:
    return {
        "outcome": "R",
        "intent_class": "activity",
        "confidence": 1.0,
        "tool_to_call": None,
        "capture_fired": False,
        "event_fast_path": True,
    }


def _profile_routing_stub() -> dict[str, Any]:
    return {
        "outcome": "R",
        "intent_class": "identity",
        "confidence": 1.0,
        "tool_to_call": None,
        "capture_fired": False,
        "profile_fast_path": True,
    }


def _use_orchestrator() -> bool:
    return orchestrator_enabled()


def _load_lana_context_pack(
    user_id: str,
    purpose: str,
    *,
    timer: TurnTimer | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Full context for legacy profile; minimal for fast paths."""
    if purpose == "event_draft":
        stage = timer.stage("load_event_context") if timer else None
        if stage:
            with stage:
                ctx_pack = load_event_draft_context(user_id)
        else:
            ctx_pack = load_event_draft_context(user_id)
        user_block = format_event_draft_context(ctx_pack)
    elif purpose == "profile_intake" and profile_fast_path_enabled():
        stage = timer.stage("load_profile_context") if timer else None
        if stage:
            with stage:
                ctx_pack = load_event_draft_context(user_id)
        else:
            ctx_pack = load_event_draft_context(user_id)
        user_block = format_profile_intake_context(ctx_pack)
    else:
        stage = timer.stage("load_user_context") if timer else None
        if stage:
            with stage:
                ctx_pack = load_user_context(user_id)
        else:
            ctx_pack = load_user_context(user_id)
        user_block = format_user_context(ctx_pack, purpose)
    purpose_ids = ctx_pack.get("event_purpose_ids") or []
    return ctx_pack, user_block, purpose_ids


def _legacy_lana_turn(
    *,
    purpose: str,
    user_block: str,
    purpose_ids: list[str],
    history: list[dict[str, Any]],
    user_message: str,
    prev_draft: dict[str, Any] | None,
    timer: TurnTimer,
    user_id: str | None = None,
    ctx_pack: dict[str, Any] | None = None,
    session_ctx: dict[str, Any] | None = None,
    session_id: str | None = None,
    user_jwt: str | None = None,
    auth: AuthSession | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    if purpose == "event_draft":
        reply, status, session_ctx, ui_raw, draft_raw = lana_event_turn(
            user_block,
            purpose_ids,
            history,
            user_message,
            prev_draft,
            timer=timer,
        )
        session_ctx["last_routing"] = _event_routing_stub()
        return reply, status, session_ctx, ui_raw, draft_raw
    if purpose == "lana" and auth and user_jwt:
        reply, status, turn_ctx, ui_raw, peers = lana_unified_turn(
            history=history,
            user_message=user_message,
            session_ctx=session_ctx or {},
            user_jwt=user_jwt,
            phone_verified=auth.phone_verified,
            home_block_id=auth.home_block_id,
            is_anonymous=auth.is_anonymous,
        )
        if peers:
            turn_ctx["peer_matches"] = peers
        else:
            turn_ctx["peer_matches"] = []
        return reply, status, turn_ctx, ui_raw, None
    if purpose == "profile_intake":
        sess_ctx = session_ctx or {}
        guest_flow = bool(
            auth
            and session_id
            and user_jwt
            and sess_ctx.get("last_status") != "completed"
            and (auth.is_anonymous or sess_ctx.get("guest_intake"))
        )
        if guest_flow:
            reply, status, turn_ctx, ui_raw, _jm = lana_profile_guest_turn(
                user_block=user_block,
                history=history,
                user_message=user_message,
                session_ctx=session_ctx or {},
                session_id=session_id,
                user_jwt=user_jwt,
                phone_verified=auth.phone_verified,
                home_block_id=auth.home_block_id,
                ctx_pack=ctx_pack,
                timer=timer,
            )
        else:
            reply, status, turn_ctx, ui_raw = lana_profile_turn(
                user_block,
                history,
                user_message,
                ctx_pack=ctx_pack,
                session_ctx=session_ctx,
                timer=timer,
            )
        patch = turn_ctx.pop("profile_patch", None)
        if patch and user_id:
            _persist_profile_patch(user_id, patch)
        turn_ctx["last_routing"] = _profile_routing_stub()
        return reply, status, turn_ctx, ui_raw, None
    reply, status, session_ctx, ui_raw = lana_turn(
        user_block,
        purpose,
        history,
        user_message,
    )
    return reply, status, session_ctx, ui_raw, session_ctx.get("event_draft")


def _routing_from_ctx(ctx: dict[str, Any]) -> TurnRouting | None:
    raw = ctx.get("last_routing")
    if not isinstance(raw, dict):
        return None
    return TurnRouting(
        outcome=raw.get("outcome"),
        intent_class=raw.get("intent_class"),
        confidence=raw.get("confidence"),
        tool_called=raw.get("tool_to_call"),
        capture_fired=bool(raw.get("capture_fired")),
    )


def _turn_debug_from_ctx(
    ctx: dict[str, Any],
    *,
    ui_intent: str | None,
    orchestrator: bool,
) -> TurnDebug:
    """Surface why this turn routed the way it did (slots + handler + phase)."""
    slots = ctx.get("_discovery_slots")
    slots = slots if isinstance(slots, dict) else None
    routing = ctx.get("last_routing")
    routing = routing if isinstance(routing, dict) else {}
    confidence = None
    if slots and slots.get("confidence") is not None:
        try:
            confidence = float(slots.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
    return TurnDebug(
        intent=(slots or {}).get("linear_intent"),
        goal=(slots or {}).get("goal"),
        confidence=confidence,
        signal_intent=(slots or {}).get("signal_intent"),
        active_intent=ctx.get("active_intent"),
        routing_phase=ctx.get("routing_phase"),
        ui_intent=ui_intent,
        handler=routing.get("tool_to_call"),
        orchestrator=orchestrator,
        event_host_active=bool(ctx.get("event_host_active")),
        slots={k: v for k, v in (slots or {}).items() if not k.startswith("_")} or None,
    )


def _ui_from_dict(raw: dict[str, Any]) -> LanaTurnUi:
    highlights = [
        HighlightSpan(text=h["text"], bucket=h.get("bucket") or "general")
        for h in raw.get("highlights") or []
        if h.get("text")
    ]
    return LanaTurnUi(
        bucket=raw.get("bucket"),
        focus_phrase=raw.get("focus_phrase"),
        highlights=highlights,
    )


def _draft_from_dict(raw: dict[str, Any] | None) -> EventDraft | None:
    if not raw:
        return None
    return EventDraft(**raw)


def _item_draft_from_dict(raw: dict[str, Any] | None) -> ItemDraft | None:
    if not raw or not isinstance(raw, dict):
        return None
    fields = set(ItemDraft.model_fields)
    return ItemDraft(**{k: v for k, v in raw.items() if k in fields})


def _tip_draft_from_dict(raw: dict[str, Any] | None) -> TipDraft | None:
    if not raw or not isinstance(raw, dict):
        return None
    fields = set(TipDraft.model_fields)
    return TipDraft(**{k: v for k, v in raw.items() if k in fields})


def _joint_moment_from_dict(raw: dict[str, Any] | None) -> JointMomentPayload | None:
    if not raw or not isinstance(raw, dict):
        return None
    card = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    return JointMomentPayload(
        joint_moment_id=str(raw.get("joint_moment_id") or "") or None,
        status=str(raw.get("status") or "") or None,
        candidate=JointMomentCandidate(
            user_id=str(card.get("user_id") or "") or None,
            nickname=str(card.get("nickname") or "") or None,
            avatar_url=str(card.get("avatar_url") or "") or None,
        ),
        lana_copy=str(raw.get("lana_copy") or "") or None,
        match_reason=str(raw.get("match_reason") or "") or None,
        is_demo=bool(raw.get("is_demo")),
    )


def _intro_proposal_from_dict(raw: dict[str, Any] | None) -> IntroProposalPayload | None:
    if not raw or not isinstance(raw, dict):
        return None
    dims = raw.get("shared_dimensions")
    if not isinstance(dims, list):
        dims = []
    return IntroProposalPayload(
        intro_id=str(raw.get("intro_id") or "") or None,
        nudge_id=str(raw.get("nudge_id") or "") or None,
        candidate_user_id=str(raw.get("candidate_user_id") or "") or None,
        candidate_nickname=str(raw.get("candidate_nickname") or "") or None,
        matching_peer_label=str(raw.get("matching_peer_label") or "") or None,
        match_reason=str(raw.get("match_reason") or "") or None,
        shared_dimensions=[str(d) for d in dims[:8]],
        status=str(raw.get("status") or "") or None,
    )


def _ui_actions_from_ctx(ctx: dict[str, Any], ui_intent: str) -> list[UiActionRow]:
    out: list[UiActionRow] = []
    for row in derive_ui_actions(ctx, ui_intent):
        if not isinstance(row, dict) or not row.get("id"):
            continue
        out.append(
            UiActionRow(
                id=str(row["id"]),
                label=str(row.get("label") or ""),
                message=str(row.get("message") or ""),
                style=row.get("style") or "primary",
                intro_id=str(row.get("intro_id") or "") or None,
                peer_user_id=str(row.get("peer_user_id") or "") or None,
            )
        )
    return out


def _pending_intros_from_ctx(ctx: dict[str, Any]) -> list[PendingIntroRow]:
    raw = ctx.get("pending_intros")
    if not isinstance(raw, list):
        return []
    out: list[PendingIntroRow] = []
    for row in raw[:12]:
        if not isinstance(row, dict):
            continue
        dims = row.get("shared_dimensions")
        if not isinstance(dims, list):
            dims = []
        actions_raw = row.get("actions")
        actions: list[UiActionRow] = []
        if isinstance(actions_raw, list):
            actions = _ui_action_rows_from_raw(actions_raw)
        out.append(
            PendingIntroRow(
                intro_id=str(row.get("intro_id") or "") or None,
                other_user_id=str(row.get("other_user_id") or "") or None,
                nickname=str(row.get("nickname") or "") or None,
                avatar_url=str(row.get("avatar_url") or "") or None,
                created_at=str(row.get("created_at") or "") or None,
                expires_at=str(row.get("expires_at") or "") or None,
                status=str(row.get("status") or "") or None,
                match_reason=str(row.get("match_reason") or "") or None,
                shared_dimensions=[str(d) for d in dims[:8]],
                direction=str(row.get("direction") or "") or None,
                actions=actions,
            )
        )
    return out


def _auth_action_from_ctx(ctx: dict[str, Any]) -> AuthActionPayload | None:
    raw = ctx.get("auth_action")
    if not isinstance(raw, dict) or not raw.get("type"):
        return None
    return AuthActionPayload(
        type=str(raw["type"]),
        phone=str(raw.get("phone") or "") or None,
        token=str(raw.get("token") or "") or None,
        verify_type=str(raw.get("verify_type") or "") or None,
    )


def _ui_action_rows_from_raw(actions_raw: Any) -> list[UiActionRow]:
    actions: list[UiActionRow] = []
    if not isinstance(actions_raw, list):
        return actions
    for act in actions_raw[:4]:
        if not isinstance(act, dict) or not act.get("id"):
            continue
        actions.append(
            UiActionRow(
                id=str(act["id"]),
                label=str(act.get("label") or ""),
                message=str(act.get("message") or ""),
                style=act.get("style") or "primary",
                intro_id=str(act.get("intro_id") or "") or None,
                peer_user_id=str(act.get("peer_user_id") or "") or None,
            )
        )
    return actions


def _discovery_surface_from_ctx(ctx: dict[str, Any]) -> DiscoverySurfacePayload | None:
    raw = ctx.get("discovery_surface")
    if not isinstance(raw, dict):
        return None
    weak_raw = raw.get("weak_peer")
    weak_peer: DiscoveryWeakPeerRow | None = None
    if isinstance(weak_raw, dict):
        weak_peer = DiscoveryWeakPeerRow(
            peer_user_id=str(weak_raw.get("peer_user_id") or "") or None,
            nickname=str(weak_raw.get("nickname") or "") or None,
            match_stars=weak_raw.get("match_stars"),
            match_badge=str(weak_raw.get("match_badge") or "") or None,
        )
    return DiscoverySurfacePayload(
        strong_count=int(raw.get("strong_count") or 0),
        partial_count=int(raw.get("partial_count") or 0),
        weak_count=int(raw.get("weak_count") or 0),
        status_label=str(raw.get("status_label") or "") or None,
        weak_peer=weak_peer,
        ranked_summary=str(raw.get("ranked_summary") or "") or None,
    )


def _peer_matches_from_ctx(ctx: dict[str, Any]) -> list[PeerMatchRow]:
    raw = ctx.get("peer_matches")
    if not isinstance(raw, list):
        return []
    out: list[PeerMatchRow] = []
    for row in raw[:8]:
        if not isinstance(row, dict):
            continue
        tags = row.get("trait_tags")
        if not isinstance(tags, list):
            tags = []
        out.append(
            PeerMatchRow(
                peer_user_id=str(row.get("peer_user_id") or "") or None,
                nickname=str(row.get("nickname") or "") or None,
                avatar_url=str(row.get("avatar_url") or "") or None,
                similarity_score=row.get("similarity_score"),
                matching_peer_label=str(row.get("matching_peer_label") or "") or None,
                matching_peer_concept=str(row.get("matching_peer_concept") or "") or None,
                has_exact_concept_match=bool(row.get("has_exact_concept_match")),
                preview=bool(row.get("preview")),
                match_stars=row.get("match_stars"),
                match_band=str(row.get("match_band") or "") or None,
                match_badge=str(row.get("match_badge") or "") or None,
                trait_tags=[str(t) for t in tags[:6]],
                actions=_ui_action_rows_from_raw(row.get("actions")),
            )
        )
    return out


def _activity_previews_from_ctx(ctx: dict[str, Any]) -> list[ActivityPreviewRow]:
    raw = ctx.get("activity_previews")
    if not isinstance(raw, list):
        return []
    out: list[ActivityPreviewRow] = []
    for row in raw[:8]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        out.append(
            ActivityPreviewRow(
                title=title,
                starts_at=str(row.get("starts_at") or "") or None,
                starts_label=str(row.get("starts_label") or "") or None,
                venue_name=str(row.get("venue_name") or "") or None,
                preview=bool(row.get("preview", True)),
            )
        )
    return out


def _block_log_from_ctx(ctx: dict[str, Any]) -> list[BlockLogEntryRow]:
    raw = ctx.get("block_log_entries")
    if not isinstance(raw, list):
        return []
    out: list[BlockLogEntryRow] = []
    for row in raw[:12]:
        if not isinstance(row, dict):
            continue
        reasons = row.get("match_reasons")
        if not isinstance(reasons, list):
            reasons = []
        out.append(
            BlockLogEntryRow(
                entry_id=str(row.get("entry_id") or row.get("id") or "") or None,
                match_type=str(row.get("match_type") or "") or None,
                peer_user_id=str(row.get("peer_user_id") or "") or None,
                peer_preview_label=str(row.get("peer_preview_label") or "") or None,
                match_strength=row.get("match_strength"),
                match_reasons=[str(r) for r in reasons[:6]],
                match_summary=str(row.get("match_summary") or "") or None,
                peer_signal_detail=str(row.get("peer_signal_detail") or "") or None,
                peer_signal_intent=str(row.get("peer_signal_intent") or "") or None,
                my_signal_detail=str(row.get("my_signal_detail") or "") or None,
                my_signal_intent=str(row.get("my_signal_intent") or "") or None,
                created_at=str(row.get("created_at") or "") or None,
                expires_at=str(row.get("expires_at") or "") or None,
                notification_sent_to_peer=bool(row.get("notification_sent_to_peer")),
                block_id=str(row.get("block_id") or "") or None,
                block_name=str(row.get("block_name") or "") or None,
            )
        )
    return out


def _hosting_draft_from_raw(raw: Any) -> HostingDraftPayload | None:
    if not isinstance(raw, dict):
        return None
    tags = raw.get("trait_tags")
    if not isinstance(tags, list):
        tags = []
    return HostingDraftPayload(
        title=str(raw.get("title") or "") or None,
        headline=str(raw.get("headline") or "") or None,
        when_label=str(raw.get("when_label") or "") or None,
        where_label=str(raw.get("where_label") or "") or None,
        who_label=str(raw.get("who_label") or "") or None,
        trait_tags=[str(t) for t in tags[:6]],
        status_label=str(raw.get("status_label") or "") or None,
        outreach_copy=str(raw.get("outreach_copy") or "") or None,
    )


def _tip_draft_from_raw(raw: Any) -> TipDraftPayload | None:
    if not isinstance(raw, dict):
        return None
    tags = raw.get("trait_tags")
    if not isinstance(tags, list):
        tags = []
    return TipDraftPayload(
        title=str(raw.get("title") or "") or None,
        headline=str(raw.get("headline") or "") or None,
        where_label=str(raw.get("where_label") or "") or None,
        trait_tags=[str(t) for t in tags[:6]],
        status_label=str(raw.get("status_label") or "") or None,
        outreach_copy=str(raw.get("outreach_copy") or "") or None,
    )


def _signal_saved_from_ctx(ctx: dict[str, Any]) -> SignalSavedPayload | None:
    raw = ctx.get("signal_saved")
    if not raw or not isinstance(raw, dict):
        return None
    hosting = _hosting_draft_from_raw(raw.get("hosting"))
    tip = _tip_draft_from_raw(raw.get("tip"))
    return SignalSavedPayload(
        signal_id=str(raw.get("signal_id") or "") or None,
        intent=str(raw.get("intent") or "") or None,
        category=str(raw.get("category") or "") or None,
        detail_text=str(raw.get("detail_text") or "") or None,
        block_id=str(raw.get("block_id") or "") or None,
        matches_created=raw.get("matches_created"),
        hosting=hosting,
        tip=tip,
    )


def _identity_profile_from_ctx(ctx: dict[str, Any]) -> IdentityProfilePayload | None:
    raw = ctx.get("identity_profile")
    if not raw or not isinstance(raw, dict):
        return None
    profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
    claims_raw = raw.get("claims") if isinstance(raw.get("claims"), list) else []
    claims: list[IdentityClaimRow] = []
    for row in claims_raw[:24]:
        if not isinstance(row, dict):
            continue
        claims.append(
            IdentityClaimRow(
                id=str(row.get("id") or "") or None,
                concept=str(row.get("concept") or "") or None,
                label=str(row.get("label") or "") or None,
                tone=str(row.get("tone") or "") or None,
                confidence=row.get("confidence"),
                disclosure=str(row.get("disclosure") or "") or None,
                bucket=str(row.get("bucket") or "") or None,
                source_quote=str(row.get("source_quote") or "") or None,
            )
        )
    return IdentityProfilePayload(
        mapped_summary=str(raw.get("mapped_summary") or "") or None,
        nickname=str(profile.get("nickname") or "") or None,
        block_display_name=str(profile.get("block_display_name") or "") or None,
        claims=claims,
    )


def _signup_routing_phase_for_fe(ctx: dict[str, Any], auth: AuthSession) -> str | None:
    """
    Map session ctx → routing_phase for the PWA verify gate.

    Do not downgrade an in-flight OTP turn back to await_signup_phone just
    because the JWT is not verified yet — that caused the FE to re-show the
    phone field after the user posted their OTP to Lana.
    """
    phase = str(ctx.get("routing_phase") or "").strip()
    raw_action = ctx.get("auth_action")
    action_type = (
        str(raw_action.get("type") or "").strip()
        if isinstance(raw_action, dict)
        else ""
    )

    if auth.phone_verified:
        return phase or None
    if phase in ("need_zip", "need_identity", "need_display_name"):
        return phase
    if phase in ("await_signup_phone", "await_signup_otp", "gate_verify"):
        return phase
    if action_type == "link_phone_signup":
        return "await_signup_otp"
    if action_type == "verify_signup_otp":
        return "await_signup_otp"
    if ctx.get("pending_post_verify"):
        return phase or "preview"
    if (
        ctx.get("requires_phone_verification")
        and not auth.phone_verified
        and phase in ("", "listening", "preview")
    ):
        return "await_signup_phone"
    return phase or None


def _onboarding_fields(
    ctx: dict[str, Any],
    auth: AuthSession,
    *,
    ready_to_complete: bool = False,
) -> dict[str, Any]:
    from app.peer_discovery_surface import stamp_peer_discovery_ctx

    stamp_peer_discovery_ctx(ctx, phone_verified=auth.phone_verified)
    jm = _joint_moment_from_dict(ctx.get("joint_moment"))
    intro = _intro_proposal_from_dict(ctx.get("intro_proposal"))
    ui_intent = derive_ui_intent(
        ctx,
        ready_to_complete=ready_to_complete,
        peer_count=len(_peer_matches_from_ctx(ctx)),
        activity_count=len(_activity_previews_from_ctx(ctx)),
        phone_verified=auth.phone_verified,
    )
    pending_intros = (
        _pending_intros_from_ctx(ctx)
        if ui_intent
        in (
            UI_INTENT_SHOW_PENDING_INTROS,
            UI_INTENT_RESPOND_PENDING_INTRO,
            UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
        )
        else []
    )
    block_log_entries = (
        _block_log_from_ctx(ctx) if ui_intent == UI_INTENT_SHOW_BLOCK_LOG else []
    )
    signal_saved = (
        _signal_saved_from_ctx(ctx) if ui_intent == UI_INTENT_SIGNAL_SAVED else None
    )
    identity_profile = (
        _identity_profile_from_ctx(ctx)
        if ui_intent == UI_INTENT_SHOW_IDENTITY_PROFILE
        else None
    )
    peers_raw = _peer_matches_from_ctx(ctx)
    activities = _activity_previews_from_ctx(ctx)
    routing_phase = _signup_routing_phase_for_fe(ctx, auth)
    active = str(ctx.get("active_intent") or "").strip()
    show_peers = ui_intent in PEER_SURFACE_UI_INTENTS or (
        bool(peers_raw) and active in PEER_DISCOVERY_ACTIVE_INTENTS
    )
    peers = peers_raw if show_peers else []
    if ui_intent == UI_INTENT_OFFER_NEIGHBOR_INTRO and peers:
        offer = ctx.get("pending_intro_offer")
        candidate_id = (
            str(offer.get("candidate_user_id") or "")
            if isinstance(offer, dict)
            else ""
        )
        if candidate_id:
            focused = [p for p in peers if str(p.peer_user_id or "") == candidate_id]
            peers = focused[:1] if focused else peers[:1]
        else:
            peers = peers[:1]
    if ui_intent != UI_INTENT_SHOW_ACTIVITY_PREVIEW:
        activities = []
    discovery_surface = _discovery_surface_from_ctx(ctx) if show_peers else None
    ui_actions = _ui_actions_from_ctx(ctx, ui_intent)
    return {
        "onboarding_step": ctx.get("guest_step"),
        "requires_phone_verification": bool(ctx.get("requires_phone_verification")),
        "joint_moment": jm,
        "intro_proposal": intro,
        "pending_intros": pending_intros,
        "block_log_entries": block_log_entries,
        "signal_saved": signal_saved,
        "identity_profile": identity_profile,
        "phone_verified": auth.phone_verified,
        "home_block_assigned": bool(auth.home_block_id or ctx.get("preview_block_id")),
        "peer_matches": peers,
        "discovery_surface": discovery_surface,
        "activity_previews": activities,
        "auth_intent": ctx.get("auth_intent"),
        "login_phone": ctx.get("login_phone"),
        "requires_login_otp": bool(ctx.get("requires_login_otp")),
        "login_otp_token": ctx.get("login_otp_token"),
        "auth_action": _auth_action_from_ctx(ctx),
        "active_intent": ctx.get("active_intent"),
        "routing_phase": routing_phase,
        "ui_intent": ui_intent,
        "ui_actions": ui_actions,
    }


def _accepted_cohost_id(user_id: str, candidate_id: str | None) -> str | None:
    if not candidate_id:
        return None
    try:
        sb = service_client()
        res = (
            sb.table("event_cohost_invites")
            .select("id")
            .eq("host_id", user_id)
            .eq("candidate_id", str(candidate_id))
            .eq("status", "accepted")
            .limit(1)
            .execute()
        )
        if res.data:
            return str(candidate_id)
    except Exception:
        return None
    return None


def _persist_profile_patch(user_id: str, patch: dict[str, str]) -> None:
    from app.claims_persist import persist_profile_patch

    persist_profile_patch(user_id, patch)


def _persist_claims(user_id: str, claims: list[ExtractedClaim]) -> None:
    replace_all_claims(user_id, claims)


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return authorization.removeprefix("Bearer ").strip()


@app.get("/")
def root():
    return {
        "service": "tagalng-lana-worker",
        "version": "0.5.3",
        "orchestrator": _use_orchestrator(),
        "endpoints": {
            "health": "GET /health",
            "create_session": "POST /lana/sessions",
            "send_message": "POST /lana/sessions/{session_id}/messages",
            "complete": "POST /lana/sessions/{session_id}/complete",
            "get_session": "GET /lana/sessions/{session_id}",
            "docs": "GET /docs",
        },
        "purposes": ["profile_intake", "event_draft"],
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "openai_configured": openai_configured(),
        "vertex_configured": _vertex_configured(),
        "orchestrator_enabled": _use_orchestrator(),
        "event_fast_path": event_fast_path_enabled(),
        "profile_fast_path": profile_fast_path_enabled(),
        "llm_provider": provider(),
        "llm_configured": llm_configured(),
        "router_model": router_model() if llm_configured() else None,
        "synth_model": synthesizer_model() if llm_configured() else None,
        "lana_model": os.environ.get(
            "VERTEX_LANA_MODEL",
            os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
        ),
        "extract_model": (
            synthesizer_model()
            if _use_orchestrator() and llm_configured()
            else os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
        ),
    }


def _profile_context_pack(
    auth: AuthSession,
    purpose: str,
    session_ctx: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    ctx_pack, user_block, purpose_ids = _load_lana_context_pack(auth.user_id, purpose)
    if auth.is_anonymous or (session_ctx and session_ctx.get("guest_intake")):
        ctx_pack = {**ctx_pack, "guest_intake": True}
        if session_ctx and session_ctx.get("guest_step"):
            ctx_pack["guest_step"] = session_ctx["guest_step"]
        user_block = format_profile_intake_context(ctx_pack)
    return ctx_pack, user_block, purpose_ids


@app.post("/lana/sessions", response_model=CreateSessionResponse)
def create_lana_session(
    body: CreateSessionRequest,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    auth = verify_auth(authorization)
    require_home_block_for_purpose(auth, body.purpose)

    purpose = body.purpose
    try:
        session, resumed = create_session(
            auth.user_id, purpose, force_new=body.force_new
        )
        session_id = str(session["id"])
        if resumed:
            messages = list_messages(session_id)
            opening = ""
            status = "active"
            merged_ctx = dict(session.get("context") or {})
            ui_raw: dict[str, Any] | None = None
            draft_raw = None
            use_orch = use_orchestrator_for_purpose(purpose)
            for m in reversed(messages):
                if m.get("role") != "assistant":
                    continue
                opening = str(m.get("content") or "")
                meta = m.get("metadata") or {}
                status = str(meta.get("status") or status)
                ui_raw = meta.get("ui") or ui_raw
                use_orch = bool(meta.get("orchestrator", use_orch))
                break
            if not opening:
                if purpose == "lana":
                    opening, status, session_ctx, ui_raw = lana_unified_opening()
                    merged_ctx = {**merged_ctx, **session_ctx}
                    use_orch = False
                else:
                    raise HTTPException(
                        status_code=500, detail="resumed_session_empty"
                    )
            ready = status == "ready_to_complete"
            ob = _onboarding_fields(merged_ctx, auth, ready_to_complete=ready)
            ui = _ui_from_dict(ui_raw)
            event_draft = _draft_from_dict(draft_raw)
            return CreateSessionResponse(
                session_id=session_id,
                purpose=purpose,
                status="active",
                assistant_message=opening,
                ready_to_complete=ready,
                ui=ui,
                event_draft=event_draft,
                orchestrator=use_orch,
                is_anonymous=auth.is_anonymous,
                **ob,
            )

        use_orch = use_orchestrator_for_purpose(purpose)
        if purpose == "lana":
            opening, status, session_ctx, ui_raw = lana_unified_opening()
            draft_raw = None
            use_orch = False
        elif use_orch:
            opening, status, session_ctx, ui_raw, draft_raw = run_opening(
                user_id=auth.user_id,
                purpose=purpose,
                session_id=session_id,
            )
        elif purpose == "event_draft":
            ctx_pack, user_block, purpose_ids = _load_lana_context_pack(auth.user_id, purpose)
            opening, status, session_ctx, ui_raw, draft_raw = lana_event_opening(
                user_block,
                purpose_ids,
                host_name=host_display_name(ctx_pack),
            )
            session_ctx["last_routing"] = _event_routing_stub()
        elif purpose == "profile_intake" and auth.is_anonymous:
            opening, status, session_ctx, ui_raw = lana_profile_guest_opening()
            session_ctx["guest_intake"] = True
            draft_raw = None
        elif purpose == "profile_intake":
            ctx_pack, user_block, _ = _profile_context_pack(auth, purpose, {})
            opening, status, session_ctx, ui_raw = lana_profile_opening(
                user_block,
                host_name=host_display_name(ctx_pack),
                ctx_pack=ctx_pack,
            )
            session_ctx["last_routing"] = _profile_routing_stub()
            draft_raw = None
        else:
            _, user_block, _ = _load_lana_context_pack(auth.user_id, purpose)
            opening, status, session_ctx, ui_raw = lana_opening(user_block, purpose)
            draft_raw = None

        insert_message(
            session_id,
            "assistant",
            opening,
            {"status": status, "ui": ui_raw, "orchestrator": use_orch},
        )
        merged_ctx = merge_session_context(session.get("context"), session_ctx)
        update_session_context(
            session_id,
            merged_ctx,
            core_block=session_ctx.get("core_block"),
        )
        ui = _ui_from_dict(ui_raw)
        event_draft = _draft_from_dict(draft_raw)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_session_failed", exc),
        ) from exc

    ready = status == "ready_to_complete"
    ob = _onboarding_fields(merged_ctx, auth, ready_to_complete=ready)
    return CreateSessionResponse(
        session_id=session_id,
        purpose=purpose,
        status="active",
        assistant_message=opening,
        ready_to_complete=ready,
        ui=ui,
        event_draft=event_draft,
        orchestrator=use_orch,
        is_anonymous=auth.is_anonymous,
        **ob,
    )


@app.post("/lana/sessions/{session_id}/messages", response_model=SendMessageResponse)
def send_lana_message(
    session_id: str,
    body: SendMessageRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    timer = TurnTimer()
    auth = verify_auth(authorization)

    with timer.stage("db_load_session"):
        session = get_session_for_user(session_id, auth.user_id)
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

    purpose = str(session.get("purpose", "profile_intake"))
    require_home_block_for_purpose(auth, purpose)
    with timer.stage("db_save_user_message"):
        user_msg_id = insert_message(session_id, "user", body.message.strip(), {}, embed=False)
    with timer.stage("db_list_messages"):
        history = list_messages(session_id)

    session_ctx_in = dict(session.get("context") or {})
    if purpose in ("lana", "profile_intake"):
        if persist_nickname_if_stated(auth.user_id, body.message.strip()):
            session_ctx_in["display_name_saved"] = True
    # Deterministic entry into the in-chat event-host flow — from the "A meet to host"
    # CTA hint OR an explicit "host/plan a <event>" message (no classifier dependency).
    if purpose == "lana" and (
        body.intent_hint == "host_event"
        or looks_like_host_event_entry(body.message)
    ):
        session_ctx_in["event_host_active"] = True
        session_ctx_in["event_host_turns"] = 0
        session_ctx_in["event_affinity_asked"] = False
    # Deterministic entry into the in-chat "pass along an item" flow — from the
    # "Something to pass along" CTA hint OR an explicit offer phrase (no classifier
    # dependency, so it engages before discovery classifies it as sharing.swap).
    if purpose == "lana" and (
        body.intent_hint == "pass_along"
        or looks_like_pass_along_entry(body.message)
    ):
        session_ctx_in["pass_along_active"] = True
        session_ctx_in["pass_along_turns"] = 0
        session_ctx_in["pass_along_photo_prompted"] = False
        session_ctx_in["item_draft"] = None
    # Deterministic entry into the in-chat "share a tip" flow — from the "A tip to
    # share" CTA hint OR an explicit recommendation phrase.
    if purpose == "lana" and not session_ctx_in.get("pass_along_active") and (
        body.intent_hint == "tip_share"
        or looks_like_tip_share_entry(body.message)
    ):
        session_ctx_in["tip_share_active"] = True
        session_ctx_in["tip_turns"] = 0
        session_ctx_in["tip_ready"] = None
        session_ctx_in["tip_enrich_count"] = 0
        session_ctx_in["tip_draft"] = None

    timing_ms: dict[str, int] | None = None
    assistant_msg_id: str | None = None
    merged: dict[str, Any] = {}
    try:
        purpose_ids: list[str] = []
        prev_draft = session_ctx_in.get("event_draft")

        use_orch = use_orchestrator_for_purpose(purpose)
        orch_used = False
        user_jwt = _bearer_token(authorization)
        if purpose == "lana":
            reply, status, session_ctx, ui_raw, draft_raw = run_lana_unified_pipeline(
                user_id=auth.user_id,
                session_id=session_id,
                history=history,
                user_message=body.message,
                session_ctx=session_ctx_in,
                user_jwt=user_jwt,
                phone_verified=auth.phone_verified,
                home_block_id=auth.home_block_id,
                is_anonymous=auth.is_anonymous,
                persisted_core=session.get("core_block") if isinstance(session.get("core_block"), dict) else None,
                timer=timer,
                use_orchestrator=use_orch,
            )
            timing_ms = session_ctx.pop("timing_ms", None)
            orch_used = bool(session_ctx.pop("_orchestrator_turn", False))
        elif use_orch:
            reply, status, session_ctx, ui_raw, draft_raw = run_turn(
                user_id=auth.user_id,
                session_id=session_id,
                purpose=purpose,
                history=history,
                user_message=body.message,
                session_ctx=session_ctx_in,
                user_jwt=user_jwt,
                persisted_core=session.get("core_block") if isinstance(session.get("core_block"), dict) else None,
                timer=timer,
            )
            timing_ms = session_ctx.pop("timing_ms", None)
            orch_used = True
        else:
            if purpose in ("profile_intake", "lana"):
                ctx_pack, user_block, purpose_ids = _profile_context_pack(
                    auth, purpose, session_ctx_in
                )
            else:
                require_home_block_for_purpose(auth, purpose)
                ctx_pack, user_block, purpose_ids = _load_lana_context_pack(
                    auth.user_id, purpose, timer=timer
                )
            reply, status, session_ctx, ui_raw, draft_raw = _legacy_lana_turn(
                purpose=purpose,
                user_block=user_block,
                purpose_ids=purpose_ids,
                history=history,
                user_message=body.message,
                prev_draft=prev_draft,
                timer=timer,
                user_id=auth.user_id,
                ctx_pack=ctx_pack,
                session_ctx=session_ctx_in,
                session_id=session_id,
                user_jwt=user_jwt,
                auth=auth,
            )
            timing_ms = timer.to_dict()
            orch_used = False

        with timer.stage("db_save_assistant_message"):
            assistant_msg_id = insert_message(
                session_id,
                "assistant",
                reply,
                {"status": status, "ui": ui_raw, "orchestrator": orch_used},
                embed=False,
            )
        with timer.stage("db_update_session"):
            merged = merge_session_context(session.get("context"), session_ctx)
            update_session_context(
                session_id,
                merged,
                core_block=session_ctx.get("core_block"),
            )
        ui = _ui_from_dict(ui_raw)
        event_draft = _draft_from_dict(draft_raw or merged.get("event_draft"))
        item_draft = _item_draft_from_dict(merged.get("item_draft"))
        tip_draft = _tip_draft_from_dict(merged.get("tip_draft"))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_message_failed", exc),
        ) from exc

    if user_msg_id:
        background_tasks.add_task(embed_message_by_id, user_msg_id, body.message.strip())
    if assistant_msg_id:
        background_tasks.add_task(embed_message_by_id, assistant_msg_id, reply)
    if purpose in ("lana", "profile_intake") and should_extract_claims_from_message(
        body.message
    ) and not merged.get("skip_claims_background_extract"):
        background_tasks.add_task(
            extract_and_upsert_claims_from_message,
            auth.user_id,
            body.message.strip(),
            bool(merged.get("skip_heritage_background_extract")),
        )

    with timer.stage("db_list_messages_final"):
        all_msgs = list_messages(session_id)
    if timing_ms is not None:
        merged_timing = dict(timing_ms)
        for key, ms in timer.ms.items():
            merged_timing[key] = merged_timing.get(key, 0) + ms
        timing_ms = merged_timing
    else:
        timing_ms = dict(timer.ms)
    timing_ms["total_ms"] = _timing_total_ms(timing_ms)

    ready = status == "ready_to_complete"
    ob = _onboarding_fields(merged, auth, ready_to_complete=ready)
    # Debug + timing stay on the backend (logged) but are no longer sent to the FE —
    # they were noise on the wire and nothing in the client reads them.
    debug = _turn_debug_from_ctx(
        merged, ui_intent=ob.get("ui_intent"), orchestrator=orch_used
    )
    _LOG.debug(
        "lana_turn session=%s msgs=%s timing=%s debug=%s",
        session_id,
        len(all_msgs),
        timing_ms,
        debug.model_dump() if debug is not None else None,
    )
    ob.pop("onboarding_step", None)  # not read by the FE
    return SendMessageResponse(
        session_id=session_id,
        status=status,
        assistant_message=reply,
        ready_to_complete=ready,
        ui=ui,
        event_draft=event_draft,
        item_draft=item_draft,
        tip_draft=tip_draft,
        routing=_routing_from_ctx(merged),
        orchestrator=orch_used,
        **ob,
    )


@app.post("/lana/profile-photo", response_model=ProfilePhotoUploadResponse)
async def upload_lana_profile_photo(
    authorization: str | None = Header(default=None),
    file: UploadFile = File(...),
):
    """Upload avatar to Supabase `avatars` bucket and set users.profile_photo_url."""
    auth = verify_auth(authorization)
    if auth.is_anonymous and not auth.phone_verified:
        raise HTTPException(status_code=403, detail="phone_not_verified")
    raw = await file.read()
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    url = upload_profile_photo_bytes(auth.user_id, raw, content_type)
    return ProfilePhotoUploadResponse(profile_photo_url=url)


@app.post(
    "/lana/sessions/{session_id}/signal-photo",
    response_model=SignalPhotoUploadResponse,
)
async def upload_lana_signal_photo(
    session_id: str,
    authorization: str | None = Header(default=None),
    file: UploadFile = File(...),
):
    """Upload a pass-along item photo to the `signal-photos` bucket and attach the
    URL to the session's in-progress item draft, so it's saved with the signal."""
    auth = verify_auth(authorization)
    if auth.is_anonymous and not auth.phone_verified:
        raise HTTPException(status_code=403, detail="phone_not_verified")
    session = get_session_for_user(session_id, auth.user_id)
    raw = await file.read()
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    url = upload_signal_photo_bytes(auth.user_id, raw, content_type)
    # Stamp onto the active item draft so the next save persists it.
    ctx = dict(session.get("context") or {})
    draft = dict(ctx.get("item_draft") or {})
    draft["photo_url"] = url
    ctx["item_draft"] = draft
    update_session_context(session_id, ctx)
    return SignalPhotoUploadResponse(photo_url=url)


@app.post("/lana/sessions/{session_id}/complete", response_model=CompleteSessionResponse)
def complete_lana_session(
    session_id: str,
    body: CompleteSessionRequest,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    auth = verify_auth(authorization)
    user_jwt = _bearer_token(authorization)

    session = get_session_for_user(session_id, auth.user_id)
    if session.get("status") == "completed":
        raise HTTPException(status_code=400, detail="session_already_completed")
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

    purpose = str(session.get("purpose", "profile_intake"))
    require_home_block_for_purpose(auth, purpose)
    messages = list_messages(session_id)
    user_turns = [m for m in messages if m.get("role") == "user"]
    if not user_turns and not body.force:
        raise HTTPException(status_code=400, detail="no_user_messages")

    last_status = (session.get("context") or {}).get("last_status")
    if last_status != "ready_to_complete" and not body.force and len(user_turns) < 2:
        raise HTTPException(
            status_code=400,
            detail="keep_chatting_or_set_force_true",
        )

    transcript = transcript_text(messages)
    sess_ctx = session.get("context") or {}

    if purpose == "event_draft":
        return _complete_event_draft(
            session_id=session_id,
            user_id=auth.user_id,
            user_jwt=user_jwt,
            transcript=transcript,
            sess_ctx=sess_ctx,
            publish=body.publish,
        )

    try:
        if _use_orchestrator():
            claims, closing, mapped_summary, spans = claude_extract_profile_from_transcript(
                transcript
            )
        else:
            claims, closing, mapped_summary, spans = vertex_extract_from_transcript(transcript)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_extract_failed", exc),
        ) from exc

    _persist_claims(auth.user_id, claims)
    final_ctx = {
        **sess_ctx,
        "last_status": "completed",
        "mapped_summary": mapped_summary,
        "spans": [s.model_dump() for s in spans],
    }
    complete_session(session_id, final_ctx)

    return CompleteSessionResponse(
        session_id=session_id,
        status="completed",
        assistant_message=closing,
        claims=claims,
        threads_found=len(claims),
        mapped_summary=mapped_summary,
        spans=spans,
        published=False,
    )


def _complete_event_draft(
    *,
    session_id: str,
    user_id: str,
    user_jwt: str,
    transcript: str,
    sess_ctx: dict[str, Any],
    publish: bool,
) -> CompleteSessionResponse:
    ctx_pack = load_event_draft_context(user_id)
    purpose_ids = ctx_pack.get("event_purpose_ids") or []
    prev_draft = sess_ctx.get("event_draft")

    try:
        if _use_orchestrator():
            draft, closing, mapped_summary, spans = claude_extract_event_from_transcript(
                transcript,
                purpose_ids=purpose_ids,
                previous_draft=prev_draft,
            )
        else:
            draft, closing, mapped_summary, spans = vertex_extract_event_from_transcript(
                transcript,
                purpose_ids=purpose_ids,
                previous_draft=prev_draft,
            )
    except ValueError as exc:
        if str(exc) == "event_title_required":
            raise HTTPException(status_code=400, detail="event_title_required") from exc
        raise HTTPException(status_code=502, detail="lana_extract_failed") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_extract_failed", exc),
        ) from exc

    event_id: str | None = None
    published = False
    if publish:
        try:
            cohost_id = _accepted_cohost_id(user_id, sess_ctx.get("pending_cohost_id"))
            event_id = publish_event(user_id, user_jwt, draft, cohost_id=cohost_id)
            published = True
        except HTTPException as exc:
            if exc.detail == "phone_not_verified":
                closing = (
                    "Your event draft is ready — verify your phone in settings, "
                    "then publish from the form or call complete again."
                )
            else:
                raise

    final_ctx = {
        **sess_ctx,
        "last_status": "completed",
        "mapped_summary": mapped_summary,
        "spans": [s.model_dump() for s in spans],
        "event_draft": draft.model_dump(),
        "event_id": event_id,
    }
    complete_session(session_id, final_ctx)

    return CompleteSessionResponse(
        session_id=session_id,
        status="completed",
        assistant_message=closing,
        mapped_summary=mapped_summary,
        spans=spans,
        event_id=event_id,
        event_draft=draft,
        published=published,
    )


@app.get("/lana/sessions/{session_id}", response_model=SessionDetailResponse)
def get_lana_session(
    session_id: str,
    authorization: str | None = Header(default=None),
):
    user_id = verify_jwt(authorization)
    session = get_session_for_user(session_id, user_id)
    messages = list_messages(session_id)
    sess_ctx = session.get("context") or {}
    return SessionDetailResponse(
        session_id=session_id,
        purpose=str(session.get("purpose", "profile_intake")),
        status=str(session.get("status", "active")),
        context=sess_ctx,
        mapped_summary=sess_ctx.get("mapped_summary"),
        spans=sess_ctx.get("spans") or [],
        event_draft=_draft_from_dict(sess_ctx.get("event_draft")),
        messages=[
            {
                "id": m.get("id"),
                "role": m.get("role"),
                "content": m.get("content"),
                "metadata": m.get("metadata") or {},
                "ui": (m.get("metadata") or {}).get("ui"),
                "created_at": m.get("created_at"),
            }
            for m in messages
        ],
    )


@app.get("/lana/users/{user_id}/block-log", response_model=BlockLogListResponse)
def get_user_block_log(
    user_id: str,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    if str(auth.user_id) != str(user_id):
        raise HTTPException(status_code=403, detail="forbidden")
    user_jwt = _bearer_token(authorization)
    rows = fetch_my_block_log(user_jwt)
    entries = [
        BlockLogEntryRow(**normalize_block_log_row(row))
        for row in rows
    ]
    block_id = entries[0].block_id if entries else auth.home_block_id
    block_name = entries[0].block_name if entries else None
    return BlockLogListResponse(block_id=block_id, block_name=block_name, entries=entries)


@app.post("/lana/block-log/{entry_id}/action")
def post_block_log_action(
    entry_id: str,
    body: BlockLogActionRequest,
    authorization: str | None = Header(default=None),
):
    verify_auth(authorization)
    user_jwt = _bearer_token(authorization)
    result = block_log_take_action(user_jwt, entry_id, body.action)
    return result
