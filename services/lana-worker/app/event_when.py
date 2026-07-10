"""Event date/time — AI resolution (host flow) + THE one tz-aware formatter.

Everything user-facing that turns a ``starts_at`` into words lives here:
``format_event_when`` renders a stored UTC instant (or a naive event-local draft
value) in the EVENT's timezone, in one of two styles — so the chat sentence, the
preview card label, and anything fed to an LLM all say the same local date. Never
strftime a raw ``starts_at`` elsewhere: QA saw one event render as three different
datetimes because each surface formatted the UTC instant its own way.

The host flow needs the event's start as an absolute (calendar date + clock time). A
regex resolver used to do this, but it choked on the things people actually type —
ordinals ("28th June"), negation ("not on friday"), and relative phrasing — which an
LLM handles trivially. The reason an earlier raw-LLM attempt was abandoned (and the
regex bolted on) was that the extractor mis-guessed the YEAR and dropped the time-of-day;
the fix for that is to ANCHOR the model on today's date and ask for a normalized
{date, time}. A thin deterministic guard then snaps any past date to its next future
occurrence — the one thing the model is unreliable at.

Best-effort: returns ``None`` when the LLM is unavailable or errors, so the caller can
fall back to the regex resolver; returns a (possibly empty) dict when the model ran, so
"the model saw no date this turn" is distinct from "no model" — and a stray weekday in a
phrase like "not on friday" is never re-matched by the fallback regex.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date as _date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── Timezone anchor ──────────────────────────────────────────────────────────────
#
# Single-region today (Orlando / Eastern); override with EVENT_DEFAULT_TZ when that
# changes. Block/user-level timezones can be threaded through the `tz` parameter of
# format_event_when once they exist in the data.

DEFAULT_EVENT_TZ = "America/New_York"


def event_tz(name: str | None = None) -> ZoneInfo:
    """The timezone a host's wall-clock time ("6 PM") — and every human-facing render
    of a stored UTC instant — is anchored to. Falls back to Eastern on a bad name."""
    tz_name = (name or os.environ.get("EVENT_DEFAULT_TZ") or "").strip() or DEFAULT_EVENT_TZ
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(DEFAULT_EVENT_TZ)


def _coerce_tz(tz: str | ZoneInfo | None) -> ZoneInfo:
    if isinstance(tz, ZoneInfo):
        return tz
    return event_tz(tz)


def event_now(tz: str | ZoneInfo | None = None) -> datetime:
    """Now on the EVENT's wall clock — use instead of datetime.now(): the worker runs
    in UTC, so the server's "today" flips hours before/after the block's does."""
    return datetime.now(_coerce_tz(tz))


def event_local_dt(raw: Any, tz: str | ZoneInfo | None = None) -> datetime | None:
    """Parse a ``starts_at`` into an aware datetime on the event's wall clock.

    Aware inputs (stored rows: ``...+00:00`` / ``...Z``) are converted to the event tz;
    naive inputs (in-flight drafts, e.g. ``2026-07-11T18:00:00``) already MEAN event-local
    wall-clock time (see event_publish._parse_iso_ts) and are anchored as-is, not shifted.
    None when unparseable.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    zone = _coerce_tz(tz)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def format_event_when(
    raw: Any,
    tz: str | ZoneInfo | None = None,
    style: str = "card",
) -> str | None:
    """THE tz-aware human render of a ``starts_at``. Two styles:

    - ``card``:   ``Mon, Jul 13 · 8:30 PM``  (preview-card label)
    - ``inline``: ``Mon Jul 13, 8:30 PM``    (mid-sentence chat text)

    A bare date (``2026-07-13``) renders without a clock. Unparseable input degrades
    to its first 10 chars (old behavior) rather than erroring; empty input is None.
    """
    if style not in ("card", "inline"):
        raise ValueError(f"unknown format_event_when style: {style!r}")
    s = str(raw or "").strip()
    if not s:
        return None
    dt = event_local_dt(s, tz)
    if dt is None:
        return s[:10] if len(s) >= 10 else s
    day = (
        f"{dt.strftime('%a')}, {dt.strftime('%b')} {dt.day}"
        if style == "card"
        else f"{dt.strftime('%a %b')} {dt.day}"
    )
    if _DATE_ONLY_RE.match(s):
        return day
    hour12 = dt.hour % 12 or 12
    clock = f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"
    return f"{day} · {clock}" if style == "card" else f"{day}, {clock}"



