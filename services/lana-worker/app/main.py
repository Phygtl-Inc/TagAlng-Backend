import os
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
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
)
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
from app.models import (
    CompleteSessionRequest,
    CompleteSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    EventDraft,
    ExtractedClaim,
    HighlightSpan,
    JointMomentCandidate,
    JointMomentPayload,
    LanaTurnUi,
    PeerMatchRow,
    SendMessageRequest,
    SendMessageResponse,
    SessionDetailResponse,
    TurnRouting,
)
from app.orchestrator import orchestrator_enabled, run_opening, run_turn
from app.orchestrator.llm import llm_configured, provider, router_model, synthesizer_model
from app.orchestrator.extract import (
    claude_extract_event_from_transcript,
    claude_extract_profile_from_transcript,
)
from app.vertex_event import lana_event_opening, lana_event_turn
from app.vertex_event_extract import vertex_extract_event_from_transcript
from app.claim_embed import claim_embedding_text
from app.vertex_extract import vertex_embed, vertex_extract_from_transcript
from app.vertex_lana import lana_opening, lana_turn

app = FastAPI(title="TagAlng lana-worker", version="0.5.3")

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
    return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())


def _vertex_required() -> None:
    if not _vertex_configured():
        raise HTTPException(
            status_code=503,
            detail="vertex_not_configured_set_GCP_VERTEX_PROJECT",
        )


def _vertex_error_detail(prefix: str, exc: Exception) -> str:
    msg = str(exc).replace("\n", " ")[:500]
    return f"{prefix}:{type(exc).__name__}:{msg}"


def _embed_claim(c: ExtractedClaim) -> list[float]:
    try:
        text = claim_embedding_text(
            concept=c.concept,
            label=c.label,
            source_quote=c.source_quote,
            bucket=c.bucket,
        )
        return vertex_embed(text)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"embedding_failed:{type(exc).__name__}",
        ) from exc


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


def _peer_matches_from_ctx(ctx: dict[str, Any]) -> list[PeerMatchRow]:
    raw = ctx.get("peer_matches")
    if not isinstance(raw, list):
        return []
    out: list[PeerMatchRow] = []
    for row in raw[:8]:
        if not isinstance(row, dict):
            continue
        out.append(
            PeerMatchRow(
                peer_user_id=str(row.get("peer_user_id") or "") or None,
                nickname=str(row.get("nickname") or "") or None,
                avatar_url=str(row.get("avatar_url") or "") or None,
                similarity_score=row.get("similarity_score"),
                matching_peer_label=str(row.get("matching_peer_label") or "") or None,
                matching_peer_concept=str(row.get("matching_peer_concept") or "") or None,
                has_exact_concept_match=bool(row.get("has_exact_concept_match")),
            )
        )
    return out


def _onboarding_fields(
    ctx: dict[str, Any],
    auth: AuthSession,
) -> dict[str, Any]:
    jm = _joint_moment_from_dict(ctx.get("joint_moment"))
    return {
        "onboarding_step": ctx.get("guest_step"),
        "requires_phone_verification": bool(ctx.get("requires_phone_verification")),
        "joint_moment": jm,
        "phone_verified": auth.phone_verified,
        "home_block_assigned": bool(auth.home_block_id),
        "peer_matches": _peer_matches_from_ctx(ctx),
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
    from app.auth import service_client

    row: dict[str, Any] = {}
    if patch.get("nickname"):
        row["nickname"] = patch["nickname"][:30]
    if patch.get("full_name"):
        row["full_name"] = patch["full_name"][:80]
    if not row:
        return
    service_client().table("users").update(row).eq("id", user_id).execute()


def _persist_claims(user_id: str, claims: list[ExtractedClaim]) -> None:
    from app.auth import service_client

    sb = service_client()
    sb.table("user_identity_claims").delete().eq("user_id", user_id).is_(
        "dismissed_at", "null"
    ).execute()
    rows: list[dict[str, Any]] = []
    for c in claims:
        rows.append(
            {
                "user_id": user_id,
                "concept": c.concept,
                "label": c.label,
                "tone": c.tone,
                "confidence": c.confidence,
                "disclosure": c.disclosure,
                "synonyms": c.synonyms,
                "source_quote": c.source_quote,
                "bucket": c.bucket,
                "embedding": _embed_claim(c),
            }
        )
    if rows:
        sb.table("user_identity_claims").insert(rows).execute()


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
        session = create_session(auth.user_id, purpose)
        session_id = str(session["id"])
        use_orch = use_orchestrator_for_purpose(purpose)
        if use_orch:
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
        merged_ctx = {**(session.get("context") or {}), **session_ctx}
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

    ob = _onboarding_fields(merged_ctx, auth)
    return CreateSessionResponse(
        session_id=session_id,
        purpose=purpose,
        status="active",
        assistant_message=opening,
        ready_to_complete=(status == "ready_to_complete"),
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

    timing_ms: dict[str, int] | None = None
    assistant_msg_id: str | None = None
    merged: dict[str, Any] = {}
    try:
        purpose_ids: list[str] = []
        prev_draft = (session.get("context") or {}).get("event_draft")

        use_orch = use_orchestrator_for_purpose(purpose)
        user_jwt = _bearer_token(authorization)
        if use_orch:
            reply, status, session_ctx, ui_raw, draft_raw = run_turn(
                user_id=auth.user_id,
                session_id=session_id,
                purpose=purpose,
                history=history,
                user_message=body.message,
                session_ctx=session.get("context") or {},
                user_jwt=user_jwt,
                persisted_core=session.get("core_block") if isinstance(session.get("core_block"), dict) else None,
                timer=timer,
            )
            timing_ms = session_ctx.pop("timing_ms", None)
        else:
            if purpose == "profile_intake":
                ctx_pack, user_block, purpose_ids = _profile_context_pack(
                    auth, purpose, session.get("context") or {}
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
                session_ctx=session.get("context") or {},
                session_id=session_id,
                user_jwt=user_jwt,
                auth=auth,
            )
            timing_ms = timer.to_dict()

        with timer.stage("db_save_assistant_message"):
            assistant_msg_id = insert_message(
                session_id,
                "assistant",
                reply,
                {"status": status, "ui": ui_raw, "orchestrator": use_orch},
                embed=False,
            )
        with timer.stage("db_update_session"):
            merged = {**(session.get("context") or {}), **session_ctx}
            update_session_context(
                session_id,
                merged,
                core_block=session_ctx.get("core_block"),
            )
        ui = _ui_from_dict(ui_raw)
        event_draft = _draft_from_dict(draft_raw or merged.get("event_draft"))
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_message_failed", exc),
        ) from exc

    if user_msg_id:
        background_tasks.add_task(embed_message_by_id, user_msg_id, body.message.strip())
    if assistant_msg_id:
        background_tasks.add_task(embed_message_by_id, assistant_msg_id, reply)

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

    ob = _onboarding_fields(merged, auth)
    return SendMessageResponse(
        session_id=session_id,
        status=status,
        assistant_message=reply,
        ready_to_complete=(status == "ready_to_complete"),
        message_count=len(all_msgs),
        ui=ui,
        event_draft=event_draft,
        routing=_routing_from_ctx(merged),
        orchestrator=use_orch,
        timing_ms=timing_ms,
        **ob,
    )


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
