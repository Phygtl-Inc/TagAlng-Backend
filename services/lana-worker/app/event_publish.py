import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


_DEFAULT_EVENT_TZ = "America/New_York"


def event_tz() -> ZoneInfo:
    """The timezone a host's wall-clock time ("6 PM") is anchored to — and the one
    event times are rendered back in (browse filter, date matching). Single-region
    today (Orlando / Eastern); override with EVENT_DEFAULT_TZ when that changes."""
    name = (os.environ.get("EVENT_DEFAULT_TZ") or _DEFAULT_EVENT_TZ).strip() or _DEFAULT_EVENT_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_EVENT_TZ)


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
            dt = dt.replace(tzinfo=event_tz())
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


_last_roll_at = 0.0
_ROLL_EVERY_S = 60.0


def roll_recurring_events() -> None:
    """Read-repair for recurring meets: advance any series whose occurrence has passed,
    and close ones past their end date. A recurring meet is ONE row carrying its NEXT
    occurrence, so without this the row falls behind the `starts_at > now()` floor every
    list query uses and the weekly meet quietly vanishes. No scheduler: browse does it.

    Best-effort — a chat turn must never fail because housekeeping did.
    """
    # ponytail: per-process 60s throttle, so a busy hour is ~1 update/minute/worker.
    # The RPC is idempotent, so overlapping workers are harmless. Swap in a real
    # scheduler only if the due-scan ever shows up in the latency budget.
    global _last_roll_at
    now = time.monotonic()
    if now - _last_roll_at < _ROLL_EVERY_S:
        return
    _last_roll_at = now
    try:
        service_client().rpc("roll_recurring_events", {}).execute()
    except Exception:  # noqa: BLE001 - housekeeping never blocks a turn
        logging.getLogger(__name__).exception("roll_recurring_events_failed")


def _parse_until_date(raw: str | None) -> str | None:
    """The host's "every Friday through August" end date, as a plain YYYY-MM-DD. A past
    date is dropped rather than published — it would end the series on its first roll."""
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        end = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return end.isoformat() if end >= datetime.now(event_tz()).date() else None


def _ai_cover_emoji(title: str, description: str | None) -> str | None:
    """Best-effort emoji pick for publish paths that skipped the setup-suggest call
    (voice / transcript extraction). One tiny LLM call; None on any failure — the FE
    falls back to its neutral icon, never a canned emoji that could clash with the vibe."""
    try:
        from app.lana_ui import sanitize_cover_emoji
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "Pick ONE emoji that captures this local event's vibe, for its card "
                'cover. Reply with compact JSON: {"cover_emoji": "..."}. Match the '
                "actual activity (soccer -> ⚽, book club -> 📚, coffee walk -> ☕)."
            ),
            user_payload=f"TITLE: {title}\nDESCRIPTION: {(description or '').strip()[:300]}",
            max_tokens=20,
            temperature=0.2,
        )
        if not isinstance(data, dict):
            return None
        return sanitize_cover_emoji(data.get("cover_emoji"))
    except Exception:  # noqa: BLE001 - cover art must never block publishing
        return None