def event_local_now(utc_now: datetime | None = None) -> datetime:
    """The host's wall-clock "now", anchored to the EVENT timezone — never the server's
    clock (UTC on Cloud Run). Grounding "tomorrow"/"thursday" on the server's UTC day
    shifted every evening turn one day forward (QA Wed 2026-07-08: "tomorrow" drafted
    Friday, "thursday" skipped a week). Returns a naive local datetime, matching the
    draft's naive-local starts_at convention. `utc_now` is a test seam."""
    from app.event_publish import _event_tz  # lazy — avoids import-time env coupling

    base = utc_now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(_event_tz()).replace(tzinfo=None)

_SYSTEM = """You resolve the DATE and TIME a neighbor wants for an event they are \
hosting, from their words and the conversation. You are given TODAY's date (with its \
weekday). Return ONE compact JSON object and nothing else:
{"date": "YYYY-MM-DD" or null, "time": "HH:MM" or null}

- Resolve natural and relative phrases against TODAY: "28th June", "the 28th", \
"next Friday", "tomorrow", "tonight", "this weekend", "next month".
- Always pick the NEXT FUTURE occurrence — never a past date. If a bare month/day has \
already passed this year, use next year.
- A bare weekday names the SOONEST such day: if TODAY is Wednesday, "thursday" is \
TOMORROW — never the week after. "tomorrow" is exactly TODAY + 1 day.
- Honor negation and corrections: "not on friday", "actually make it the 28th", \
"change it to Sunday".
- time is 24-hour "HH:MM": "9pm" -> "21:00", "9 in the night" -> "21:00", \
"noon" -> "12:00", "morning" -> "10:00", "evening"/"night" -> "18:00", \
"sunrise" -> "06:30".
- Return a value ONLY when THIS message states or changes it. Use null to leave the \
existing draft value untouched — never echo the draft back as if it were new.

Never invent a date or time the user did not express."""

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _snap_future(iso_date: str, today: _date) -> str | None:
    """Thin guard: parse the model's date; bump a past date to next year (covers a bare
    month/day already gone by this year). None if unparseable or still in the past."""
    try:
        d = _date.fromisoformat(iso_date)
    except ValueError:
        return None
    if d < today:
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:  # Feb 29 → non-leap year
            return None
    return d.isoformat() if d >= today else None


def resolve_event_when(
    *,
    history: list[dict[str, Any]],
    user_message: str,
    draft: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, str] | None:
    """Resolve the date/time the user expressed THIS turn.

    Returns ``{"date": "YYYY-MM-DD", "time": "HH:MM"}`` (any subset the user expressed),
    or ``None`` when the LLM is unavailable / errored so the caller falls back to regex.
    An empty dict means the model ran but saw no date/time change this turn.
    """
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        # Anchor on the HOST's local day, not the server's UTC day — a Wednesday-evening
        # turn is Thursday in UTC, which mis-grounded every relative date by one day.
        today = now or event_now()
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        payload = "\n\n".join(
            [
                f"TODAY: {today.strftime('%A, %Y-%m-%d')}",
                "EVENT DRAFT SO FAR:\n"
                + json.dumps({"starts_at": draft.get("starts_at")}, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                f"USER'S NEW MESSAGE:\n{str(user_message or '').strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_SYSTEM,
            user_payload=payload,
            max_tokens=80,
            temperature=0.0,
        )
        if not isinstance(data, dict):
            return None
        out: dict[str, str] = {}
        raw_date = data.get("date")
        if isinstance(raw_date, str) and _ISO_DATE_RE.match(raw_date.strip()):
            snapped = _snap_future(raw_date.strip(), today.date())
            if snapped:
                out["date"] = snapped
        raw_time = data.get("time")
        if isinstance(raw_time, str) and _HHMM_RE.match(raw_time.strip()):
            hh, mm = raw_time.strip().split(":")
            out["time"] = f"{int(hh):02d}:{mm}"
        return out
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to regex
        import logging

        logging.getLogger(__name__).exception("resolve_event_when_failed")
        return None
