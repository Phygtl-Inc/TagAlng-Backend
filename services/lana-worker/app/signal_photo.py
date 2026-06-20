"""Pass-along item photo upload — Supabase `signal-photos` bucket.

Mirrors app.profile_photo, but items can have many photos so each upload gets a
unique filename (no overwrite) under `signal-photos/{user_id}/{uuid}.{ext}`.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.auth import SUPABASE_URL, service_client

_ALLOWED_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_MAX_BYTES = 2 * 1024 * 1024


def public_signal_photo_url(user_id: str, name: str) -> str:
    base = (SUPABASE_URL or "").rstrip("/")
    return f"{base}/storage/v1/object/public/signal-photos/{user_id}/{name}"


def upload_signal_photo_bytes(
    user_id: str,
    file_bytes: bytes,
    content_type: str,
) -> str:
    if len(file_bytes) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="signal_photo_too_large")
    ext = _ALLOWED_TYPES.get(content_type)
    if not ext:
        raise HTTPException(status_code=415, detail="signal_photo_type_not_allowed")
    name = f"{uuid.uuid4().hex}.{ext}"
    path = f"{user_id}/{name}"
    sb = service_client()
    sb.storage.from_("signal-photos").upload(
        path,
        file_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return public_signal_photo_url(user_id, name)
