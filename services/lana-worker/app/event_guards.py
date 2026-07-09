"""Pre-publish sanity guards for the host flow — warm clarifying questions, never
hard rejections.

Production QA (2026-07-08) found implausible meets reaching the feed: a playdate whose
pinned venue geocoded to New York attached to a Lake Nona block, and a kids birthday
party starting at midnight local. The geocoder resolves NAMES well — its result was
just never cross-checked against the host's block, and nothing sanity-checked kid-event
hours. These guards catch both at the publish gate every host path funnels through.

Design rules:
- Each guard is a QUESTION ("is that right, or did you mean …?") — the host may
  genuinely mean it, and one confirmation proceeds.
- The pipeline remembers answers in ``event_guards_confirmed`` (session context), so a
  confirmed guard is never re-asked (no loop).
- Best-effort: a guard that can't compute (no pin, no centroid, bad timestamp) stays
  silent rather than blocking a publish.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

# Guard ids — stored in session ctx (event_guard_pending / event_guards_confirmed).
GUARD_FAR_VENUE = "far_venue"
GUARD_KID_HOURS = "kid_hours"

# A meet is block-scale (5-minute-walk vicinity); anything this far from the block
# centroid is almost certainly a geocode landing in the wrong city (the NY playdate).
FAR_VENUE_KM = 40.0

# Local quiet hours for kid/family events: 21:00 → 06:00 ("midnight birthday" QA case).
KID_QUIET_START_HOUR = 21
KID_QUIET_END_HOUR = 6


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km — coarse sanity distances, not navigation."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def host_block_centroid(user_id: str) -> tuple[float, float] | None:
    """Best-known coordinates of the host's block. Blocks store a PostGIS geography
    centroid the REST client can't read, so reuse the same resolution events use:
    users.home_zip → zip_centroids (plain lat/lng), dev-block fallback, pilot default.
    None only when the lookup itself fails (no DB in tests, etc.)."""
    try:
        from app.event_location import resolve_event_location

        lat, lng, _ = resolve_event_location(user_id, None)
        return (lat, lng)
    except Exception:  # noqa: BLE001 - best-effort; guard stays silent
        return None


def far_venue_km(draft: dict[str, Any], user_id: str) -> float | None:
    """How far a PINNED venue is from the host's block, when implausibly far.

    Returns the distance in km when the draft carries exact coordinates (Google pin)
    more than FAR_VENUE_KM from the block centroid, else None. An unpinned venue needs
    no check — publish resolves it biased to the block by construction."""
    lat, lng = draft.get("venue_lat"), draft.get("venue_lng")
    if lat is None or lng is None:
        return None
    centroid = host_block_centroid(user_id)
    if centroid is None:
        return None
    try:
        km = haversine_km(float(lat), float(lng), centroid[0], centroid[1])
    except (TypeError, ValueError):
        return None
    return km if km > FAR_VENUE_KM else None


# Child-centered words only — deliberately NOT "moms"/"parents" (a moms' wine night at
# 9:30 PM is a normal adult meet; a toddler playdate at 11 PM is not).
_KID_RE = re.compile(
    r"\b(?:kid|kids|kiddo?s?|child|children|toddlers?|bab(?:y|ies)|infants?|"
    r"preschool(?:ers?)?|playdate|play\s?date|playgroup|play\s?group|famil(?:y|ies))\b",
    re.I,
)


def _is_kid_event(draft: dict[str, Any]) -> bool:
    """Kid/family event — by cohort tag first, then title/description wording."""
    for tag in draft.get("cohort_tags") or []:
        if _KID_RE.search(str(tag)):
            return True
    text = f"{draft.get('title') or ''} {draft.get('description') or ''}"
    return bool(_KID_RE.search(text))


def _local_start(draft: dict[str, Any]) -> datetime | None:
    """The draft's start as a LOCAL wall-clock datetime, anchored the same way publish
    anchors it: naive timestamps mean the event's local timezone; aware ones (the
    QA case's 04:00Z = midnight ET) are converted into it."""
    raw = str(draft.get("starts_at") or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    from app.event_publish import _event_tz

    tz = _event_tz()
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def kid_quiet_hours_start(draft: dict[str, Any]) -> datetime | None:
    """The local start datetime when a kid/family event begins in quiet hours
    (21:00–06:00 local) — else None."""
    if not _is_kid_event(draft):
        return None
    local = _local_start(draft)
    if local is None:
        return None
    if local.hour >= KID_QUIET_START_HOUR or local.hour < KID_QUIET_END_HOUR:
        return local
    return None


def _clock_label(dt: datetime) -> str:
    """"11 PM" / "12:30 AM" — manual formatting (%-I is not portable)."""
    hour12 = dt.hour % 12 or 12
    minutes = f":{dt.minute:02d}" if dt.minute else ""
    return f"{hour12}{minutes} {'AM' if dt.hour < 12 else 'PM'}"


def pending_event_guard(
    draft: dict[str, Any], user_id: str, *, confirmed: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The first unconfirmed guard this draft trips, as a warm clarifying question:
    ``{"id", "question", "options"}`` — or None when the draft is clear to publish.

    ``confirmed`` is the host's remembered answers (guard id → True); a confirmed
    guard never re-asks, so one confirmation proceeds."""
    ok = confirmed or {}

    if not ok.get(GUARD_FAR_VENUE):
        km = far_venue_km(draft, user_id)
        if km is not None:
            venue = str(draft.get("venue_name") or "that spot").strip() or "that spot"
            return {
                "id": GUARD_FAR_VENUE,
                "question": (
                    f"Quick check before I post it — **{venue}** looks about "
                    f"{km:.0f} km from your block. Is that right, or did you mean "
                    "somewhere nearby?"
                ),
                "options": ["Yes, that's the spot", "Pick somewhere nearby"],
            }

    if not ok.get(GUARD_KID_HOURS):
        local = kid_quiet_hours_start(draft)
        if local is not None:
            label = _clock_label(local)
            return {
                "id": GUARD_KID_HOURS,
                "question": (
                    f"Quick check — this starts at {label}, which is pretty late for "
                    f"the kids. Did you mean noon, or is {label} right?"
                ),
                "options": ["Make it noon", f"Keep it at {label}"],
            }

    return None


# Answer reading for a pending guard question. "change" wins only when no confirm word
# is present (and vice versa); a mixed/unclear answer returns None so the flow holds
# instead of guessing.
_GUARD_CONFIRM_RE = re.compile(
    r"\b(?:yes|yep|yeah|yup|correct|right|keep|sure|confirmed?|intentional|"
    r"on purpose|that'?s the spot)\b",
    re.I,
)
_GUARD_CHANGE_RE = re.compile(
    r"\b(?:no|nope|nah|wrong|mistake|change|nearby|somewhere else|different|"
    r"noon|meant|make it|fix|move|actually)\b",
    re.I,
)


def classify_guard_answer(message: str) -> str | None:
    """Read the host's reply to a guard question: "confirm" (proceed as-is), "change"
    (they want to fix the flagged detail), or None when unclear."""
    text = str(message or "").strip()
    if not text:
        return None
    change = bool(_GUARD_CHANGE_RE.search(text))
    confirm = bool(_GUARD_CONFIRM_RE.search(text))
    if change and not confirm:
        return "change"
    if confirm and not change:
        return "confirm"
    return None
