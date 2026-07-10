"""Deterministic availability/need constraint memory.

Production QA (2026-07-08): a mom who said "single working mom — I can only do
evenings after 6 or weekends" was shown "Morning meetup" and 10 AM coffee mornings,
and the constraint was never acknowledged. A triple ask ("I need a double stroller,
want a walk buddy, and want to host a bbq") silently dropped two of the three needs.

This module fixes both, deterministically (regex — no LLM dependency):

  * ``extract_constraints_from_message`` — time windows (evenings/mornings/afternoons,
    "after 6", weekends/weekdays) and kid age bands, captured on ANY turn.
  * ``capture_constraints_for_turn`` — stores constraints in session context (all
    users) and durably in the ``user_constraints`` table (verified users), and builds
    the "Evenings after 6pm or weekends — noted." acknowledgment.
  * ``filter_events_by_constraints`` — applied wherever event result sets are
    assembled (activity browse + activities preview), so a 10 AM event never reaches
    an evenings-only mom; ``constraints_all_filtered_note`` is the graceful reply
    when the filter removes everything.
  * ``detect_multi_needs`` — enumerations of multiple intents; the extras are parked
    in session state (``parked_needs`` / ``multi_need_stack``) and named in the
    acknowledgment instead of silently vanishing. Full goal-stack resume is a
    follow-up PR — here we only guarantee nothing is dropped silently.

Storage choice (durable): a dedicated ``user_constraints`` table — one ACTIVE row per
(user, kind), service-role writes, RLS select-own (the pending_event_drafts /
latent_signals conventions). Neither existing store fits: ``user_identity_claims`` is
label-text only (no structured jsonb payload the event filter can read, and a
constraint is not an identity thread to embed/match on), and ``latent_signals`` is by
its own charter an append-only observation firehose ("collect, don't surface"), not a
current-state store a filter reads on every turn.

Kid age bands are captured, acknowledged, and persisted, but only TIME windows filter
events today — events carry no structured age data to filter against.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

TIME_KIND = "availability_time"
KID_AGE_KIND = "kid_age"

# Session-context keys. `user_constraints` / `multi_need_stack` / `parked_needs` are
# durable across turns (plain keys survive merge_session_context); the ack key is
# per-turn and popped by finalize_constraint_turn before the context persists.
CONSTRAINT_CTX_KEY = "user_constraints"
ACK_CTX_KEY = "constraint_ack_pending"
MULTI_NEED_STACK_KEY = "multi_need_stack"
PARKED_NEEDS_KEY = "parked_needs"

_MORNING_END = 12 * 60
_AFTERNOON_START = 12 * 60
_EVENING_START = 17 * 60

# ─────────────────────────────── time windows ───────────────────────────────

# Availability phrasing gate — a statement about WHEN the user can do things, so a
# plain browse query ("anything this weekend?") never becomes a durable constraint.
_AVAILABILITY_MARKER_RE = re.compile(
    r"\b(?:can|could)\s+only\s+(?:do|make|manage|meet)\b"
    r"|\bonly\s+(?:free|available)\b"
    r"|\b(?:i'?m|i\s+am|we'?re|we\s+are)\s+(?:only\s+)?(?:free|available)\b"
    r"|\b(?:evenings?|mornings?|afternoons?|weekends?|weekdays?)\s+only\b"
    r"|\bonly\s+(?:do\s+|on\s+)?(?:evenings?|mornings?|afternoons?|weekends?|weekdays?)\b"
    r"|\b(?:evenings?|mornings?|afternoons?|weekends?|weekdays?)\b[^.!?]{0,24}"
    r"\bworks?\s+(?:best\s+)?for\s+(?:me|us)\b"
    r"|\bcan'?t\s+(?:do|make)\s+(?:mornings?|afternoons?|evenings?|weekdays?|weekends?)\b",
    re.IGNORECASE,
)

_AFTER_RE = re.compile(r"\bafter\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", re.I)
_BEFORE_RE = re.compile(r"\bbefore\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", re.I)
_CANT_DO_RE = re.compile(
    r"\bcan'?t\s+(?:do|make)\s+(mornings?|afternoons?|evenings?|weekdays?|weekends?)\b",
    re.I,
)
# "weekday evenings" / "weekend mornings" — day AND period combined into one window.
_DAY_PERIOD_RE = re.compile(
    r"\b(weekdays?|weekends?)\s+(mornings?|afternoons?|evenings?)\b", re.I
)


def _fmt_minute(minute: int) -> str:
    h, m = divmod(int(minute), 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}" if m else f"{h12}{suffix}"


def _clock_minute(hour: str, minute: str | None, meridiem: str | None, low: str) -> int:
    h = int(hour)
    m = int(minute or 0)
    mer = str(meridiem or "").replace(".", "").lower()
    if mer == "pm":
        h = h % 12 + 12
    elif mer == "am":
        h = h % 12
    elif "evening" in low and h <= 11:
        h += 12  # "evenings after 6" means 6 PM
    elif h <= 8:
        h += 12  # a bare small hour ("after 6") reads as evening, not 6 AM
    return h * 60 + m


def _first_minute(pattern: re.Pattern[str], low: str) -> int | None:
    m = pattern.search(low)
    if not m:
        return None
    try:
        return _clock_minute(m.group(1), m.group(2), m.group(3), low)
    except (TypeError, ValueError):
        return None


def _period_window(
    period: str, after_min: int | None, before_min: int | None
) -> tuple[dict[str, Any], str]:
    """One window + human label for a period word, honoring an explicit after/before."""
    if period == "evening":
        start = after_min if (after_min is not None and after_min >= _AFTERNOON_START) else _EVENING_START
        label = "evenings"
        if after_min is not None and start == after_min:
            label += f" after {_fmt_minute(start)}"
        return {"days": "any", "start_minute": start, "end_minute": None}, label
    if period == "morning":
        end = before_min if (before_min is not None and before_min <= _MORNING_END) else _MORNING_END
        start = after_min if (after_min is not None and after_min < _MORNING_END) else None
        return {"days": "any", "start_minute": start, "end_minute": end}, "mornings"
    # afternoon
    start = (
        after_min
        if (after_min is not None and _AFTERNOON_START <= after_min < _EVENING_START)
        else _AFTERNOON_START
    )
    return {"days": "any", "start_minute": start, "end_minute": _EVENING_START}, "afternoons"


def _parse_time_windows(text: str) -> tuple[list[dict[str, Any]], str]:
    """Parse the message into OR-combined availability windows + a human label.

    "evenings after 6 or weekends" → [{any, start 18:00}, {weekend}] — an event fits
    if it lands inside ANY window. "weekday evenings" is one combined (AND) window.
    """
    low = str(text or "").lower()
    windows: list[dict[str, Any]] = []
    labels: list[str] = []

    after_min = _first_minute(_AFTER_RE, low)
    before_min = _first_minute(_BEFORE_RE, low)

    negated = {m.group(1).rstrip("s") for m in _CANT_DO_RE.finditer(low)}

    consumed: set[str] = set()
    for m in _DAY_PERIOD_RE.finditer(low):
        day = "weekday" if m.group(1).lower().startswith("weekday") else "weekend"
        period = m.group(2).lower().rstrip("s")
        if day in negated or period in negated:
            continue
        w, lbl = _period_window(period, after_min, before_min)
        w["days"] = day
        windows.append(w)
        labels.append(f"{day} {lbl}")
        consumed.add(day)
        consumed.add(period)

    had_period = bool(consumed & {"morning", "afternoon", "evening"})
    for period in ("morning", "afternoon", "evening"):
        if period in consumed or period in negated:
            continue
        if re.search(rf"\b{period}s?\b", low):
            w, lbl = _period_window(period, after_min, before_min)
            windows.append(w)
            labels.append(lbl)
            had_period = True

    for day in ("weekend", "weekday"):
        if day in consumed or day in negated:
            continue
        if re.search(rf"\b{day}s?\b", low):
            w: dict[str, Any] = {"days": day, "start_minute": None, "end_minute": None}
            lbl = f"{day}s"
            if not had_period:
                # "weekdays after 3" — the clock bound belongs to the day window.
                if after_min is not None:
                    w["start_minute"] = after_min
                    lbl += f" after {_fmt_minute(after_min)}"
                if before_min is not None:
                    w["end_minute"] = before_min
                    lbl += f" before {_fmt_minute(before_min)}"
            windows.append(w)
            labels.append(lbl)

    # Pure negation ("can't do mornings") → the complement window(s).
    if not windows and negated:
        if "morning" in negated:
            windows.append({"days": "any", "start_minute": _MORNING_END, "end_minute": None})
            labels.append("afternoons or later")
        if "afternoon" in negated:
            windows.append({"days": "any", "start_minute": None, "end_minute": _AFTERNOON_START})
            windows.append({"days": "any", "start_minute": _EVENING_START, "end_minute": None})
            labels.append("mornings or evenings")
        if "evening" in negated:
            windows.append({"days": "any", "start_minute": None, "end_minute": _EVENING_START})
            labels.append(f"before {_fmt_minute(_EVENING_START)}")
        if "weekend" in negated:
            windows.append({"days": "weekday", "start_minute": None, "end_minute": None})
            labels.append("weekdays")
        if "weekday" in negated:
            windows.append({"days": "weekend", "start_minute": None, "end_minute": None})
            labels.append("weekends")

    # "I'm only free after 5" — a bare clock bound with no period/day word.
    if not windows and (after_min is not None or before_min is not None):
        w = {"days": "any", "start_minute": after_min, "end_minute": before_min}
        bits = []
        if after_min is not None:
            bits.append(f"after {_fmt_minute(after_min)}")
        if before_min is not None:
            bits.append(f"before {_fmt_minute(before_min)}")
        windows.append(w)
        labels.append(" ".join(bits))

    return windows, " or ".join(labels)


# ─────────────────────────────── kid age bands ───────────────────────────────

_KID_CONTEXT_RE = re.compile(
    r"\b(?:my|our|kids?|sons?|daughters?|child|children|little\s+ones?)\b", re.I
)
_HAVE_KID_RE = re.compile(r"\b(?:my|our|(?:i|we)\s+have|(?:i|we)'?ve\s+got)\b", re.I)
_AGE_YEARS_RE = re.compile(r"\b(\d{1,2})\s*(?:-|–|\s)\s*(?:years?|yrs?)[-\s]*olds?\b|\b(\d{1,2})\s*yo\b", re.I)
_AGE_RANGE_RE = re.compile(r"\bages?\s+(\d{1,2})(?:\s*(?:-|–|to)\s*(\d{1,2}))?\b", re.I)
_AGE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("newborn", 0, 1),
    ("infant", 0, 1),
    ("baby", 0, 1),
    ("toddler", 1, 3),
    ("preschooler", 3, 5),
    ("preschool", 3, 5),
    ("kindergartner", 5, 6),
    ("tween", 9, 12),
    ("teenager", 13, 19),
    ("teen", 13, 19),
)


def _parse_kid_age(text: str) -> dict[str, Any] | None:
    low = str(text or "")
    m = _AGE_RANGE_RE.search(low)
    if m and _KID_CONTEXT_RE.search(low):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        if 0 <= lo <= 19 and lo <= hi <= 19:
            label = f"ages {lo}–{hi}" if hi != lo else f"around age {lo}"
            return {"min_years": lo, "max_years": hi, "label": label}
    m = _AGE_YEARS_RE.search(low)
    if m and _KID_CONTEXT_RE.search(low):
        age = int(m.group(1) or m.group(2))
        if 0 <= age <= 19:
            return {"min_years": age, "max_years": age, "label": f"around age {age}"}
    for band, lo, hi in _AGE_BANDS:
        if re.search(rf"\b{band}s?\b", low, re.I) and _HAVE_KID_RE.search(low):
            return {"min_years": lo, "max_years": hi, "label": f"{band} (ages {lo}–{hi})"}
    return None


# ─────────────────────────────── extraction ───────────────────────────────


def extract_constraints_from_message(message: str) -> dict[str, Any]:
    """Deterministic constraint extraction from ONE user line. {} when none stated."""
    text = str(message or "").strip()
    if not text:
        return {}
    out: dict[str, Any] = {}
    if _AVAILABILITY_MARKER_RE.search(text):
        windows, label = _parse_time_windows(text)
        if windows:
            out[TIME_KIND] = {"windows": windows, "label": label}
    kid = _parse_kid_age(text)
    if kid:
        out[KID_AGE_KIND] = kid
    return out


def merge_constraints(
    existing: dict[str, Any] | None, new: dict[str, Any] | None
) -> dict[str, Any]:
    """Per-kind replace: a restated availability supersedes the stored one."""
    merged = dict(existing or {})
    for kind, value in (new or {}).items():
        if isinstance(value, dict) and value:
            merged[kind] = value
    return merged


def constraint_ack_line(new_constraints: dict[str, Any] | None) -> str | None:
    """'Evenings after 6pm or weekends — noted.' for the constraints captured this turn."""
    parts: list[str] = []
    for kind in (TIME_KIND, KID_AGE_KIND):
        value = (new_constraints or {}).get(kind)
        label = str((value or {}).get("label") or "").strip() if isinstance(value, dict) else ""
        if label:
            parts.append(label)
    if not parts:
        return None
    line = " · ".join(parts)
    return f"{line[:1].upper()}{line[1:]} — noted."


# ─────────────────────────────── event filtering ───────────────────────────────


def _event_start(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fits_window(dt: datetime, window: dict[str, Any]) -> bool:
    days = str(window.get("days") or "any")
    is_weekend = dt.weekday() in (5, 6)
    if days == "weekend" and not is_weekend:
        return False
    if days == "weekday" and is_weekend:
        return False
    minute = dt.hour * 60 + dt.minute
    start = window.get("start_minute")
    end = window.get("end_minute")
    if start is not None and minute < int(start):
        return False
    if end is not None and minute >= int(end):
        return False
    return True


def event_fits_constraints(event: dict[str, Any], constraints: dict[str, Any] | None) -> bool:
    """True when the event lands inside ANY availability window (OR semantics).

    Fail-open: no constraints, no windows, or an unparseable start time all keep the
    event — the filter must never hide events because of missing data.
    """
    time_c = (constraints or {}).get(TIME_KIND)
    windows = (time_c or {}).get("windows") if isinstance(time_c, dict) else None
    if not windows:
        return True
    dt = _event_start(event.get("starts_at"))
    if dt is None:
        return True
    return any(_fits_window(dt, w) for w in windows if isinstance(w, dict))


def filter_events_by_constraints(
    events: list[dict[str, Any]], constraints: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], int]:
    """Apply the mom's availability windows to an event result set.

    Returns (kept, dropped_count) so callers can distinguish "nothing on the block"
    from "everything was outside her windows" and reply gracefully.
    """
    events = events or []
    kept = [e for e in events if not isinstance(e, dict) or event_fits_constraints(e, constraints)]
    return kept, len(events) - len(kept)


def constraints_all_filtered_note(
    constraints: dict[str, Any] | None, *, scope: str = "on your block"
) -> str:
    """Graceful reply when the availability filter removed every event."""
    time_c = (constraints or {}).get(TIME_KIND)
    label = str((time_c or {}).get("label") or "").strip() if isinstance(time_c, dict) else ""
    window_bit = label or "your availability"
    return (
        f"Heads up — everything coming up {scope} right now falls outside {window_bit}. "
        "Want me to listen and text you the moment something that fits pops up, "
        "or widen the search?"
    )


# ─────────────────────────────── multi-need asks ───────────────────────────────

_NEED_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\s+(?:also\s+)?|\bplus\b|\balso\b)\s*", re.I)
_HOST_NEED_RE = re.compile(r"\b(?:host|throw|organi[sz]e|plan)\b", re.I)
_MEET_NEED_RE = re.compile(
    r"\b(?:budd(?:y|ies)|playgroups?|play\s?dates?|meet\s?ups?"
    r"|(?:walking|running|workout|gym)\s+partners?"
    r"|someone\s+to\s+(?:walk|run|talk|hang)"
    r"|meet\s+(?:other\s+)?(?:moms?|mums?|people|neighbou?rs?|parents?))\b",
    re.I,
)
_ITEM_NEED_RE = re.compile(r"\b(?:need|looking\s+for|want)\b", re.I)
_NEED_VERB_STRIP_RE = re.compile(
    r"^\s*(?:i|we)?\s*(?:really|also|still)?\s*"
    r"(?:need|want\s+to|wanna|want|would\s+like\s+to|would\s+like|'?d\s+like\s+to"
    r"|am\s+looking\s+for|are\s+looking\s+for|looking\s+for)\s+",
    re.I,
)
_LEAD_ARTICLE_RE = re.compile(r"^(?:a|an)\s+", re.I)


def detect_multi_needs(message: str) -> list[dict[str, str]]:
    """Detect an enumeration of ≥2 distinct asks in one message.

    Kinds: need_item (swap/gear ask), find_meet (a person/meet to find), host (an
    event to host). Returns [] unless at least two clauses classify — a single ask is
    never a multi-need.
    """
    text = str(message or "").strip()
    if not text:
        return []
    needs: list[dict[str, str]] = []
    for clause in _NEED_CLAUSE_SPLIT_RE.split(text):
        clause = (clause or "").strip()
        if not clause:
            continue
        if _HOST_NEED_RE.search(clause):
            kind = "host"
        elif _MEET_NEED_RE.search(clause):
            kind = "find_meet"
        elif _ITEM_NEED_RE.search(clause):
            kind = "need_item"
        else:
            continue
        label = _NEED_VERB_STRIP_RE.sub("", clause).strip().rstrip(".!?") or clause
        needs.append({"kind": kind, "text": label[:120]})
    return needs if len(needs) >= 2 else []


def _held_phrase(need: dict[str, str]) -> str:
    text = str(need.get("text") or "").strip()
    if need.get("kind") == "host":
        text = _HOST_NEED_RE.sub("", text).strip() or text
    return _LEAD_ARTICLE_RE.sub("the ", text)


def multi_need_ack(needs: list[dict[str, str]]) -> str:
    """'Walk buddy first — I'm also holding the double stroller and the bbq.'"""
    primary = next((n for n in needs if n.get("kind") == "find_meet"), needs[0])
    others = [n for n in needs if n is not primary]
    prim = _LEAD_ARTICLE_RE.sub("", str(primary.get("text") or "").strip()) or "That"
    prim = prim[:1].upper() + prim[1:]
    held = [_held_phrase(n) for n in others]
    if len(held) > 1:
        held_txt = ", ".join(held[:-1]) + f" and {held[-1]}"
    else:
        held_txt = held[0] if held else ""
    return f"{prim} first — I'm also holding {held_txt}."


