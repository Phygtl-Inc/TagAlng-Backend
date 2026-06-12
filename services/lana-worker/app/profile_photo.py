"""Profile photo upload — AI intent via discovery slots + storage helper."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.auth import SUPABASE_URL, service_client
from app.discovery_slots import slots_want_profile_photo

PHASE_AWAIT_PROFILE_PHOTO = "await_profile_photo"

_ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_MAX_BYTES = 2 * 1024 * 1024


def user_profile_photo_url(user_id: str | None) -> str | None:
    if not user_id:
        return None
    try:
        res = (
            service_client()
            .table("users")
            .select("profile_photo_url")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not isinstance(row, dict):
            return None
        url = str(row.get("profile_photo_url") or "").strip()
        return url or None
    except Exception:
        return None


def public_avatar_url(user_id: str, ext: str) -> str:
    base = (SUPABASE_URL or "").rstrip("/")
    return f"{base}/storage/v1/object/public/avatars/{user_id}/avatar.{ext}"


def upload_profile_photo_bytes(
    user_id: str,
    file_bytes: bytes,
    content_type: str,
) -> str:
    if len(file_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="profile_photo_too_large")
    ext = _ALLOWED_TYPES.get(content_type)
    if not ext:
        raise HTTPException(status_code=415, detail="profile_photo_type_not_allowed")
    path = f"{user_id}/avatar.{ext}"
    sb = service_client()
    sb.storage.from_("avatars").upload(
        path,
        file_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    url = public_avatar_url(user_id, ext)
    sb.table("users").update({"profile_photo_url": url}).eq("id", user_id).execute()
    return url


def _profile_photo_action(slots: dict[str, Any] | None) -> str:
    action = str((slots or {}).get("profile_photo_action") or "none").lower()
    if action in ("start", "accept", "skip", "done"):
        return action
    goal = str((slots or {}).get("goal") or "none")
    if goal == "profile_photo":
        return "start"
    return "none"


def handle_profile_photo_turn(
    user_message: str,
    *,
    session_ctx: dict[str, Any],
    slots: dict[str, Any] | None,
    user_id: str | None,
    phone_verified: bool,
    is_anonymous: bool,
) -> tuple[str, dict[str, Any]] | None:
    """Profile photo sub-flow driven by Flash slots. None if unrelated."""
    msg = str(user_message or "").strip()
    if not msg:
        return None

    phase = str(session_ctx.get("routing_phase") or "")
    if not slots_want_profile_photo(slots or {}, routing_phase=phase):
        return None

    action = _profile_photo_action(slots)
    in_phase = phase == PHASE_AWAIT_PROFILE_PHOTO
    goal = str((slots or {}).get("goal") or "none")
    conf = float((slots or {}).get("confidence", 0.0))

    if in_phase and goal not in ("profile_photo", "none", "chat") and conf >= 0.6:
        ctx = {**session_ctx, "routing_phase": "listening", "profile_photo_intent": None}
        return ("No problem — what would you like to do next?", ctx)

    if action == "skip" or (
        in_phase
        and goal == "chat"
        and conf >= 0.65
    ):
        ctx = {**session_ctx, "routing_phase": "listening", "profile_photo_intent": None}
        return ("No problem — what would you like to do next?", ctx)

    if action == "done":
        if user_profile_photo_url(user_id):
            ctx = {**session_ctx, "routing_phase": "listening", "profile_photo_intent": None}
            return ("Looking good — your photo is on your profile now!", ctx)
        ctx = {
            **session_ctx,
            "routing_phase": PHASE_AWAIT_PROFILE_PHOTO,
            "profile_photo_intent": "upload",
        }
        return (
            "Tap **Add photo** below to choose from your gallery or take one.",
            ctx,
        )

    if is_anonymous and not phone_verified:
        return (
            "Verify your phone first — then I can save a profile photo for you.",
            {**session_ctx, "routing_phase": "listening"},
        )

    ctx = {
        **session_ctx,
        "routing_phase": PHASE_AWAIT_PROFILE_PHOTO,
        "profile_photo_intent": "upload",
        "unified_mode": True,
    }
    has_photo = bool(user_profile_photo_url(user_id))
    if has_photo and action in ("start", "accept"):
        return (
            "Sure — tap **Add photo** below to replace your current picture.",
            ctx,
        )
    return (
        "Great — tap **Add photo** below to choose from your gallery or take one.",
        ctx,
    )
