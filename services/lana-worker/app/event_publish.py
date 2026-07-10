import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from app.auth import service_client
from app.event_location import resolve_event_location
from app.event_when import event_tz as _event_tz
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


# QA accounts sign up with a plus-tag containing "qa" (t+lanaqa1@phygtl.com,
# t+qa2@phygtl.com, …). Events they create are stamped is_test=true so QA runs never
# dirty the member-facing feed again (the 2026-07-08 findings: junk rows in ~20/24
# QA result lists). Matched on the authed host's stored email — no header/env plumbing.
_QA_EMAIL_RE = re.compile(r"^[^@+]*\+[^@]*qa[^@]*@")


def is_qa_email(email: Any) -> bool:
    """True when `email` carries a '+…qa…' plus-tag (e.g. t+lanaqa1@phygtl.com)."""
    return bool(_QA_EMAIL_RE.match(str(email or "").strip().lower()))


def _host_is_qa_account(user_id: str) -> bool:
    """Whether the host's account email flags them as QA. Best-effort: any lookup
    failure means NOT QA — a real member's event must never be silently hidden."""
    if not user_id:
        return False
    try:
        res = (
            service_client()
            .table("users")
            .select("email")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        return is_qa_email(row.get("email")) if isinstance(row, dict) else False
    except Exception:  # noqa: BLE001 - fence is best-effort, never breaks publish
        return False


# The tz anchor moved to app.event_when.event_tz so parsing (here) and every
# human-facing render share ONE definition of the event's wall clock.


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
            # The host typed a wall-clock time with no zone (e.g. "6 PM"). It means 6 PM
            # in the EVENT's local timezone, not UTC — anchor it there before converting
            # to the stored UTC instant, so the saved time is the time they intended.
            dt = dt.replace(tzinfo=_event_tz())
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def build_create_event_fields(
    user_id: str,
    draft: EventDraft,
    *,
    cohost_id: str | None = None,
) -> dict[str, Any]:
    title = (draft.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="event_title_required")

    # Prefer the EXACT picked place's coordinates (Google Places); only re-geocode the
    # name when we don't have them — so the saved pin is the place the host actually chose.
    if draft.venue_lat is not None and draft.venue_lng is not None:
        _, _, block_id = resolve_event_location(user_id, None)
        lat, lng = float(draft.venue_lat), float(draft.venue_lng)
    else:
        lat, lng, block_id = resolve_event_location(user_id, draft.venue_name)
    fields: dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "title": title[:80],
        "description": (draft.description or "").strip()[:500] or None,
        "venue_name": (draft.venue_name or "").strip()[:120] or None,
        "venue_address": (draft.venue_address or "").strip()[:300] or None,
        "place_id": (draft.place_id or "").strip()[:300] or None,
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
    if draft.auto_approve is not None:
        fields["auto_approve"] = bool(draft.auto_approve)
    if draft.allow_attendee_share is not None:
        fields["allow_attendee_share"] = bool(draft.allow_attendee_share)
    bring = [str(b).strip()[:60] for b in (draft.bring_items or []) if str(b).strip()][:12]
    if bring:
        fields["bring_items"] = bring
    if cohost_id:
        fields["cohost_id"] = cohost_id
    # QA fence: test-account events are stamped is_test so they never reach the feed.
    if _host_is_qa_account(user_id):
        fields["is_test"] = True
    return fields


def _is_duplicate_event_error(detail: str) -> bool:
    """Whether a create_event failure is the dedupe-guard unique violation — either the
    RPC's mapped 'duplicate_event' or a raw 23505 that slipped through unmapped."""
    lower = str(detail or "").lower()
    return (
        "duplicate_event" in lower
        or "23505" in lower
        or "duplicate key value" in lower
        or "events_host_title_starts_live_uniq" in lower
    )


def publish_event(
    user_id: str,
    user_jwt: str,
    draft: EventDraft,
    *,
    cohost_id: str | None = None,
) -> str:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    fields = build_create_event_fields(user_id, draft, cohost_id=cohost_id)

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
        if _is_duplicate_event_error(detail):
            # The dedupe guard (partial unique index) fired: same host, title, start.
            raise HTTPException(status_code=409, detail="duplicate_event")
        raise HTTPException(status_code=502, detail=f"create_event_failed:{res.status_code}")

    event_id = res.json()
    if not event_id:
        raise HTTPException(status_code=502, detail="create_event_empty_id")
    return str(event_id)