def stamp_multi_needs_ctx(session_ctx: dict[str, Any], needs: list[dict[str, str]]) -> None:
    """Park the extras in session state so no need silently vanishes.

    The first find_meet need (else the first need) stays 'active' — that's the one the
    normal routing will handle this turn; the rest are 'parked' for a later resume.
    """
    if not needs:
        return
    primary = next((n for n in needs if n.get("kind") == "find_meet"), needs[0])
    stack = [
        {**n, "status": "active" if n is primary else "parked"}
        for n in needs
    ]
    session_ctx[MULTI_NEED_STACK_KEY] = stack
    session_ctx[PARKED_NEEDS_KEY] = [n for n in stack if n["status"] == "parked"]


# ─────────────────────────────── durable store ───────────────────────────────


def persist_user_constraints(
    user_id: str,
    new_constraints: dict[str, Any],
    *,
    source_quote: str | None = None,
) -> None:
    """Upsert the captured constraints for a verified user (one active row per kind).

    Best-effort: a store failure must never break the turn that captured it.
    """
    if not user_id or not new_constraints:
        return
    try:
        from app.auth import service_client

        sb = service_client()
        now = datetime.now(timezone.utc).isoformat()
        for kind, value in new_constraints.items():
            if kind not in (TIME_KIND, KID_AGE_KIND) or not isinstance(value, dict):
                continue
            sb.table("user_constraints").upsert(
                {
                    "user_id": user_id,
                    "kind": kind,
                    "value": value,
                    "label": str(value.get("label") or "")[:200],
                    "source_quote": (str(source_quote).strip()[:500] or None)
                    if source_quote
                    else None,
                    "updated_at": now,
                },
                on_conflict="user_id,kind",
            ).execute()
    except Exception:  # noqa: BLE001
        logger.exception("persist_user_constraints_failed user_id=%s", user_id)