def _ai_event_description(title: str, draft: EventDraft) -> str | None:
    """Best-effort card blurb for publish paths where the chat flow never drafted one
    (sparse opening → setup carousel, or an LLM miss on the entry turn). Grounded ONLY
    in facts the host actually set; None on any failure — a missing description must
    never block publishing, and no description beats a canned line."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        facts = {
            "title": title,
            "venue_name": (draft.venue_name or "").strip() or None,
            "starts_at": draft.starts_at,
            "bring_items": [str(b).strip() for b in (draft.bring_items or []) if str(b).strip()][:6],
            "cohort_tags": list(draft.cohort_tags or [])[:4],
        }
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "Write ONE short warm sentence (<=120 chars) describing this local "
                "event for its card, e.g. \"Saturday morning coffee — open to "
                'first-time Brazilian moms." Reply with compact JSON: '
                '{"description": "..."}. Ground it ONLY in the facts given — never '
                "invent details the host didn't set."
            ),
            user_payload=json.dumps(facts, ensure_ascii=False),
            max_tokens=80,
            temperature=0.3,
        )
        if not isinstance(data, dict):
            return None
        return str(data.get("description") or "").strip()[:500] or None
    except Exception:  # noqa: BLE001 - description must never block publishing
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
    description = (draft.description or "").strip()[:500] or None
    if not description:
        description = _ai_event_description(title[:80], draft)
    fields: dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "title": title[:80],
        "description": description,
        "venue_name": (draft.venue_name or "").strip()[:120] or None,
        "venue_address": (draft.venue_address or "").strip()[:300] or None,
        "place_id": (draft.place_id or "").strip()[:300] or None,
        "cohort_tags": _filter_cohort_tags(draft.cohort_tags),
        "block_id": block_id,
    }
    starts = _parse_iso_ts(draft.starts_at)
    if starts:
        fields["starts_at"] = starts
        # Truthful clock flag: the pipeline stamps has_time from the when-resolver
        # (True only when the host actually said a time). Absent = legacy draft →
        # keep the DB default (true) rather than guessing here.
        if draft.has_time is not None:
            fields["has_time"] = bool(draft.has_time)
    ends = _parse_iso_ts(draft.ends_at)
    if ends:
        fields["ends_at"] = ends
    # Recurrence: the stored row IS the next occurrence and rolls forward from here
    # (roll_recurring_events). Whitelisted, so a hallucinated cadence ("daily") publishes
    # a normal one-off meet instead of failing the host's publish on a check constraint.
    if draft.recurrence in ("weekly", "biweekly", "monthly"):
        fields["recurrence"] = draft.recurrence
        until = _parse_until_date(draft.recurrence_until)
        if until:
            fields["recurrence_until"] = until
    if draft.max_attendees is not None:
        cap = int(draft.max_attendees)
        if 1 <= cap <= 200:
            fields["max_attendees"] = cap
    if draft.auto_approve is not None:
        fields["auto_approve"] = bool(draft.auto_approve)
    if draft.allow_attendee_share is not None:
        fields["allow_attendee_share"] = bool(draft.allow_attendee_share)
    from app.lana_ui import is_none_bring_item

    bring = [
        str(b).strip()[:60]
        for b in (draft.bring_items or [])
        if str(b).strip() and not is_none_bring_item(str(b))
    ][:12]
    if bring:
        fields["bring_items"] = bring
    from app.lana_ui import sanitize_cover_emoji

    cover_emoji = sanitize_cover_emoji(draft.cover_emoji) or _ai_cover_emoji(
        fields["title"], fields["description"]
    )
    if cover_emoji:
        fields["cover_emoji"] = cover_emoji
    if cohost_id:
        fields["cohost_id"] = cohost_id
    return fields


def skip_event_occurrence(event_id: str, user_jwt: str) -> str | None:
    """Call off ONE occurrence of a recurring meet. Returns the meet's new start, or None
    when that was the last one (the RPC then closes the series). Host-only, enforced in
    the RPC — this runs with the host's own JWT, never service role."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="server_misconfigured")

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/skip_event_occurrence",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {user_jwt}",
                "Content-Type": "application/json",
            },
            json={"p_event_id": event_id},
        )
    if res.status_code >= 400:
        detail = res.text[:300]
        # The RPC's own refusals are the host's answer ("that meet doesn't repeat"), not
        # a server fault — pass them through as 400s the FE can render.
        for known in (
            "not_event_host",
            "not_recurring",
            "event_not_open",
            "event_not_found",
            "occurrence_conflict",
        ):
            if known in detail:
                raise HTTPException(status_code=400, detail=known)
        raise HTTPException(status_code=502, detail=f"skip_occurrence_failed:{res.status_code}")

    nxt = res.json()
    return str(nxt) if nxt else None


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
        raise HTTPException(status_code=502, detail=f"create_event_failed:{res.status_code}")

    event_id = res.json()
    if not event_id:
        raise HTTPException(status_code=502, detail="create_event_empty_id")
    # Anchor the event to its canonical place (Place Profile §2.4) — dual-write next
    # to the legacy place_id string. Background: publish never waits on Google.
    try:
        from app.event_place import stamp_event_place_async

        stamp_event_place_async(str(event_id), fields.get("place_id"), user_id)
    except Exception:  # noqa: BLE001 - anchoring must never block publishing
        import logging

        logging.getLogger(__name__).exception("event_place_stamp_kickoff_failed")
    # The community the host picked on the setup card (2/5): tag the meet and email that
    # community's members. Background for the same reason — a mail-out never gates publish.
    try:
        from app.event_place import stamp_event_community_async

        stamp_event_community_async(
            str(event_id), draft.circle_place_id, user_id, fields["title"]
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("event_community_stamp_kickoff_failed")
    return str(event_id)
