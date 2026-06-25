from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.auth import service_client

from app.turn_surfaces import TURN_SCOPED_SURFACES

# Keys set to None in a turn ctx are removed from persisted session (shallow merge otherwise keeps stale values).
_INTRO_STATE_NULL_DELETES = frozenset({
    "pending_intro_respond",
    "pending_intro_offer",
    "intro_offer_shown",
})
_CTX_NULL_DELETES = frozenset(
    {"signal_draft", *_INTRO_STATE_NULL_DELETES, *TURN_SCOPED_SURFACES}
)

# Session keys that together describe a complete, ready-to-publish event the host flow
# built. Stashed/recovered as a unit when a guest verifies into an existing account and
# the session resets (see pending_event_drafts migration).
HOST_CTX_KEYS = (
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
)


def extract_host_ctx(session_ctx: dict[str, Any] | None) -> dict[str, Any]:
    """Pull the host-flow context subset from a session, for stashing across a login."""
    ctx = session_ctx or {}
    out = {k: ctx[k] for k in HOST_CTX_KEYS if ctx.get(k) is not None}
    return out


def stash_pending_event_draft(user_id: str, host_ctx: dict[str, Any]) -> None:
    """Persist a guest's in-progress event for `user_id` to recover after they log in.
    Best-effort: a stash failure must not break the verification turn."""
    if not user_id or not isinstance(host_ctx, dict) or not host_ctx.get("event_draft"):
        return
    try:
        service_client().table("pending_event_drafts").upsert(
            {
                "user_id": user_id,
                "host_ctx": host_ctx,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id",
        ).execute()
    except Exception:
        return


def pop_pending_event_draft(user_id: str) -> dict[str, Any] | None:
    """Read and delete the pending event for `user_id` (one-shot recovery). Returns the
    stashed host context, or None if there's nothing waiting / on any error."""
    if not user_id:
        return None
    try:
        sb = service_client()
        res = (
            sb.table("pending_event_drafts")
            .select("host_ctx")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not isinstance(row, dict):
            return None
        sb.table("pending_event_drafts").delete().eq("user_id", user_id).execute()
        host_ctx = row.get("host_ctx")
        return host_ctx if isinstance(host_ctx, dict) and host_ctx.get("event_draft") else None
    except Exception:
        return None


def merge_session_context(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = {**(old or {}), **(new or {})}
    new_ctx = new or {}
    for key in _CTX_NULL_DELETES:
        if key in new_ctx and new_ctx.get(key) is None:
            merged.pop(key, None)
    for key in TURN_SCOPED_SURFACES:
        if key not in new_ctx:
            merged.pop(key, None)
    return merged


def _embed_message(message_id: str, content: str) -> None:
    try:
        from app.vertex_extract import vertex_embed

        embedding = vertex_embed(content[:2000])
        sb = service_client()
        sb.table("lana_messages").update({"embedding": embedding}).eq("id", message_id).execute()
    except Exception:
        return


def abandon_other_active_sessions(user_id: str, purpose: str) -> None:
    sb = service_client()
    sb.table("lana_sessions").update(
        {"status": "abandoned", "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("user_id", user_id).eq("purpose", purpose).eq("status", "active").execute()


def get_active_session(user_id: str, purpose: str) -> dict[str, Any] | None:
    sb = service_client()
    res = (
        sb.table("lana_sessions")
        .select("*")
        .eq("user_id", user_id)
        .eq("purpose", purpose)
        .eq("status", "active")
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    return row if isinstance(row, dict) else None


def create_session(user_id: str, purpose: str, *, force_new: bool = False) -> tuple[dict[str, Any], bool]:
    """
    Create or resume the user's active Lana session.
    Returns (session_row, resumed).
    """
    if not force_new:
        existing = get_active_session(user_id, purpose)
        if existing:
            return existing, True
    abandon_other_active_sessions(user_id, purpose)
    sb = service_client()
    res = (
        sb.table("lana_sessions")
        .insert({"user_id": user_id, "purpose": purpose, "status": "active"})
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=500, detail="session_create_failed")
    return res.data[0], False


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
    *,
    embed: bool = True,
) -> str | None:
    sb = service_client()
    res = sb.table("lana_messages").insert(
        {
            "session_id": session_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
        }
    ).execute()
    if not res.data:
        return None
    message_id = str(res.data[0]["id"])
    if embed:
        _embed_message(message_id, content)
    return message_id


def embed_message_by_id(message_id: str, content: str) -> None:
    """Background-safe embedding for lana_messages (recall index)."""
    _embed_message(message_id, content)


def update_session_context(
    session_id: str,
    context: dict[str, Any],
    core_block: dict[str, Any] | None = None,
) -> None:
    sb = service_client()
    patch: dict[str, Any] = {
        "context": context,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if core_block is not None:
        patch["core_block"] = core_block
    sb.table("lana_sessions").update(patch).eq("id", session_id).execute()


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
