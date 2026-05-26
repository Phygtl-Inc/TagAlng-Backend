import os
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

from app.models import (
    ExtractRequest,
    ExtractResponse,
    ExtractedClaim,
    IntakeRequest,
    IntakeResponse,
)
from app.vertex_extract import vertex_embed, vertex_extract_claims
from app.vertex_intake import vertex_intake

app = FastAPI(title="TagAlng identity-worker", version="0.2.0")

_cors_raw = os.environ.get("CORS_ALLOW_ORIGINS", "*").strip()
_cors_origins = ["*"] if _cors_raw == "*" else [o.strip() for o in _cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def _vertex_configured() -> bool:
    return bool(os.environ.get("GCP_VERTEX_PROJECT", "").strip())


def _verify_jwt(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.removeprefix("Bearer ").strip()
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    with httpx.Client(timeout=15.0) as client:
        res = client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
        )
    if res.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid_session")
    user = res.json()
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid_session")
    return user_id


def _require_home_block(user_id: str) -> None:
    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    profile = sb.table("users").select("home_block_id").eq("id", user_id).execute()
    row = profile.data[0] if profile.data else None
    if not row or not row.get("home_block_id"):
        raise HTTPException(status_code=400, detail="home_block_required")


def _vertex_required() -> None:
    if not _vertex_configured():
        raise HTTPException(
            status_code=503,
            detail="vertex_not_configured_set_GCP_VERTEX_PROJECT",
        )


def _vertex_error_detail(prefix: str, exc: Exception) -> str:
    msg = str(exc).replace("\n", " ")[:500]
    if "403" in msg and "PERMISSION_DENIED" in msg:
        return (
            f"{prefix}:vertex_permission_denied — enable Vertex AI API on GCP project "
            f"and grant roles/aiplatform.user to the service account in gcp.json. "
            f"Raw: {msg}"
        )
    if "404" in msg and "not found" in msg.lower():
        return (
            f"{prefix}:model_or_region — try GCP_VERTEX_LOCATION=us-central1 or "
            f"VERTEX_EXTRACT_MODEL=gemini-1.5-flash-002. Raw: {msg}"
        )
    return f"{prefix}:{type(exc).__name__}:{msg}"


def _embed(label: str, concept: str) -> list[float]:
    payload = f"{concept}: {label}"
    try:
        return vertex_embed(payload)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"embedding_failed:{type(exc).__name__}",
        ) from exc


def _persist_claims(user_id: str, claims: list[ExtractedClaim]) -> None:
    if not SUPABASE_SERVICE_ROLE_KEY or not SUPABASE_URL:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

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
                "embedding": _embed(c.label, c.concept),
            }
        )

    if rows:
        sb.table("user_identity_claims").insert(rows).execute()


@app.get("/")
def root():
    return {
        "service": "tagalng-identity-worker",
        "endpoints": {
            "health": "GET /health",
            "intake": "POST /identity/intake",
            "extract": "POST /identity/extract",
            "docs": "GET /docs",
        },
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "mode": "vertex",
        "vertex_configured": _vertex_configured(),
        "extract_model": os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.0-flash-001"),
        "embed_model": os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005"),
    }


@app.post("/identity/intake", response_model=IntakeResponse)
def identity_intake(
    body: IntakeRequest,
    authorization: str | None = Header(default=None),
):
    """
    ChatGPT-style intake:
    - First call (cover_text only) → may return clarify + questions
    - Second call (cover_text + clarifications) → extract, embed, save
    """
    _vertex_required()
    user_id = _verify_jwt(authorization)
    _require_home_block(user_id)

    try:
        status, message, questions, claims = vertex_intake(
            body.cover_text.strip(),
            body.clarifications or None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("identity_intake_failed", exc),
        ) from exc

    if status == "clarify" and not body.clarifications:
        return IntakeResponse(
            user_id=user_id,
            status="clarify",
            assistant_message=message,
            questions=questions,
            claims=[],
            threads_found=0,
            mode="vertex",
        )

    if not claims:
        # Fallback: full extract on merged context
        merged = body.cover_text
        for c in body.clarifications:
            merged += f"\n{c.question} {c.answer}"
        claims = vertex_extract_claims(merged)

    _persist_claims(user_id, claims)

    return IntakeResponse(
        user_id=user_id,
        status="complete",
        assistant_message=message,
        questions=[],
        claims=claims,
        threads_found=len(claims),
        mode="vertex",
    )


@app.post("/identity/extract", response_model=ExtractResponse)
def identity_extract(
    body: ExtractRequest,
    authorization: str | None = Header(default=None),
):
    """One-shot extract (skip clarification). Prefer /identity/intake for app UX."""
    _vertex_required()
    user_id = _verify_jwt(authorization)
    _require_home_block(user_id)

    try:
        claims = vertex_extract_claims(body.cover_text.strip())
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=_vertex_error_detail("identity_extract_failed", exc),
        ) from exc

    _persist_claims(user_id, claims)

    return ExtractResponse(
        user_id=user_id,
        claims=claims,
        threads_found=len(claims),
        mode="vertex",
    )
