import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.auth import require_home_block, service_client, verify_jwt
from app.context import format_user_context, load_user_context
from app.db import (
    complete_session,
    create_session,
    get_session_for_user,
    insert_message,
    list_messages,
    transcript_text,
    update_session_context,
)
from app.event_publish import publish_event
from app.models import (
    CompleteSessionRequest,
    CompleteSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    EventDraft,
    ExtractedClaim,
    HighlightSpan,
    LanaTurnUi,
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
from app.vertex_extract import vertex_embed, vertex_extract_from_transcript
from app.vertex_lana import lana_opening, lana_turn

app = FastAPI(title="TagAlng lana-worker", version="0.5.1")

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


def _embed(label: str, concept: str) -> list[float]:
    try:
        return vertex_embed(f"{concept}: {label}")
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"embedding_failed:{type(exc).__name__}",
        ) from exc


def _use_orchestrator() -> bool:
    return orchestrator_enabled()


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
                "embedding": _embed(c.label, c.concept),
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
        "version": "0.5.1",
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


@app.post("/lana/sessions", response_model=CreateSessionResponse)
def create_lana_session(
    body: CreateSessionRequest,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    user_id = verify_jwt(authorization)
    require_home_block(user_id)

    purpose = body.purpose
    try:
        session = create_session(user_id, purpose)
        session_id = str(session["id"])
        ctx_pack = load_user_context(user_id)
        user_block = format_user_context(ctx_pack, purpose)
        purpose_ids = ctx_pack.get("event_purpose_ids") or []

        use_orch = _use_orchestrator()
        if use_orch:
            opening, status, session_ctx, ui_raw, draft_raw = run_opening(
                user_id=user_id,
                purpose=purpose,
                session_id=session_id,
            )
        elif purpose == "event_draft":
            opening, status, session_ctx, ui_raw, draft_raw = lana_event_opening(
                user_block, purpose_ids
            )
        else:
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

    return CreateSessionResponse(
        session_id=session_id,
        purpose=purpose,
        status="active",
        assistant_message=opening,
        ready_to_complete=(status == "ready_to_complete"),
        ui=ui,
        event_draft=event_draft,
        orchestrator=use_orch,
    )


@app.post("/lana/sessions/{session_id}/messages", response_model=SendMessageResponse)
def send_lana_message(
    session_id: str,
    body: SendMessageRequest,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    user_id = verify_jwt(authorization)
    require_home_block(user_id)

    session = get_session_for_user(session_id, user_id)
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

    purpose = str(session.get("purpose", "profile_intake"))
    insert_message(session_id, "user", body.message.strip(), {})
    history = list_messages(session_id)

    try:
        ctx_pack = load_user_context(user_id)
        user_block = format_user_context(ctx_pack, purpose)
        purpose_ids = ctx_pack.get("event_purpose_ids") or []
        prev_draft = (session.get("context") or {}).get("event_draft")

        use_orch = _use_orchestrator()
        user_jwt = _bearer_token(authorization)
        if use_orch:
            reply, status, session_ctx, ui_raw, draft_raw = run_turn(
                user_id=user_id,
                session_id=session_id,
                purpose=purpose,
                history=history,
                user_message=body.message,
                session_ctx=session.get("context") or {},
                user_jwt=user_jwt,
                persisted_core=session.get("core_block") if isinstance(session.get("core_block"), dict) else None,
            )
        elif purpose == "event_draft":
            reply, status, session_ctx, ui_raw, draft_raw = lana_event_turn(
                user_block,
                purpose_ids,
                history,
                body.message,
                prev_draft,
            )
        else:
            reply, status, session_ctx, ui_raw = lana_turn(
                user_block,
                purpose,
                history,
                body.message,
            )
            draft_raw = session_ctx.get("event_draft")

        insert_message(
            session_id,
            "assistant",
            reply,
            {"status": status, "ui": ui_raw, "orchestrator": use_orch},
        )
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

    all_msgs = list_messages(session_id)
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
    )


@app.post("/lana/sessions/{session_id}/complete", response_model=CompleteSessionResponse)
def complete_lana_session(
    session_id: str,
    body: CompleteSessionRequest,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    user_id = verify_jwt(authorization)
    user_jwt = _bearer_token(authorization)
    require_home_block(user_id)

    session = get_session_for_user(session_id, user_id)
    if session.get("status") == "completed":
        raise HTTPException(status_code=400, detail="session_already_completed")
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

    purpose = str(session.get("purpose", "profile_intake"))
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
            user_id=user_id,
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

    _persist_claims(user_id, claims)
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
    ctx_pack = load_user_context(user_id)
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
