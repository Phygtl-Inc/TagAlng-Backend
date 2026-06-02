from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.auth import service_client


def abandon_other_active_sessions(user_id: str, purpose: str) -> None:
    sb = service_client()
    sb.table("lana_sessions").update(
        {"status": "abandoned", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("user_id", user_id).eq("purpose", purpose).eq("status", "active").execute()


def create_session(user_id: str, purpose: str) -> dict[str, Any]:
    abandon_other_active_sessions(user_id, purpose)
    sb = service_client()
    res = (
        sb.table("lana_sessions")
        .insert({"user_id": user_id, "purpose": purpose, "status": "active"})
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=500, detail="session_create_failed")
    return res.data[0]


def get_session_for_user(session_id: str, user_id: str) -> dict[str, Any]:
    sb = service_client()
    res = (
        sb.table("lana_sessions")
        .select("*")
        .eq("id", session_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="session_not_found")
    return res.data[0]


def list_messages(session_id: str) -> list[dict[str, Any]]:
    sb = service_client()
    res = (
        sb.table("lana_messages")
        .select("id, role, content, metadata, created_at")
        .eq("session_id", session_id)
        .order("created_at")
        .execute()
    )
    return res.data or []


def insert_message(
    session_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    sb = service_client()
    sb.table("lana_messages").insert(
        {
            "session_id": session_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
        }
    ).execute()


def update_session_context(session_id: str, context: dict[str, Any]) -> None:
    sb = service_client()
    sb.table("lana_sessions").update(
        {"context": context, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", session_id).execute()


def complete_session(session_id: str, context: dict[str, Any]) -> None:
    sb = service_client()
    now = datetime.now(timezone.utc).isoformat()
    sb.table("lana_sessions").update(
        {
            "status": "completed",
            "context": context,
            "completed_at": now,
            "updated_at": now,
        }
    ).eq("id", session_id).execute()


def transcript_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        label = "User" if role == "user" else "Lana"
        parts.append(f"{label}: {m.get('content', '').strip()}")
    return "\n\n".join(parts)
