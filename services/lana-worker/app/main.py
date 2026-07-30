import json
import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.auth import (
    AuthSession,
    require_home_block_for_purpose,
    service_client,
    verify_auth,
    verify_jwt,
)
from app.context import (
    cohort_tag_labels_for,
    format_event_draft_context,
    format_user_context,
    load_event_draft_context,
    load_user_context,
    set_address_context,
)
from app.event_context import host_display_name
from app.db import (
    complete_session,
    create_session,
    embed_message_by_id,
    get_session_for_user,
    insert_message,
    list_messages,
    pop_pending_event_draft,
    pop_pending_meet_seek,
    pop_pending_signal_ask,
    transcript_text,
    update_session_context,
    merge_session_context,
)
from app.local_signals import block_log_take_action, fetch_my_block_log, normalize_block_log_row
from app.lana_paths import (
    event_fast_path_enabled,
    latent_extract_enabled,
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
from app.i18n import (
    finalize_reply_language,
    lang_from_accept_language,
    localize_labels,
    localize_text,
    session_lang,
)
from app.lana_dispatch import lana_unified_opening, lana_unified_turn
from app.lang_pref import (
    get_user_preferred_language,
    language_preference_post_turn,
    seed_session_language,
)
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
    EventCancelHookRequest,
    EventDecisionHookRequest,
    EventDraft,
    EventJoinHookRequest,
    EventSetupRequest,
    EventVenueRequest,
    ItemDraft,
    LookDraft,
    PlaceResult,
    PlaceSearchRequest,
    PlaceSearchResponse,
    ProfilePhotoUploadResponse,
    ReverseGeocodeRequest,
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
    latest_claim_id,
    persist_nickname_if_stated,
    replace_all_claims,
    should_extract_claims_from_message,
    try_upsert_claims_from_message,
    user_needs_display_name,
)
from app.rapport_synth import ensure_gap_buffer as rapport_ensure_gap_buffer
from app.reply_compose import compose_reply
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

from app.analytics import track as amplitude_track
from app.feedback import record_feedback as record_lana_feedback
from app.notifications import email_html, notify_user
from app.rapport_gaps import (
    mark_answered as rapport_mark_answered,
    mute_gap as rapport_mute_gap,
    reconcile_gaps as rapport_reconcile_gaps,
    record_skip as rapport_record_skip,
)
from app.rapport_ranker import next_ask as rapport_next_ask
from pydantic import BaseModel as _BaseModel

_LOG = logging.getLogger(__name__)

# Surface app-level INFO logs (per-turn traces, etc.). uvicorn configures no root handler,
# so app.* logs otherwise reach stderr only via Python's lastResort handler (WARNING-only) —
# which is why INFO diagnostics were invisible. Attach a dedicated INFO StreamHandler to the
# "app" logger and stop propagation so nothing double-prints. Level via LANA_LOG_LEVEL.
_app_logger = logging.getLogger("app")
_app_logger.setLevel(os.environ.get("LANA_LOG_LEVEL", "INFO").upper())
if not any(getattr(h, "_lana_turn_handler", False) for h in _app_logger.handlers):
    _turn_handler = logging.StreamHandler()
    _turn_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _turn_handler._lana_turn_handler = True  # type: ignore[attr-defined]
    _app_logger.addHandler(_turn_handler)
    _app_logger.propagate = False

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


# Rapport safety gate — AI-signal, not keywords: defer to the classifier's own verdict and
# never open a rapport gap on a turn it routed to a safety / out-of-scope rail. These are
# classifier labels the pipeline already produces (same ones the discovery rails check).
_RAPPORT_BLOCK_GOALS = frozenset({"out_of_scope", "unsafe", "medical", "crisis"})
_RAPPORT_BLOCK_LINEAR = frozenset(
    {"system.out_of_scope", "system.unsafe", "system.medical", "system.crisis"}
)


def _rapport_gap_allowed(ctx: dict[str, Any]) -> bool:
    slots = ctx.get("_discovery_slots")
    slots = slots if isinstance(slots, dict) else {}
    goal = str(slots.get("goal") or "")
    linear = str(slots.get("linear_intent") or "")
    return goal not in _RAPPORT_BLOCK_GOALS and linear not in _RAPPORT_BLOCK_LINEAR


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


def _ui_from_dict(raw: dict[str, Any] | None) -> LanaTurnUi:
    raw = raw or {}
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
    draft = EventDraft(**raw)
    if draft.cohort_tags and not draft.cohort_tag_labels:
        draft.cohort_tag_labels = cohort_tag_labels_for(draft.cohort_tags)
    return draft


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


def _look_draft_from_dict(raw: dict[str, Any] | None) -> LookDraft | None:
    if not raw or not isinstance(raw, dict):
        return None
    fields = set(LookDraft.model_fields)
    return LookDraft(**{k: v for k, v in raw.items() if k in fields})


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


