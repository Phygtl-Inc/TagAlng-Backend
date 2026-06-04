import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.auth import service_client
from app.event_location import resolve_event_location
from app.models import EventDraft

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


def _valid_purpose_ids() -> set[str]:
    sb = service_client()
    res = sb.rpc("get_event_purposes").execute()
    rows = res.data or []
    return {str(r["id"]) for r in rows if r.get("id")}


def _filter_cohort_tags(tags: list[str]) -> list[str]:
    allowed = _valid_purpose_ids()
    legacy = {
        "parents",
        "sports",
        "faith",
        "sober",
        "runner",
        "newcomer",
        "professional",
        "creative",
        "volunteer",
        "basketball",
        "soccer",
        "tennis",
        "pickleball",
        "running",
        "cycling",
        "swimming",
        "other",
    }
    allowed |= legacy
    out: list[str] = []
    for t in tags:
        tid = str(t).strip()
        if tid in allowed and tid not in out:
            out.append(tid)
    return out[:6]


def _parse_iso_ts(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def build_create_event_fields(user_id: str, draft: EventDraft) -> dict[str, Any]:
    title = (draft.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="event_title_required")

    lat, lng, block_id = resolve_event_location(user_id, draft.venue_name)
    fields: dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "title": title[:80],
        "description": (draft.description or "").strip()[:500] or None,
        "venue_name": (draft.venue_name or "").strip()[:120] or None,
        "cohort_tags": _filter_cohort_tags(draft.cohort_tags),
        "block_id": block_id,
    }
    starts = _parse_iso_ts(draft.starts_at)
    if starts:
        fields["starts_at"] = starts
    ends = _parse_iso_ts(draft.ends_at)
    if ends:
        fields["ends_at"] = ends
    if draft.max_attendees is not None:
        cap = int(draft.max_attendees)
        if 1 <= cap <= 200:
            fields["max_attendees"] = cap
    return fields


def publish_event(user_id: str, user_jwt: str, draft: EventDraft) -> str:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    fields = build_create_event_fields(user_id, draft)

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/create_event",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {user_jwt}",
                "Content-Type": "application/json",
            },
            json={"p_fields": fields},
        )
    if res.status_code >= 400:
        detail = res.text[:300]
        if "phone_not_verified" in detail.lower() or "phone_verified" in detail.lower():
            raise HTTPException(status_code=400, detail="phone_not_verified")
        raise HTTPException(status_code=502, detail=f"create_event_failed:{res.status_code}")

    event_id = res.json()
    if not event_id:
        raise HTTPException(status_code=502, detail="create_event_empty_id")
    return str(event_id)
