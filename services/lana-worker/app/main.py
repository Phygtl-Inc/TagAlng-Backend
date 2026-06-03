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
from app.models import (
    CompleteSessionRequest,
    CompleteSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ExtractedClaim,
    HighlightSpan,
    LanaTurnUi,
    SendMessageRequest,
    SendMessageResponse,
    SessionDetailResponse,
)
from app.vertex_extract import vertex_embed, vertex_extract_from_transcript
from app.vertex_lana import lana_opening, lana_turn

app = FastAPI(title="TagAlng lana-worker", version="0.2.0")

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


def _persist_claims(user_id: str, claims: list[ExtractedClaim]) -> None:
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


@app.get("/")
def root():
    return {
        "service": "tagalng-lana-worker",
        "version": "0.2.0",
        "endpoints": {
            "health": "GET /health",
            "create_session": "POST /lana/sessions",
            "send_message": "POST /lana/sessions/{session_id}/messages",
            "complete": "POST /lana/sessions/{session_id}/complete",
            "get_session": "GET /lana/sessions/{session_id}",
            "docs": "GET /docs",
        },
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "vertex_configured": _vertex_configured(),
        "lana_model": os.environ.get(
            "VERTEX_LANA_MODEL",
            os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
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
    if purpose != "profile_intake":
        raise HTTPException(status_code=400, detail="only_profile_intake_supported_v01")

    try:
        session = create_session(user_id, purpose)
        session_id = str(session["id"])
        ctx_pack = load_user_context(user_id)
        user_block = format_user_context(ctx_pack, purpose)
        opening, status, session_ctx, ui_raw = lana_opening(user_block, purpose)
        insert_message(
            session_id,
            "assistant",
            opening,
            {"status": status, "ui": ui_raw},
        )
        merged_ctx = {**(session.get("context") or {}), **session_ctx}
        update_session_context(session_id, merged_ctx)
        ui = _ui_from_dict(ui_raw)
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

    insert_message(session_id, "user", body.message.strip(), {})
    history = list_messages(session_id)

    try:
        ctx_pack = load_user_context(user_id)
        user_block = format_user_context(ctx_pack, str(session.get("purpose", "profile_intake")))
        reply, status, session_ctx, ui_raw = lana_turn(
            user_block,
            str(session.get("purpose", "profile_intake")),
            history,
            body.message,
        )
        insert_message(session_id, "assistant", reply, {"status": status, "ui": ui_raw})
        merged = {**(session.get("context") or {}), **session_ctx}
        update_session_context(session_id, merged)
        ui = _ui_from_dict(ui_raw)
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
    )


@app.post("/lana/sessions/{session_id}/complete", response_model=CompleteSessionResponse)
def complete_lana_session(
    session_id: str,
    body: CompleteSessionRequest,
    authorization: str | None = Header(default=None),
):
    _vertex_required()
    user_id = verify_jwt(authorization)
    require_home_block(user_id)

    session = get_session_for_user(session_id, user_id)
    if session.get("status") == "completed":
        raise HTTPException(status_code=400, detail="session_already_completed")
    if session.get("status") != "active":
        raise HTTPException(status_code=400, detail="session_not_active")

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
    try:
        claims, closing, mapped_summary, spans = vertex_extract_from_transcript(transcript)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("lana_extract_failed", exc),
        ) from exc

    _persist_claims(user_id, claims)
    final_ctx = {
        **(session.get("context") or {}),
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
