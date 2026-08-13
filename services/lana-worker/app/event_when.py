"""AI date/time resolution for the in-chat event-host flow.

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
import re
from datetime import date as _date, datetime
from typing import Any

_SYSTEM = """You resolve the DATE and TIME a neighbor wants for an event they are \
hosting, from their words and the conversation. You are given TODAY's date (with its \
weekday). Return ONE compact JSON object and nothing else:
{"date": "YYYY-MM-DD" or null, "time": "HH:MM" or null, \
"repeats": "weekly"|"biweekly"|"monthly" or null, "until": "YYYY-MM-DD" or null}

- Resolve natural and relative phrases against TODAY: "28th June", "the 28th", \
"next Friday", "tomorrow", "tonight", "this weekend", "next month".
- Always pick the NEXT FUTURE occurrence — never a past date. If a bare month/day has \
already passed this year, use next year.
- Honor negation and corrections: "not on friday", "actually make it the 28th", \
"change it to Sunday".
- time is 24-hour "HH:MM": "9pm" -> "21:00", "9 in the night" -> "21:00", \
"noon" -> "12:00", "morning" -> "10:00", "evening"/"night" -> "18:00".
- repeats is for a RECURRING meet the host wants on a cadence: "every Friday" -> \
"weekly", "every other Saturday"/"fortnightly" -> "biweekly", "first Sunday of the \
month"/"monthly" -> "monthly". date is still the FIRST occurrence. A one-time meet, \
even a far-off one, has repeats null — "this Friday" is not "every Friday".
- until is when a recurring meet should STOP, only if they said so: "every Friday \
through August" -> the last day of August, "for the next 6 weeks" -> that date. Null \
when they gave no end.
- Return a value ONLY when THIS message states or changes it. Use null to leave the \
existing draft value untouched — never echo the draft back as if it were new.

Never invent a date, time, or cadence the user did not express."""

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
    """Resolve the date/time (and recurrence) the user expressed THIS turn.

    Returns ``{"date": "YYYY-MM-DD", "time": "HH:MM", "repeats": "weekly",
    "until": "YYYY-MM-DD"}`` (any subset the user expressed),
    or ``None`` when the LLM is unavailable / errored so the caller falls back to regex.
    An empty dict means the model ran but saw no date/time change this turn.
    """
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return None
        today = now or datetime.now()
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
        # Recurring meets: the cadence and (optional) end date. Whitelisted — anything
        # outside the three the DB accepts is dropped, so the meet publishes as a one-off
        # rather than blowing up at publish on a check constraint.
        raw_repeats = str(data.get("repeats") or "").strip().lower()
        if raw_repeats in ("weekly", "biweekly", "monthly"):
            out["repeats"] = raw_repeats
        raw_until = data.get("until")
        if isinstance(raw_until, str) and _ISO_DATE_RE.match(raw_until.strip()):
            snapped = _snap_future(raw_until.strip(), today.date())
            if snapped:
                out["until"] = snapped
        return out
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to regex
        import logging

        logging.getLogger(__name__).exception("resolve_event_when_failed")
        return None