def _place_suggestions_from_ctx(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Google Places fallback for an empty tip-seek → tappable cards. maps_url opens the
    spot in Google Maps (by place_id when available, else a name+address search)."""
    from urllib.parse import quote_plus

    raw = ctx.get("google_place_suggestions")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        addr = str(p.get("address") or "").strip()
        pid = str(p.get("place_id") or "").strip()
        if pid:
            maps_url = f"https://www.google.com/maps/place/?q=place_id:{pid}"
        else:
            maps_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(
                f"{name} {addr}".strip()
            )
        out.append({"name": name, "address": addr, "place_id": pid or None, "maps_url": maps_url})
    return out


def _ui_actions_from_ctx(ctx: dict[str, Any], ui_intent: str) -> list[UiActionRow]:
    rows = [
        row for row in derive_ui_actions(ctx, ui_intent)
        if isinstance(row, dict) and row.get("id")
    ]
    # Chips mirror the session language for DISPLAY only — the message payload stays
    # canonical English so deterministic accept-matchers keep working; the chip-tap
    # language pin (see _offered_chip_msgs) stops that English payload from flipping
    # the session. One batched render per turn, cached per (label, lang).
    labels = [str(r.get("label") or "") for r in rows]
    labels = localize_labels(labels, session_lang(ctx))
    out: list[UiActionRow] = []
    for row, label in zip(rows, labels):
        out.append(
            UiActionRow(
                id=str(row["id"]),
                label=label or str(row.get("label") or ""),
                message=str(row.get("message") or ""),
                style=row.get("style") or "primary",
                intro_id=str(row.get("intro_id") or "") or None,
                peer_user_id=str(row.get("peer_user_id") or "") or None,
            )
        )
    return out


def _offered_chip_messages(ob: dict[str, Any]) -> list[str]:
    """Every message payload a chip in this response can post back, deduped — the
    main ui_actions row plus card-attached actions (pending intros, peer cards)."""
    msgs: list[str] = []

    def _collect(actions: Any) -> None:
        for a in actions or []:
            m = str(getattr(a, "message", "") or "").strip()
            if m and m not in msgs:
                msgs.append(m)

    _collect(ob.get("ui_actions"))
    for row in ob.get("pending_intros") or []:
        _collect(getattr(row, "actions", None))
    for row in ob.get("peer_matches") or []:
        _collect(getattr(row, "actions", None))
    return msgs[:24]


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
        email=str(raw.get("email") or "") or None,
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
                matching_my_label=str(row.get("matching_my_label") or "") or None,
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
                activity_id=str(row.get("activity_id") or "") or None,
                title=title,
                starts_at=str(row.get("starts_at") or "") or None,
                has_time=row.get("has_time") is not False,
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
        "place_suggestions": _place_suggestions_from_ctx(ctx),
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
    accept_language: str | None = Header(default=None),
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
            opening_msg_id: str | None = None
            for m in reversed(messages):
                if m.get("role") != "assistant":
                    continue
                opening = str(m.get("content") or "")
                opening_msg_id = str(m.get("id")) if m.get("id") else None
                meta = m.get("metadata") or {}
                status = str(meta.get("status") or status)
                ui_raw = meta.get("ui") or ui_raw
                use_orch = bool(meta.get("orchestrator", use_orch))
                break
            if not opening:
                if purpose == "lana":
                    opening, status, session_ctx, ui_raw = lana_unified_opening(
                        is_anonymous=auth.is_anonymous
                    )
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
                assistant_message_id=opening_msg_id,
                ready_to_complete=ready,
                ui=ui,
                event_draft=event_draft,
                orchestrator=use_orch,
                is_anonymous=auth.is_anonymous,
                **ob,
            )

        use_orch = use_orchestrator_for_purpose(purpose)
        if purpose == "lana":
            # Recover an event a guest built before logging into this (existing) account —
            # the login reset would otherwise lose it. Seed the host context so their next
            # message republishes it, and greet with the event instead of the cold opening.
            recovered = (
                pop_pending_event_draft(auth.user_id) if not auth.is_anonymous else None
            )
            # A meet seek the guest confirmed ("Start listening for me") just before logging
            # into this existing account — stashed against this account at email entry. Only
            # pop it when there's no event to recover this turn (event takes priority; the
            # seek stays for the next session).
            recovered_seek = (
                pop_pending_meet_seek(auth.user_id)
                if not auth.is_anonymous and not recovered
                else None
            )
            # A signal ask (babysitter rec, swap, …) the guest made just before logging
            # into this existing account — stashed at email entry, same one-at-a-time
            # priority as the meet seek above.
            recovered_signal = (
                pop_pending_signal_ask(auth.user_id)
                if not auth.is_anonymous and not recovered and not recovered_seek
                else None
            )
            if recovered and isinstance(recovered.get("event_draft"), dict):
                session_ctx = {
                    **recovered,
                    "event_host_active": True,
                    "host_publish_pending": True,
                    "unified_mode": True,
                }
                _rec_title = str(recovered["event_draft"].get("title") or "your event")
                opening = compose_reply(
                    goal=(
                        "The user just logged in and you still have the event they "
                        "built before logging in, ready to post. Welcome them back "
                        "and tell them to send any message so you can post it for "
                        "their neighbors."
                    ),
                    facts=[f"The recovered event's name: {_rec_title}"],
                    fallback=(
                        f"Welcome back! I still have **{_rec_title}** ready to go — "
                        "send a message and I'll post it for your neighbors."
                    ),
                )
                status = "continue"
                ui_raw = None
                draft_raw = recovered["event_draft"]
                use_orch = False
            elif recovered_seek:
                # The guest already confirmed, so finish the save straight away (mirrors the
                # event recovery above) and greet with the listening confirmation instead of
                # the cold opening — otherwise the seek is silently lost on an existing-account
                # login and never shows in their radar.
                from app.look_meet import save_pending_meet_seek

                opening, status, session_ctx, ui_raw = lana_unified_opening(
                    is_anonymous=auth.is_anonymous
                )
                session_ctx["look_seek_pending"] = recovered_seek
                saved_reply = save_pending_meet_seek(
                    session_ctx=session_ctx,
                    user_jwt=_bearer_token(authorization),
                    block_id=auth.home_block_id,
                    zip_code=None,
                )
                if saved_reply:
                    opening = saved_reply
                draft_raw = None
                use_orch = False
            elif recovered_signal:
                # Same rescue for a signal ask (babysitter rec, swap, …): save it now and
                # greet with the confirmation; when the account has no block yet, the
                # stash stays in session and the reply asks for the ZIP (the post-verify
                # pop finishes the save on the next message).
                from app.discovery_route import save_pending_signal_ask

                opening, status, session_ctx, ui_raw = lana_unified_opening(
                    is_anonymous=auth.is_anonymous
                )
                session_ctx["signal_pending"] = recovered_signal
                saved_reply = save_pending_signal_ask(
                    session_ctx=session_ctx,
                    user_jwt=_bearer_token(authorization),
                    block_id=auth.home_block_id,
                    zip_code=None,
                )
                if saved_reply:
                    opening = saved_reply
                draft_raw = None
                use_orch = False
            else:
                # Signed-in mom with no name yet → greet with the name-ask up front (it's
                # needed anyway, and asking first avoids interrupting a topic mid-chat).
                _needs_name = (not auth.is_anonymous) and user_needs_display_name(
                    auth.user_id, {}
                )
                opening, status, session_ctx, ui_raw = lana_unified_opening(
                    is_anonymous=auth.is_anonymous, needs_name=_needs_name
                )
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

        if purpose == "lana":
            # The saved preference decides how the conversation STARTS — the
            # opening greets in it. Guests have no saved row, so their device
            # locale (Accept-Language — the same signal rendering the app's own
            # UI in their language) is the preference; same fallback for a
            # signed-in user who never saved one. Live detection owns every
            # turn after.
            pref = None if auth.is_anonymous else get_user_preferred_language(auth.user_id)
            pref = pref or lang_from_accept_language(accept_language)
            if pref:
                seed_session_language(session_ctx, pref)
                effective_lang = session_lang(session_ctx)
                if effective_lang:
                    opening = localize_text(opening, effective_lang)

        opening_msg_id = insert_message(
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
        # Log the full traceback to the console — otherwise the caller only sees a
        # truncated 502 string and the real stack (where it actually broke) is lost.
        _LOG.exception("lana_session_failed (purpose=%s)", purpose)
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_session_failed", exc),
        ) from exc

    ready = status == "ready_to_complete"
    ob = _onboarding_fields(merged_ctx, auth, ready_to_complete=ready)
    # Chip-tap language pin: remember the opening's chip payloads too (the message
    # endpoint does the same) — one extra tiny write, once per session, only when
    # the opening actually offers chips.
    _chip_msgs = _offered_chip_messages(ob)
    if _chip_msgs:
        merged_ctx["_offered_chip_msgs"] = _chip_msgs
        try:
            update_session_context(
                session_id, merged_ctx, core_block=merged_ctx.get("core_block")
            )
        except Exception:  # noqa: BLE001 — the pin is best-effort, never fail the opening
            _LOG.exception("opening_chip_stash_persist_failed")
    return CreateSessionResponse(
        session_id=session_id,
        purpose=purpose,
        status="active",
        assistant_message=opening,
        assistant_message_id=opening_msg_id,
        ready_to_complete=ready,
        ui=ui,
        event_draft=event_draft,
        orchestrator=use_orch,
        is_anonymous=auth.is_anonymous,
        **ob,
    )


def _run_lana_message(
    session_id: str,
    body: SendMessageRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None,
    emit: Callable[[str, str | None], None] | None = None,
    accept_language: str | None = None,
) -> SendMessageResponse:
    """Core of a Lana message turn. Shared by the blocking and streaming endpoints.

    `emit`, when provided, receives human-readable progress labels (+ optional
    detail subheading) as the turn advances (attached to the TurnTimer, which is
    threaded through every path). The blocking endpoint passes None, a no-op.
    """
    _vertex_required()
    timer = TurnTimer()
    if emit is not None:
        timer.set_emitter(emit)
    auth = verify_auth(authorization)

    with timer.stage("db_load_session"):
        session = get_session_for_user(session_id, auth.user_id)
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

    purpose = str(session.get("purpose", "profile_intake"))
    _LOG.info(
        "──▶ turn start | purpose=%s session=%s user=%s msg=%r",
        purpose,
        session_id,
        auth.user_id,
        body.message.strip()[:200],
    )
    require_home_block_for_purpose(auth, purpose)
    with timer.stage("db_save_user_message"):
        user_msg_id = insert_message(session_id, "user", body.message.strip(), {}, embed=False)
    with timer.stage("db_list_messages"):
        history = list_messages(session_id)

    session_ctx_in = dict(session.get("context") or {})
    # Sessions opened before any language signal (guest funnels: every turn a tap,
    # an email, an OTP) would ship deterministic replies in English forever — the
    # final-mile localizer has no language to render into. Seed once from the
    # device locale; any AI verdict or saved preference already present wins.
    if "lang" not in session_ctx_in and "preferred_lang" not in session_ctx_in:
        device_lang = lang_from_accept_language(accept_language)
        if device_lang:
            seed_session_language(session_ctx_in, device_lang)
    # Lingo §3.3/§4: the inferred household role + grammatical gender ride along on
    # every turn so composers can address the user correctly ("your grandkids",
    # bienvenido/bienvenida). Free — verify_auth already loaded the users row.
    # Re-stamped (None clears) each turn so a role extracted mid-conversation
    # reaches copy from the very next turn. set_address_context feeds every prompt
    # built through lingo_constitution() for the rest of this request.
    session_ctx_in["user_role"] = auth.role
    session_ctx_in["user_grammatical_gender"] = auth.grammatical_gender
    set_address_context(auth.role, auth.grammatical_gender)
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
        # Start a fresh draft — don't inherit a prior session's event (the stale
        # "dads meetup" card). event_id too, so a past publish doesn't skip the flow.
        session_ctx_in["event_draft"] = None
        session_ctx_in["event_id"] = None
        session_ctx_in["event_when_date"] = None
        session_ctx_in["event_when_time"] = None
        session_ctx_in["event_place_asked"] = False
        session_ctx_in["event_venue"] = None
        session_ctx_in["event_venue_tried"] = None
        session_ctx_in["event_settings"] = None
        session_ctx_in["event_cap_asked"] = False
        session_ctx_in["event_approval_asked"] = False
        session_ctx_in["event_share_asked"] = False
        # Batched-setup stage (review → setup → confirm → published); start unset.
        session_ctx_in["host_stage"] = None
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
        # Entering pass-along closes any in-flight tip flow + its card.
        session_ctx_in["tip_share_active"] = False
        session_ctx_in["tip_ready"] = None
        session_ctx_in["tip_draft"] = None
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
        # Entering tip-share closes any in-flight pass-along flow + its card.
        session_ctx_in["pass_along_active"] = False
        session_ctx_in["item_draft"] = None
    # Entry into "looking for a meet/playgroup" — the explicit "A meet or playgroup" CTA
    # (intent_hint). Meet ≡ activity: looking for a meet means SEARCHING the block's real
    # activities first, so the CTA enters the activity-browse (search) flow. The seek to be
    # MATCHED is offered only as a fallback when the search comes up empty (see
    # activity_browse's _seek_offer). Natural language is left to the AI classifier downstream.
    if purpose == "lana" and not session_ctx_in.get("pass_along_active") and not session_ctx_in.get(
        "tip_share_active"
    ) and body.intent_hint == "look_meet":
        session_ctx_in["activity_browse_active"] = True
        session_ctx_in["browse_turns"] = 0
        session_ctx_in["browse_draft"] = None
        # Button entry carries a generic seed phrase with no real interest — skip mining it so
        # the flow asks P1 ("what kind of meet?") and never releases on the seed turn (the
        # classifier mis-reads the generic payload as meet_seek). Consumed next turn.
        session_ctx_in["browse_skip_seed"] = True
    # A "By the way…" tile answer — route it deterministically to the profile path (save the
    # claim + reply in-thread) rather than the classifier, which would misread a bare answer
    # ("I love trying new restaurants") as a recommendation seek. Carries the gap + tile question.
    if purpose == "lana" and body.intent_hint == "rapport_answer":
        session_ctx_in["rapport_answer"] = {
            "gap_row_id": (body.rapport_gap_row_id or "").strip() or None,
            "question": (body.rapport_question or "").strip() or None,
        }

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
            # Language preference: persist an accepted/requested default-language
            # change, and offer the switch once when the observed language keeps
            # diverging from the saved preference.
            reply = language_preference_post_turn(
                user_id=auth.user_id,
                user_message=body.message,
                session_ctx=session_ctx,
                reply=reply,
                is_anonymous=auth.is_anonymous,
            )
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

        # Final-mile localization: EVERY reply renders into the session
        # language at this one choke point — composer opt-outs are gone. The
        # old `_reply_localized` stamp let a composer vouch for text nobody
        # verified, and mixed English/Spanish replies shipped (QA 2026-07-30).
        with timer.stage("localize_reply"):
            reply = finalize_reply_language(reply, session_ctx)

        # Final-mile lingo guard: EVERY reply from every composer passes here, so
        # this one check makes the constitution's banned lexicon unshippable —
        # regardless of which inline prompt forgot the rules (the leaks all came
        # from composers that never append the constitution). Regex-only on clean
        # turns (~0ms); one rewrite call only on a violation. After localization
        # on purpose: the banned lexicon includes the es/pt forms, and the
        # rewriter keeps the reply's language. LANA_LINGO_GUARD=0 disables.
        if reply and os.environ.get("LANA_LINGO_GUARD", "1").strip().lower() not in (
            "0", "false", "off"
        ):
            try:
                from app.lingo_guard import enforce as _lingo_enforce
                from app.lingo_guard import find_violations as _lingo_hits

                if _lingo_hits(reply):
                    with timer.stage("lingo_guard"):
                        _guard = _lingo_enforce(reply)
                    reply = _guard.text
                    _LOG.warning(
                        "lingo_guard_final_mile hits=%s rewritten=%s naive=%s",
                        _guard.hits, _guard.rewritten, _guard.naive_fallback,
                    )
                    session_ctx["_lingo_guard_result"] = _guard.audit_dict()
            except Exception:  # noqa: BLE001 — the guard must never break a turn
                _LOG.exception("lingo_guard_final_mile_failed")

        # These two writes hit different tables (lana_messages insert vs
        # lana_sessions update) and don't depend on each other, so run them
        # concurrently — one round-trip of wall-clock instead of two. Safe to
        # share the cached Supabase client across threads: its underlying httpx
        # session handles concurrent requests.
        merged = merge_session_context(session.get("context"), session_ctx)

        def _save_assistant_message() -> str | None:
            return insert_message(
                session_id,
                "assistant",
                reply,
                {"status": status, "ui": ui_raw, "orchestrator": orch_used},
                embed=False,
            )

        def _persist_session() -> None:
            update_session_context(
                session_id,
                merged,
                core_block=session_ctx.get("core_block"),
            )

        with timer.stage("db_write_assistant_and_session"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                msg_future = pool.submit(_save_assistant_message)
                session_future = pool.submit(_persist_session)
                assistant_msg_id = msg_future.result()
                session_future.result()
        ui = _ui_from_dict(ui_raw)
        event_draft = _draft_from_dict(draft_raw or merged.get("event_draft"))
        item_draft = _item_draft_from_dict(merged.get("item_draft"))
        tip_draft = _tip_draft_from_dict(merged.get("tip_draft"))
        look_draft = _look_draft_from_dict(merged.get("look_draft"))
    except Exception as exc:
        # Log the full traceback to the console — otherwise the caller only sees a
        # truncated 502 string and the real stack (where it actually broke) is lost.
        _LOG.exception(
            "lana_message_failed (purpose=%s session=%s)", purpose, session_id
        )
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
        # Extracts claims AND opens one contextual rapport follow-up gap from the extractor's
        # own warm question (semantic, not templated). Fire-and-forget, never blocks the turn.
        background_tasks.add_task(
            extract_and_upsert_claims_from_message,
            auth.user_id,
            body.message.strip(),
            skip_heritage=bool(merged.get("skip_heritage_background_extract")),
            message_id=user_msg_id,
            # Defer to the classifier: no rapport gap on safety/OOS/medical/crisis turns.
            allow_rapport_gap=_rapport_gap_allowed(merged),
        )
        # Close any gaps whose concept the user has since stated.
        background_tasks.add_task(rapport_reconcile_gaps, auth.user_id, user_msg_id)
    # Layer 3b latent-intent collection (Phase 1: collect, don't surface). Off by default.
    if purpose == "lana" and latent_extract_enabled():
        from app.latent_extract import run_latent_intent

        background_tasks.add_task(
            run_latent_intent,
            auth.user_id,
            session_id,
            user_msg_id,
            auth.home_block_id,
            body.message.strip(),
        )
    # Rolling conversation summary (memory hygiene) — folds turns older than the
    # recent window into lana_sessions.context.rolling_summary for decide_turn.
    # The len gate keeps short sessions free; the task re-checks its own stride.
    if purpose == "lana" and len(history) >= 28:
        from app.policy.summary import maybe_update_rolling_summary

        background_tasks.add_task(maybe_update_rolling_summary, session_id, auth.user_id)

    # Message count for the debug log below — computed in memory instead of
    # re-fetching the whole thread from Supabase. `history` already holds every
    # message through the user turn (it was listed after the user insert); the
    # only new row this turn is the assistant message we just inserted.
    final_msg_count = len(history) + (1 if assistant_msg_id else 0)
    if timing_ms is not None:
        # timing_ms is a mid-turn snapshot of the SAME timer (to_dict() taken inside the
        # pipeline) — timer.ms holds the authoritative full-turn accumulation. Overwrite,
        # never add: adding double-counted every pre-snapshot stage, inflating total_ms
        # by up to ~2× (a 23s turn logged as 40s).
        merged_timing = dict(timing_ms)
        merged_timing.update(timer.ms)
        timing_ms = merged_timing
    else:
        timing_ms = dict(timer.ms)
    timing_ms["total_ms"] = _timing_total_ms(timing_ms)

    ready = status == "ready_to_complete"
    ob = _onboarding_fields(merged, auth, ready_to_complete=ready)
    # Chip-tap language pin, part 1: remember EXACTLY which chip payloads this response
    # offers, so the next turn can tell an app-authored tap from typed text (the pipeline
    # pins the session language on a match — a canonical-English chip payload must never
    # flip an Urdu session back to English). Chips are assembled after the session write
    # above, so a changed set re-persists in the background; unchanged sets write nothing.
    _chip_msgs = _offered_chip_messages(ob)
    if (merged.get("_offered_chip_msgs") or []) != _chip_msgs:
        merged["_offered_chip_msgs"] = _chip_msgs
        background_tasks.add_task(_persist_session)
    # Debug + timing stay on the backend (logged) but are no longer sent to the FE —
    # they were noise on the wire and nothing in the client reads them.
    debug = _turn_debug_from_ctx(
        merged, ui_intent=ob.get("ui_intent"), orchestrator=orch_used
    )
    _LOG.info(
        "◀── turn done | session=%s total=%sms intent=%s handler=%s ui=%s",
        session_id,
        timing_ms.get("total_ms"),
        getattr(debug, "intent", None),
        getattr(debug, "handler", None),
        ob.get("ui_intent"),
    )
    _LOG.debug(
        "lana_turn session=%s msgs=%s timing=%s debug=%s",
        session_id,
        final_msg_count,
        timing_ms,
        debug.model_dump() if debug is not None else None,
    )
    ob.pop("onboarding_step", None)  # not read by the FE

    # Server-truth product analytics (Amplitude) — fire-and-forget, same user_id as the
    # browser so events stitch. Tracks the turn + the conversions the client can't be
    # trusted to report (publish / save actually happened server-side).
    _ui = ob.get("ui_intent")
    amplitude_track("lana_turn", user_id=auth.user_id, event_properties={"ui_intent": _ui})
    _conversions = {
        "event_created": "event_hosted",
        "item_listed": "item_listed",
        "tip_listed": "tip_shared",
        "look_meet_saved": "meet_seek_saved",
    }
    if _ui in _conversions:
        amplitude_track(
            _conversions[_ui],
            user_id=auth.user_id,
            event_properties={"event_id": merged.get("event_id")} if _ui == "event_created" else None,
        )
        # A meet just published → confirm to the host via push + email.
        if _ui == "event_created" and merged.get("event_id"):
            _eid = str(merged.get("event_id"))
            _etitle = (
                getattr(event_draft, "title", None) or "your meet"
            ) if event_draft else "your meet"
            notify_user(
                auth.user_id,
                title="Your meet is live 🎉",
                body=f"“{_etitle}” is posted in your area — I’ll tell you when neighbors ask to join.",
                url=f"/meet/{_eid}",
                email_subject=f"Your meet “{_etitle}” is live",
                email_html=email_html(
                    "Your meet is live 🎉",
                    f"“{_etitle}” is now posted in your area. I’ll let you know as neighbors ask to join.",
                    cta_label="Open the meet",
                    cta_path=f"/meet/{_eid}",
                ),
            )
    elif _ui == "signal_saved":
        _sig = merged.get("signal_saved") if isinstance(merged.get("signal_saved"), dict) else {}
        amplitude_track(
            "signal_saved",
            user_id=auth.user_id,
            event_properties={"intent": (_sig or {}).get("intent"), "category": (_sig or {}).get("category")},
        )

    return SendMessageResponse(
        session_id=session_id,
        status=status,
        assistant_message=reply,
        assistant_message_id=assistant_msg_id,
        ready_to_complete=ready,
        ui=ui,
        event_draft=event_draft,
        item_draft=item_draft,
        tip_draft=tip_draft,
        look_draft=look_draft,
        event_id=(str(merged.get("event_id")) if merged.get("event_id") else None),
        routing=_routing_from_ctx(merged),
        orchestrator=orch_used,
        **ob,
    )


@app.post("/lana/sessions/{session_id}/messages", response_model=SendMessageResponse)
def send_lana_message(
    session_id: str,
    body: SendMessageRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    accept_language: str | None = Header(default=None),
):
    """Blocking turn — returns the full SendMessageResponse as one JSON body."""
    return _run_lana_message(
        session_id, body, background_tasks, authorization,
        accept_language=accept_language,
    )


def _sse_frame(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/lana/sessions/{session_id}/messages/stream")
def stream_lana_message(
    session_id: str,
    body: SendMessageRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    accept_language: str | None = Header(default=None),
):
    """Same turn as the blocking endpoint, streamed as Server-Sent Events.

    Frames: `{"type":"status","label":...,"detail":...}` as the turn advances (detail
    is an optional subheading, omitted when absent), then a terminal
    `{"type":"result","turn":<SendMessageResponse>}` (or `{"type":"error","detail":...}`).
    The turn logic is identical — only the transport differs.

    The (sync, blocking) turn runs on a worker thread and pushes progress + the final
    result onto a queue; the SSE generator drains it. Keeps the blocking LLM clients
    sync — no async rewrite. `background_tasks` is populated by the worker before the
    result frame is queued, so FastAPI still runs the fire-and-forget jobs after the
    stream closes.
    """
    events: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    def emit(label: str, detail: str | None = None) -> None:
        events.put(("status", (label, detail)))

    def worker() -> None:
        try:
            resp = _run_lana_message(
                session_id, body, background_tasks, authorization, emit=emit,
                accept_language=accept_language,
            )
            events.put(("result", resp))
        except HTTPException as exc:
            events.put(("error", exc.detail))
        except Exception:  # noqa: BLE001
            _LOG.exception("stream_lana_message worker failed session=%s", session_id)
            events.put(("error", "lana_message_failed"))
        finally:
            events.put(("done", None))

    def gen():
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            try:
                kind, payload = events.get(timeout=10)
            except queue.Empty:
                # Keep-alive: a long synthesis call can idle the connection past a proxy's
                # timeout. An SSE comment keeps it open without confusing the client.
                yield ": ping\n\n"
                continue
            if kind == "status":
                label, detail = payload
                frame: dict[str, Any] = {"type": "status", "label": label}
                if detail:
                    frame["detail"] = detail
                yield _sse_frame(frame)
            elif kind == "result":
                yield _sse_frame(
                    {"type": "result", "turn": payload.model_dump(mode="json")}
                )
            elif kind == "error":
                yield _sse_frame({"type": "error", "detail": payload})
            else:  # done
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx response buffering for SSE
            "Connection": "keep-alive",
        },
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


@app.post("/lana/sessions/{session_id}/event-venue")
def set_event_venue(
    session_id: str,
    body: EventVenueRequest,
    authorization: str | None = Header(default=None),
):
    """Stamp the exact picked place onto the session's event draft, so the next host
    turn completes the where-step with the precise pin (lat/lng/address/place_id) and
    publish stores it — instead of re-geocoding the venue name."""
    auth = verify_auth(authorization)
    session = get_session_for_user(session_id, auth.user_id)
    ctx = dict(session.get("context") or {})
    draft = dict(ctx.get("event_draft") or {})
    draft["venue_name"] = body.name.strip()
    draft["venue_address"] = (body.address or "").strip() or None
    draft["place_id"] = (body.place_id or "").strip() or None
    draft["venue_lat"] = body.lat
    draft["venue_lng"] = body.lng
    ctx["event_draft"] = draft
    ctx["event_venue"] = {
        "name": draft["venue_name"],
        "address": draft["venue_address"],
        "place_id": draft["place_id"],
        "lat": body.lat,
        "lng": body.lng,
    }
    ctx["event_place_asked"] = True  # the where-step is satisfied precisely now
    update_session_context(session_id, ctx)
    return {"ok": True}


@app.post("/lana/sessions/{session_id}/event-setup")
def set_event_setup(
    session_id: str,
    body: EventSetupRequest,
    authorization: str | None = Header(default=None),
):
    """Stamp the host's quick-setup submission onto the session's event draft in one shot —
    the settings (capacity / sharing / approval / bring) AND any blockers the opening
    message didn't provide (title / when / where) — so the next message advances the batched
    host flow to the final review with everything applied, no turn-by-turn questions."""
    auth = verify_auth(authorization)
    session = get_session_for_user(session_id, auth.user_id)
    ctx = dict(session.get("context") or {})
    draft = dict(ctx.get("event_draft") or {})
    # Blockers the carousel collected when the opening message was sparse. Stamp them (and
    # mirror onto the step-gate keys the pipeline reads) so the draft resolves as complete.
    if body.title and body.title.strip():
        draft["title"] = body.title.strip()[:80]
    if body.starts_at and body.starts_at.strip():
        draft["starts_at"] = body.starts_at.strip()
        try:
            from datetime import datetime as _dt, timedelta as _td

            _start = _dt.fromisoformat(body.starts_at.strip())
            _dur = int(draft.get("duration_minutes") or 90)
            draft["ends_at"] = (_start + _td(minutes=_dur)).isoformat()
            ctx["event_when_date"] = _start.date().isoformat()
            ctx["event_when_time"] = _start.strftime("%H:%M")
        except (ValueError, TypeError):
            pass
    if body.venue_name and body.venue_name.strip():
        draft["venue_name"] = body.venue_name.strip()
        draft["venue_address"] = (body.venue_address or "").strip() or None
        draft["place_id"] = (body.place_id or "").strip() or None
        draft["venue_lat"] = body.venue_lat
        draft["venue_lng"] = body.venue_lng
        ctx["event_venue"] = {
            "name": draft["venue_name"],
            "address": draft["venue_address"],
            "place_id": draft["place_id"],
            "lat": body.venue_lat,
            "lng": body.venue_lng,
        }
        ctx["event_place_asked"] = True
    if body.max_attendees is not None:
        cap = int(body.max_attendees)
        draft["max_attendees"] = cap if 1 <= cap <= 200 else None
    else:
        draft["max_attendees"] = None  # explicit "no limit"
    if body.auto_approve is not None:
        draft["auto_approve"] = bool(body.auto_approve)
    if body.allow_attendee_share is not None:
        draft["allow_attendee_share"] = bool(body.allow_attendee_share)
    from app.lana_ui import is_none_bring_item

    # A typed none-answer ("nothing", "no need") on the bring card is an empty list,
    # not a literal chip.
    bring = [
        str(b).strip()[:60]
        for b in (body.bring_items or [])
        if str(b).strip() and not is_none_bring_item(str(b))
    ][:12]
    draft["bring_items"] = bring
    ctx["event_draft"] = draft
    # Keep the scratch settings dict in sync so a same-turn re-parse doesn't clobber these.
    settings = dict(ctx.get("event_settings") or {})
    settings["max_attendees"] = draft.get("max_attendees")
    settings["_cap_set"] = True
    if "auto_approve" in draft:
        settings["auto_approve"] = draft["auto_approve"]
    if "allow_attendee_share" in draft:
        settings["allow_attendee_share"] = draft["allow_attendee_share"]
    ctx["event_settings"] = settings
    update_session_context(session_id, ctx)
    return {"ok": True}


@app.post("/hooks/event-join")
def hook_event_join(
    body: EventJoinHookRequest,
    authorization: str | None = Header(default=None),
):
    """Called by the JOINER's client right after request_to_join_event. Notifies the host
    (someone wants in / joined) and the joiner (request sent / you're in). Sending is
    server-side via the service client; the caller's JWT just authorizes they're the joiner."""
    auth = verify_auth(authorization)
    from app.auth import service_client
    from app.notifications import _user_contact

    sb = service_client()
    if sb is None:
        return {"ok": False}
    try:
        ev = (
            sb.table("events").select("title,host_id").eq("id", body.event_id).single().execute().data
            or {}
        )
        req = (
            sb.table("event_requests")
            .select("status")
            .eq("event_id", body.event_id)
            .eq("requester_id", auth.user_id)
            .single()
            .execute()
            .data
            or {}
        )
    except Exception:  # noqa: BLE001
        return {"ok": False}

    title = ev.get("title") or "a meet"
    host_id = ev.get("host_id")
    auto = (req.get("status") or "pending") in ("approved", "attended")
    _, joiner_nick = _user_contact(auth.user_id)
    who = joiner_nick or "A neighbor"
    eid = body.event_id

    # → host
    if host_id and host_id != auth.user_id:
        if auto:
            notify_user(
                host_id,
                title=f"{who} joined",
                body=f"{who} just joined “{title}”.",
                url=f"/meet/{eid}/requests",
                email_subject=f"{who} joined “{title}”",
                email_html=email_html(
                    f"{who} joined “{title}”", f"{who} is in — see everyone going.",
                    "View attendees", f"/meet/{eid}/requests",
                ),
            )
        else:
            notify_user(
                host_id,
                title="New join request",
                body=f"{who} wants to join “{title}”.",
                url=f"/meet/{eid}/requests",
                email_subject=f"{who} wants to join “{title}”",
                email_html=email_html(
                    "New join request", f"{who} asked to join “{title}”. Approve or decline.",
                    "Review request", f"/meet/{eid}/requests",
                ),
            )

    # → joiner
    if auto:
        notify_user(
            auth.user_id,
            title="You’re in 🎉",
            body=f"You joined “{title}”.",
            url=f"/meet/{eid}",
            email_subject=f"You’re in: “{title}”",
            email_html=email_html(
                "You’re in 🎉", f"You joined “{title}”. See the details and group chat.",
                "Open the meet", f"/meet/{eid}",
            ),
        )
    else:
        notify_user(
            auth.user_id,
            title="Request sent",
            body=f"Your request to join “{title}” is in — the host will confirm.",
            url=f"/meet/{eid}",
            email_subject=f"Request sent: “{title}”",
            email_html=email_html(
                "Request sent", f"Your request to join “{title}” is in. The host will confirm.",
                "Open the meet", f"/meet/{eid}",
            ),
        )
    return {"ok": True}


@app.post("/hooks/event-decision")
def hook_event_decision(
    body: EventDecisionHookRequest,
    authorization: str | None = Header(default=None),
):
    """Called by the HOST's client right after decide_event_request. Notifies the requester
    of the outcome. The caller's JWT authorizes; the row + host ownership are re-checked."""
    auth = verify_auth(authorization)
    from app.auth import service_client

    sb = service_client()
    if sb is None:
        return {"ok": False}
    try:
        req = (
            sb.table("event_requests")
            .select("event_id,requester_id,status")
            .eq("id", body.request_id)
            .single()
            .execute()
            .data
            or {}
        )
    except Exception:  # noqa: BLE001
        return {"ok": False}
    requester_id = req.get("requester_id")
    eid = req.get("event_id")
    status = req.get("status")
    if not requester_id or not eid:
        return {"ok": False}
    try:
        ev = sb.table("events").select("title,host_id").eq("id", eid).single().execute().data or {}
    except Exception:  # noqa: BLE001
        return {"ok": False}
    if ev.get("host_id") != auth.user_id:  # only the host may trigger this
        return {"ok": False}
    title = ev.get("title") or "a meet"
    if status == "approved":
        notify_user(
            requester_id,
            title="You’re in 🎉",
            body=f"You’re approved for “{title}”.",
            url=f"/meet/{eid}",
            email_subject=f"You’re in: “{title}”",
            email_html=email_html(
                "You’re in 🎉", f"The host approved you for “{title}”. See the details and group chat.",
                "Open the meet", f"/meet/{eid}",
            ),
        )
    elif status == "declined":
        notify_user(
            requester_id,
            title=f"Update on “{title}”",
            body="The host couldn’t fit you in this time.",
            url=f"/meet/{eid}",
        )
    return {"ok": True}


@app.post("/hooks/event-cancel")
def hook_event_cancel(
    body: EventCancelHookRequest,
    authorization: str | None = Header(default=None),
):
    """Called by the HOST's client right after cancel_event. Fans out push + email to the
    going roster so the FE's "I let everyone know" / per-attendee "Notified" is truthful.
    The in-app group-chat system message is posted by cancel_event itself; this hook only
    handles device/email delivery. Host ownership + cancelled status are re-checked here."""
    auth = verify_auth(authorization)
    from app.auth import service_client

    sb = service_client()
    if sb is None:
        return {"ok": False}
    try:
        ev = (
            sb.table("events")
            .select("title,host_id,status")
            .eq("id", body.event_id)
            .single()
            .execute()
            .data
            or {}
        )
    except Exception:  # noqa: BLE001
        return {"ok": False}
    if ev.get("host_id") != auth.user_id:  # only the host may trigger this
        return {"ok": False}
    if ev.get("status") != "cancelled":  # cancel_event must have run first
        return {"ok": False}

    title = ev.get("title") or "a meet"
    eid = body.event_id
    try:
        rows = (
            sb.table("event_requests")
            .select("requester_id")
            .eq("event_id", eid)
            .in_("status", ["approved", "attended"])
            .eq("rsvp_status", "going")
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return {"ok": False}

    roster = {r.get("requester_id") for r in rows} - {None, auth.user_id}
    for uid in roster:
        notify_user(
            uid,
            title=f"“{title}” was cancelled",
            body="The host cancelled this meet. Sorry — hopefully next time.",
            url=f"/meet/{eid}",
            email_subject=f"Cancelled: “{title}”",
            email_html=email_html(
                f"“{title}” was cancelled",
                "The host had to call this one off. Keep an eye out — "
                "something new is usually around the corner.",
                "See what’s nearby", "/",
            ),
        )
    return {"ok": True, "notified": len(roster)}


@app.post("/lana/places/search", response_model=PlaceSearchResponse)
def search_places_endpoint(
    body: PlaceSearchRequest,
    authorization: str | None = Header(default=None),
):
    """Free-text place search (Google Places), biased to the caller's block. Powers the
    "search a place" box in the host where-step. Server-side proxy — the Maps key never
    reaches the browser. POST (not GET) so the PWA service worker doesn't intercept it.
    Returns [] when the key isn't configured."""
    auth = verify_auth(authorization)
    from app.places import search_places

    rows = search_places(query=body.q, block_id=auth.home_block_id, user_id=auth.user_id)
    return PlaceSearchResponse(results=[PlaceResult(**r) for r in rows])


@app.post("/lana/places/reverse-geocode", response_model=PlaceSearchResponse)
def reverse_geocode_endpoint(
    body: ReverseGeocodeRequest,
    authorization: str | None = Header(default=None),
):
    """Name a bare device pin ("Use my current location") — lat/lng in, at most one
    PlaceResult out with a human label + address (issue #42). The result echoes the
    CALLER's coordinates: the device pin stays authoritative, this only names it.
    Server-side proxy like /lana/places/search; [] when the key isn't configured."""
    verify_auth(authorization)
    from app.places import reverse_geocode

    row = reverse_geocode(body.lat, body.lng)
    return PlaceSearchResponse(results=[PlaceResult(**row)] if row else [])


# ── Circles (§A/§G · Place Profile §3) ────────────────────────────────────────
# The user's real-world communities, optionally grounded to a canonical places row.
# POST-only (same service-worker reason as the rapport/places endpoints). The word
# "circle" is internal-only (§A.4 M7) — surfaces name the place, never the term.


class CirclesListBody(_BaseModel):
    pass


class CircleAddBody(_BaseModel):
    circle_type: str
    detail: str | None = None
    google_place_id: str | None = None


class CircleUpdateBody(_BaseModel):
    affiliation_id: str
    detail: str | None = None


class CircleRemoveBody(_BaseModel):
    affiliation_id: str


class CircleGroundOptionsBody(_BaseModel):
    affiliation_id: str
    # "search for another" free text; defaults to the user's own captured phrase.
    query: str | None = None


class CircleGroundBody(_BaseModel):
    affiliation_id: str
    # The tapped option. Only the id crosses the wire — name/geo/address are fetched
    # server-side from Google (places.place_details), never taken from the client.
    google_place_id: str


@app.post("/lana/circles/mine")
def post_circles_mine(
    body: CirclesListBody | None = None,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.circles_flow import list_my_circles

    return {"circles": list_my_circles(auth.user_id)}


@app.post("/lana/circles/add")
def post_circles_add(
    body: CircleAddBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.circles_flow import add_circle

    try:
        result = add_circle(
            auth.user_id,
            circle_type=(body.circle_type or "").strip().lower(),
            detail=body.detail,
            google_place_id=(body.google_place_id or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    amplitude_track(
        "circle_added",
        user_id=auth.user_id,
        event_properties={"circle_type": body.circle_type, "grounded": bool(body.google_place_id)},
    )
    return result


@app.post("/lana/circles/update")
def post_circles_update(
    body: CircleUpdateBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.circles_flow import update_circle

    try:
        update_circle(auth.user_id, body.affiliation_id, detail=body.detail)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/lana/circles/remove")
def post_circles_remove(
    body: CircleRemoveBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.circles_flow import remove_circle

    try:
        remove_circle(auth.user_id, body.affiliation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    amplitude_track(
        "circle_removed",
        user_id=auth.user_id,
        event_properties={"affiliation_id": body.affiliation_id},
    )
    return {"ok": True}


@app.post("/lana/circles/ground-options")
def post_circles_ground_options(
    body: CircleGroundOptionsBody,
    authorization: str | None = Header(default=None),
):
    """2-3 real nearby places for the "which spot — X, or somewhere else?" chips,
    biased to the caller's block (server-side Google proxy, same as /lana/places)."""
    auth = verify_auth(authorization)
    from app.circles_flow import _own_affiliation, ground_options

    affiliation = _own_affiliation(auth.user_id, body.affiliation_id)
    if not affiliation:
        raise HTTPException(status_code=404, detail="affiliation_not_found")
    options = ground_options(
        auth.user_id, affiliation, block_id=auth.home_block_id, query=body.query
    )
    return {"affiliation_id": body.affiliation_id, "options": options}


@app.post("/lana/circles/ground")
def post_circles_ground(
    body: CircleGroundBody,
    authorization: str | None = Header(default=None),
):
    """Pin an affiliation to its canonical place and confirm it. Also flushes any
    pre-grounding feature notes onto the place and queues the §4.3 enrichment
    question ("What do you enjoy most at {place}?") on the rapport tile."""
    auth = verify_auth(authorization)
    from app.circles_flow import ground_affiliation

    try:
        result = ground_affiliation(auth.user_id, body.affiliation_id, body.google_place_id)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "affiliation_not_found" else 502
        raise HTTPException(status_code=status, detail=detail) from exc
    amplitude_track(
        "circle_grounded",
        user_id=auth.user_id,
        event_properties={
            "affiliation_id": body.affiliation_id,
            "place_id": result.get("place_id"),
        },
    )
    return result


# ── Invites + ZIP unlock (§A.2 · §D · §E) ─────────────────────────────────────
# Invite ≠ membership: redeem records growth only; a circle row appears only when
# the joiner self-confirms her own community (generic prompt, never the inviter's
# place). Unlock state gates consumption of others' supply — never creation.


class InviteMintBody(_BaseModel):
    # Optional label: which of the caller's circles this link is for.
    circle_key: str | None = None


class InviteRedeemBody(_BaseModel):
    token: str


class InviteSelfConfirmBody(_BaseModel):
    token: str
    circle_type: str
    detail: str | None = None


class AreaProgressBody(_BaseModel):
    zip: str | None = None


@app.post("/lana/invites/mint")
def post_invites_mint(
    body: InviteMintBody | None = None,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.circle_invites import mint_invite

    try:
        result = mint_invite(
            auth.user_id, circle_key=(body.circle_key if body else None)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    amplitude_track(
        "circle_invite_minted",
        user_id=auth.user_id,
        event_properties={"labeled": bool(body.circle_key if body else None)},
    )
    return result


@app.post("/lana/invites/redeem")
def post_invites_redeem(
    body: InviteRedeemBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    from app.circle_invites import redeem_invite

    try:
        result = redeem_invite(auth.user_id, body.token)
    except ValueError as exc:
        detail = str(exc)
        status = 429 if detail == "invite_rate_limited" else 404
        raise HTTPException(status_code=status, detail=detail) from exc
    amplitude_track(
        "circle_invite_redeemed",
        user_id=auth.user_id,
        event_properties={"confirm_prompt": result.get("confirm_prompt")},
    )
    return result


@app.post("/lana/invites/self-confirm")
def post_invites_self_confirm(
    body: InviteSelfConfirmBody,
    authorization: str | None = Header(default=None),
):
    """The joiner's yes to "are you part of a <type> community nearby?" — writes
    her own (ungrounded) affiliation. Grounding her own place then runs through
    /lana/circles/ground-options → /ground, which is what confirms it."""
    auth = verify_auth(authorization)
    from app.circle_invites import self_confirm

    try:
        result = self_confirm(
            auth.user_id,
            body.token,
            circle_type=(body.circle_type or "").strip().lower(),
            detail=body.detail,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 404 if detail == "invite_not_found" else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    return result


class EventInviteSuggestionsBody(_BaseModel):
    event_id: str


@app.post("/lana/events/invite-suggestions")
def post_event_invite_suggestions(
    body: EventInviteSuggestionsBody,
    authorization: str | None = Header(default=None),
):
    """"N people already go here — invite them?" (Place Profile §5.2): confirmed
    members of the event's anchored place, host-only, first names only."""
    auth = verify_auth(authorization)
    from app.event_place import invite_suggestions

    try:
        return invite_suggestions(auth.user_id, body.event_id)
    except ValueError as exc:
        detail = str(exc)
        status = 403 if detail == "not_event_host" else 404
        raise HTTPException(status_code=status, detail=detail) from exc


@app.post("/lana/area/progress")
def post_area_progress(
    body: AreaProgressBody | None = None,
    authorization: str | None = Header(default=None),
):
    """Warm goal-gradient status for the caller's area (§E.2): state + count/threshold
    + founding eligibility. Recounts on read (read-repair), so it is also the
    transition trigger alongside redeem — no cron at pilot scale."""
    auth = verify_auth(authorization)
    from app.zip_unlock import area_progress

    return area_progress(auth.user_id, (body.zip if body else None))


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
    # Extraction prompts are English-internal — render the closing in the
    # session language before it reaches the user.
    closing = localize_text(closing, session_lang(sess_ctx))
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
                closing = compose_reply(
                    goal=(
                        "The user's event draft is finished but publishing needs a "
                        "verified email. Tell them the draft is ready and to verify "
                        "their email in settings, then publish again."
                    ),
                    fallback=(
                        "Your event draft is ready — verify your email in settings, "
                        "then publish from the form or call complete again."
                    ),
                    cache=True,
                )
            else:
                raise

    # Covers both the extractor's closing and the hardcoded verify-email
    # override above — English until this point.
    closing = localize_text(closing, session_lang(sess_ctx))
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


# ── Rapport · Ring C ("By the way…") ──────────────────────────────────────────
# The home-screen tile asks one well-timed follow-up. next-ask is called on home
# render; record-answer/-skip/-mute report what the mom did with the tile. user_id is
# always derived from the auth token — never trusted from the client.


class RapportAnswerBody(_BaseModel):
    gap_row_id: str
    text: str
    session_id: str | None = None
    message_id: str | None = None
    # The tile's question, echoed back so Lana's concierge reply can reference it.
    question: str | None = None


class RapportSkipBody(_BaseModel):
    gap_row_id: str


class RapportMuteBody(_BaseModel):
    gap_id: str


class RapportNextAskBody(_BaseModel):
    surface: str = "homescreen"
    # True when the user taps the tile's refresh (⟳): retire the current ask and return a
    # different one now, bypassing the 24h cap.
    cycle: bool = False


@app.post("/lana/rapport/next-ask")
def post_rapport_next_ask(
    body: RapportNextAskBody | None = None,
    authorization: str | None = Header(default=None),
):
    # POST (not GET): the PWA service worker breaks cross-origin GETs to the worker but
    # lets POSTs through — same reason the chat/places calls are POST.
    auth = verify_auth(authorization)
    # Guests (anonymous auth) never get the "By the way…" tile — profile-deepening
    # questions are for committed accounts. Their claims still accrue in chat and
    # carry over on verify (same user_id), so nothing is lost by waiting.
    if auth.is_anonymous:
        return {"ask": None}
    surface = (body.surface if body else "homescreen") or "homescreen"
    cycle = bool(body.cycle) if body else False
    return {"ask": rapport_next_ask(auth.user_id, surface, cycle=cycle)}


@app.post("/lana/rapport/record-answer")
def post_rapport_record_answer(
    body: RapportAnswerBody,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    text = (body.text or "").strip()

    # Circles: a place-grounding gap's answer is a place NAME, not an identity fact —
    # skip the claims extractor. A tapped chip (or typed candidate name) grounds the
    # affiliation outright; free text is kept as detail on it. The conversational
    # confirm-chips flow lives on the chat path (lana_unified_pipeline) — this
    # endpoint just never lets a grounding answer decay into a junk claim.
    from app.rapport_gaps import get_gap_row

    gap_row = get_gap_row(body.gap_row_id)
    if gap_row and gap_row.get("affiliation_ref"):
        from app.circles_flow import (
            ground_and_confirm,
            match_grounding_candidate,
            note_ungrounded_detail,
        )

        affiliation_id = str(gap_row["affiliation_ref"])
        grounded = False
        stored = gap_row.get("grounding_options")
        tapped = match_grounding_candidate(
            stored if isinstance(stored, list) else None, text
        )
        if tapped:
            grounded = bool(
                ground_and_confirm(
                    auth.user_id, affiliation_id, str(tapped["google_place_id"])
                ).get("grounded")
            )
        elif text:
            note_ungrounded_detail(auth.user_id, affiliation_id, text)
        rapport_mark_answered(body.gap_row_id)
        background_tasks.add_task(rapport_ensure_gap_buffer, auth.user_id)
        amplitude_track(
            "rapport_gap_answered",
            user_id=auth.user_id,
            event_properties={
                "gap_row_id": body.gap_row_id,
                "kind": "place_grounding",
                "grounded": grounded,
            },
        )
        return {"ok": True, "grounded": grounded}

    claim_id: str | None = None
    saved = 0
    if text:
        # Extract claims from the answer (no per-message rapport gap — the coverage synth owns
        # tile questions), then reconcile any now-covered gaps.
        res = try_upsert_claims_from_message(
            auth.user_id, text, message_id=body.message_id, allow_rapport_gap=False
        )
        saved = res.saved
        rapport_reconcile_gaps(auth.user_id, body.message_id)
        # Link the gap to the claim the extractor made from the answer. If it made NONE, we do
        # NOT fabricate one — the extractor already declined it (junk like "i dont know", or a
        # privacy case). Trust that judgment rather than storing a non-answer as a claim.
        if saved > 0:
            claim_id = latest_claim_id(auth.user_id)
            # Circles §4.3: a place-enrichment gap tags the claim its answer produced,
            # so "the Saturday long runs" is attributable to that gym. Best-effort.
            if claim_id:
                from app.circles_flow import tag_claim_place_from_gap

                tag_claim_place_from_gap(body.gap_row_id, claim_id)
    # Close the gap regardless of whether a claim was made (don't re-ask a topic she engaged).
    rapport_mark_answered(body.gap_row_id, answer_claim_id=claim_id)
    # Refill the reserve so the "By the way…" tile always has a fresh, non-repeat question queued
    # ahead. Background so it never delays the reply; synthesizes only the shortfall from claims.
    background_tasks.add_task(rapport_ensure_gap_buffer, auth.user_id)
    amplitude_track(
        "rapport_gap_answered",
        user_id=auth.user_id,
        event_properties={"gap_row_id": body.gap_row_id, "claim_id": claim_id},
    )
    return {"ok": True}


@app.post("/lana/rapport/record-skip")
def post_rapport_record_skip(
    body: RapportSkipBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    rapport_record_skip(body.gap_row_id)
    amplitude_track(
        "rapport_gap_skipped",
        user_id=auth.user_id,
        event_properties={"gap_row_id": body.gap_row_id},
    )
    return {"ok": True}


@app.post("/lana/rapport/mute-fact")
def post_rapport_mute_fact(
    body: RapportMuteBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    rapport_mute_gap(auth.user_id, body.gap_id)
    return {"ok": True}


# ── Feedback (👍/👎 on Lana output) ────────────────────────────────────────────
# One endpoint for both rateable surfaces: an assistant chat reply (message_id) or a
# rapport tile question (gap_row_id). Same thumb again → the FE sends rating='clear'.
# Rows land in lana_feedback (service-role only) for the team to review.


class LanaFeedbackBody(_BaseModel):
    rating: str  # 'up' | 'down' | 'clear'
    message_id: str | None = None
    gap_row_id: str | None = None
    # Where the thumb lives in the UI ('chat', 'rapport_tile', …) — stored for triage.
    surface: str | None = None
    # Optional free-text follow-up (the FE offers it on 👎). Tracks the latest rating
    # write: re-rating without one clears any previous comment.
    comment: str | None = None


@app.post("/lana/feedback")
def post_lana_feedback(
    body: LanaFeedbackBody,
    authorization: str | None = Header(default=None),
):
    auth = verify_auth(authorization)
    result = record_lana_feedback(
        auth.user_id,
        rating=body.rating,
        message_id=(body.message_id or "").strip() or None,
        gap_row_id=(body.gap_row_id or "").strip() or None,
        comment=body.comment,
        context={"surface": (body.surface or "").strip() or None},
    )
    amplitude_track(
        "lana_feedback",
        user_id=auth.user_id,
        event_properties={
            "rating": result["rating"],
            "target_kind": result["target_kind"],
            "message_id": body.message_id,
            "gap_row_id": body.gap_row_id,
            "surface": body.surface,
            # Comment text stays in the DB — analytics only needs to know one exists.
            "has_comment": bool((body.comment or "").strip()),
        },
    )
    return {"ok": True, **result}