def load_user_constraints(user_id: str) -> dict[str, Any]:
    """Read the durable constraints for a user → {kind: value}. {} on any failure."""
    if not user_id:
        return {}
    try:
        from app.auth import service_client

        res = (
            service_client()
            .table("user_constraints")
            .select("kind, value")
            .eq("user_id", user_id)
            .execute()
        )
        return {
            str(r.get("kind")): r.get("value")
            for r in (res.data or [])
            if isinstance(r, dict) and isinstance(r.get("value"), dict)
        }
    except Exception:  # noqa: BLE001
        logger.exception("load_user_constraints_failed user_id=%s", user_id)
        return {}


# ─────────────────────────────── turn hooks ───────────────────────────────


def capture_constraints_for_turn(
    session_ctx: dict[str, Any],
    message: str,
    *,
    user_id: str | None = None,
    persist_durably: bool = False,
) -> str | None:
    """Run on EVERY lana turn before routing (see main._run_lana_message).

    Seeds the session store from the durable table once per session (verified users),
    captures any constraint/multi-need stated this turn into session context, persists
    it durably when allowed, and stamps the acknowledgment line for the reply.
    Returns the ack line, or None when nothing was captured.
    """
    # Cross-session recall: a verified mom shouldn't have to restate "evenings only".
    if (
        persist_durably
        and user_id
        and not session_ctx.get(CONSTRAINT_CTX_KEY)
        and not session_ctx.get("_constraints_loaded")
    ):
        stored = load_user_constraints(user_id)
        if stored:
            session_ctx[CONSTRAINT_CTX_KEY] = stored
        session_ctx["_constraints_loaded"] = True

    ack_parts: list[str] = []
    new = extract_constraints_from_message(message)
    if new:
        session_ctx[CONSTRAINT_CTX_KEY] = merge_constraints(
            session_ctx.get(CONSTRAINT_CTX_KEY), new
        )
        line = constraint_ack_line(new)
        if line:
            ack_parts.append(line)
        if persist_durably and user_id:
            persist_user_constraints(user_id, new, source_quote=message)

    needs = detect_multi_needs(message)
    if needs:
        stamp_multi_needs_ctx(session_ctx, needs)
        ack_parts.append(multi_need_ack(needs))

    if not ack_parts:
        return None
    ack = " ".join(ack_parts)
    session_ctx[ACK_CTX_KEY] = ack
    return ack


def finalize_constraint_turn(
    reply: str,
    session_ctx_in: dict[str, Any],
    turn_ctx: dict[str, Any],
) -> str:
    """After the turn is handled: carry constraint memory into the RETURNED context
    (whichever handler built it — some build a fresh dict) and prepend this turn's
    capture acknowledgment to the reply. The ack key is popped so it never persists.
    """
    for key in (CONSTRAINT_CTX_KEY, MULTI_NEED_STACK_KEY, PARKED_NEEDS_KEY, "_constraints_loaded"):
        if key in session_ctx_in and key not in turn_ctx:
            turn_ctx[key] = session_ctx_in[key]
    ack = turn_ctx.pop(ACK_CTX_KEY, None) or session_ctx_in.get(ACK_CTX_KEY)
    ack = str(ack or "").strip()
    if not ack or not str(reply or "").strip():
        return reply
    if ack.lower() in str(reply).lower():
        return reply  # the handler already acknowledged it verbatim
    return f"{ack}\n\n{reply}"
