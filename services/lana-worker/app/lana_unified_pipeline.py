"""Unified Lana: discovery gates first (code), orchestrator for everything else (AI)."""

from __future__ import annotations

from typing import Any

from app.discovery_route import handle_discovery_turn
from app.lana_dispatch import lana_unified_turn
from app.lana_ui import sanitize_assistant_message
from app.lana_paths import unified_rules_first_enabled
from app.loop_guard import discovery_reply_is_stuck, reset_sticky_discovery_state
from app.orchestrator.pipeline import run_turn
from app.turn_surfaces import clear_turn_surfaces
from app.turn_timing import TurnTimer


_EVENT_DRAFT_FIELDS = {
    "title",
    "description",
    "venue_name",
    "venue_address",
    "place_id",
    "venue_lat",
    "venue_lng",
    "starts_at",
    "ends_at",
    "duration_minutes",
    "max_attendees",
    "cohort_tags",
    "affinity_prompt",
    "affinity_options",
    "missing",
}


def _event_draft_complete(draft: Any) -> bool:
    """A host draft is publishable once it has a title, a place, and a start time."""
    if not isinstance(draft, dict):
        return False
    return all(str(draft.get(k) or "").strip() for k in ("title", "venue_name", "starts_at"))


def _auto_publish_event(user_id: str, user_jwt: str, draft: dict[str, Any]) -> str | None:
    """Create the event via create_event. Returns event_id, or None on any failure
    (never raises — a publish problem must not break the chat turn)."""
    try:
        from app.event_publish import publish_event
        from app.models import EventDraft

        clean = {k: v for k, v in draft.items() if k in _EVENT_DRAFT_FIELDS}
        return publish_event(user_id, user_jwt, EventDraft(**clean)) or None
    except Exception:  # noqa: BLE001 - publish is best-effort; chat continues regardless
        import logging

        logging.getLogger(__name__).exception("auto_publish_event_failed")
        return None


_TITLE_SUGGESTIONS = ["Playdate at the park", "Weekend playgroup", "Morning meetup"]
# A "what time to start?" question (the day is already chosen) → a concrete clock
# spread, so one tap resolves the time instead of re-offering days.
_START_TIME_SUGGESTIONS = ["9 AM", "12 PM", "3 PM", "6 PM"]


def _when_suggestions() -> list[str]:
    """Concrete upcoming weekend dates ("Sat Jun 20", "Sun Jun 21", + next weekend)
    so a "when?" answer lands on a REAL date instead of a vague "this weekend" the
    extractor can't pin to a calendar day. Computed from the worker's live clock."""
    from datetime import datetime, timedelta

    today = datetime.now().date()
    sat = today + timedelta(days=(5 - today.weekday()) % 7)  # this week's Sat (today if Sat)
    sun = today + timedelta(days=(6 - today.weekday()) % 7)

    def fmt(d) -> str:
        return f"{d.strftime('%a %b')} {d.day}"

    return [fmt(sat), fmt(sun), fmt(sat + timedelta(days=7)), fmt(sun + timedelta(days=7))]
_AFTERNOON_SUGGESTIONS = ["12 PM", "1 PM", "2 PM", "3 PM"]
_MORNING_SUGGESTIONS = ["8 AM", "9 AM", "10 AM", "11 AM"]
_EVENING_SUGGESTIONS = ["5 PM", "6 PM", "7 PM"]
_PLACE_SUGGESTIONS = ["The playground", "The park", "My place", "Somewhere on the block"]
# Sentinel suggestion — the FE swaps this chip for a Google place-search field.
_SEARCH_PLACE_OPTION = "🔍 Search a place"


def _nearby_host_places(
    zip_code: str | None, block_id: str | None, user_id: str | None
) -> list[str]:
    """Real nearby venues to host at (Google Places around the block). [] with no key
    or no results, so the where-step falls back to the generic chips."""
    try:
        from app.places import nearby_place_suggestions

        return nearby_place_suggestions(
            query="park playground community center",
            zip_code=zip_code,
            block_id=block_id,
            user_id=user_id,
            limit=3,
        )
    except Exception:  # noqa: BLE001 - best-effort
        return []

# Bare words the extractor mistakes for a title (from "host a meet" etc.) — not real names.
_GENERIC_TITLES = {
    "meet", "meetup", "meet up", "a meet", "the meet", "event", "an event", "the event",
    "meeting", "gathering", "a gathering", "get together", "get-together",
    "a get together", "get-together meet", "hangout", "a hangout", "party", "a party",
}


def _is_generic_title(title: Any) -> bool:
    return str(title or "").strip().lower().strip(".!?") in _GENERIC_TITLES


_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _resolve_event_date(text: str) -> str | None:
    """Deterministically resolve a date phrase → ISO 'YYYY-MM-DD' for the NEXT occurrence
    (correct year), since the LLM extractor mis-guesses the year. None if no date found."""
    import re
    from datetime import datetime, timedelta

    t = str(text or "").lower()
    today = datetime.now().date()

    # "jun 20" / "june 20" / "20 jun"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", t)
    m = m or re.search(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", t)
    if m:
        g1, g2 = m.group(1), m.group(2)
        mon = _MONTHS.get(g1) or _MONTHS.get(g2)
        day = int(g2 if g1 in _MONTHS else g1)
        for yr in (today.year, today.year + 1):
            try:
                d = datetime(yr, mon, day).date()
            except ValueError:
                return None
            if d >= today:
                return d.isoformat()
        return None
    if "tomorrow" in t:
        return (today + timedelta(days=1)).isoformat()
    if "today" in t or "tonight" in t:
        return today.isoformat()
    if "this weekend" in t or "weekend" in t:
        return (today + timedelta(days=(5 - today.weekday()) % 7)).isoformat()
    for name, wd in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            return (today + timedelta(days=(wd - today.weekday()) % 7)).isoformat()
    return None


def _resolve_event_time(text: str) -> str | None:
    """Deterministically resolve a time phrase → 'HH:MM' (24h). None if none found."""
    import re

    t = str(text or "").lower()
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3) == "pm":
            h += 12
        return f"{h:02d}:{int(m.group(2) or 0):02d}"
    # Order matters: "afternoon" contains "noon", so check the periods first.
    if "afternoon" in t:
        return "14:00"
    if "morning" in t:
        return "10:00"
    if "evening" in t or "night" in t:
        return "18:00"
    if "noon" in t:
        return "12:00"
    return None


def _inject_event_quick_replies(
    draft: dict[str, Any], question: str = "", *, title_suggestions: list[str] | None = None
) -> None:
    """Tappable options for the current question. Reads the question Lana actually
    asked (so 'what time in the afternoon?' → afternoon clock times), falling back to
    the next missing blocker (title → when → place). Always present so chips show.
    `title_suggestions` (AI, event-aware) replaces the generic title list when given."""
    import re

    if draft.get("affinity_prompt"):
        return  # a deliberate affinity gate owns the chips this turn

    titles = title_suggestions or _TITLE_SUGGESTIONS

    # Lana often acknowledges the prior answer ("Friday evening sounds lovely!")
    # before asking the next thing ("Where would you host?"). Match on the actual
    # question clause — the last sentence with a "?" — so the acknowledgment's words
    # don't hijack the chips.
    parts = re.split(r"(?<=[.!?])\s+", str(question or "").strip())
    q = (next((p for p in reversed(parts) if "?" in p), parts[-1] if parts else "")).lower()

    def has(k: str) -> bool:
        return bool(str(draft.get(k) or "").strip())

    # Question-aware FIRST: match the options to what Lana actually just asked,
    # so the chips track the question even when the draft fields lag behind.
    # (Specific period → clock times; generic when → day+time combos.)
    if re.search(r"\bafternoon\b", q):
        draft["suggestions"] = _AFTERNOON_SUGGESTIONS
    elif re.search(r"\bmorning\b", q):
        draft["suggestions"] = _MORNING_SUGGESTIONS
    elif re.search(r"\bevening\b|\bnight\b", q):
        draft["suggestions"] = _EVENING_SUGGESTIONS
    elif re.search(r"what time|which time|time of day|\bstart\b", q):
        # The day is set; this asks the clock time → offer concrete times.
        draft["suggestions"] = _START_TIME_SUGGESTIONS
    elif re.search(
        r"\btime\b|\bwhen\b|\bday\b|which day|what day|\bdate\b|weekend|"
        r"saturday|sunday|monday|tuesday|wednesday|thursday|friday",
        q,
    ):
        # "When?" or a "which day — Sat or Sun?" follow-up → concrete dated days.
        draft["suggestions"] = _when_suggestions()
    elif re.search(r"\bwhere\b|\bplace\b|\blocation\b|\bvenue\b", q):
        draft["suggestions"] = _PLACE_SUGGESTIONS
    elif re.search(r"\bcall\b|\bname\b|\btitle\b|what kind|about", q):
        draft["suggestions"] = titles
    # Fallback: next missing blocker (title → when → place).
    elif not has("title"):
        draft["suggestions"] = titles
    elif not has("starts_at"):
        draft["suggestions"] = _when_suggestions()
    elif not has("venue_name"):
        draft["suggestions"] = _PLACE_SUGGESTIONS


def _event_published_reply(reply: str, draft: dict[str, Any]) -> str:
    title = str((draft or {}).get("title") or "your event").strip() or "your event"
    note = f"🎉 Done — **{title}** is live on your block. Neighbors who match can RSVP now."
    base = str(reply or "").strip()
    # The orchestrator wrote `base` without knowing we'd publish this turn. If it's
    # still asking for a detail ("…where will the jog start?"), keeping it contradicts
    # the publish — so drop any question and lead only with a clean acknowledgment.
    if base and "?" not in base:
        return f"{base}\n\n{note}"
    return note


def run_lana_unified_pipeline(
    *,
    user_id: str,
    session_id: str,
    history: list[dict[str, Any]],
    user_message: str,
    session_ctx: dict[str, Any],
    user_jwt: str,
    phone_verified: bool,
    home_block_id: str | None,
    is_anonymous: bool,
    persisted_core: dict[str, Any] | None = None,
    timer: TurnTimer | None = None,
    use_orchestrator: bool = True,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """
    1. Discovery/auth gates (code) — ZIP, identity, preview, verify; orchestrator off.
    2. Orchestrator (AI) — companionship, hosting; enforce still applies discovery overrides.
    """
    timer = timer or TurnTimer()
    session_ctx = {
        **session_ctx,
        "phone_verified": phone_verified,
        "unified_mode": True,
    }

    # Wipe per-turn surfaces (…_listed_now, …_published_now, saved cards) up front, so
    # a one-shot card from a prior turn never leaks into this one — the early host /
    # pass-along / tip gates return before the discovery-path clear would run.
    clear_turn_surfaces(session_ctx)

    # Sticky "pass along an item" capture owns the whole turn (deterministic flow +
    # structured extraction), the same way event_host_active does. It releases on
    # save, cancel, or a turn cap, so other flows are never affected.
    if session_ctx.get("pass_along_active"):
        from app.pass_along import run_pass_along_turn

        reply = sanitize_assistant_message(
            run_pass_along_turn(
                user_message=user_message,
                session_ctx=session_ctx,
                history=history,
                user_jwt=user_jwt,
                home_block_id=home_block_id,
            )
        )
        session_ctx["_orchestrator_turn"] = False
        session_ctx["timing_ms"] = timer.to_dict()
        session_ctx["last_routing"] = {
            "outcome": "pass_along",
            "intent_class": "swap",
            "tool_called": "save_local_signal" if session_ctx.get("item_listed_now") else None,
        }
        ui = {"bucket": None, "focus_phrase": None, "highlights": []}
        return reply, "continue", session_ctx, ui, session_ctx.get("event_draft")

    # Sticky "share a tip" capture — same self-contained pattern as pass-along.
    if session_ctx.get("tip_share_active"):
        from app.tip_share import run_tip_share_turn

        reply = sanitize_assistant_message(
            run_tip_share_turn(
                user_message=user_message,
                session_ctx=session_ctx,
                history=history,
                user_jwt=user_jwt,
                home_block_id=home_block_id,
            )
        )
        session_ctx["_orchestrator_turn"] = False
        session_ctx["timing_ms"] = timer.to_dict()
        session_ctx["last_routing"] = {
            "outcome": "tip_share",
            "intent_class": "discovery",
            "tool_called": "save_local_signal" if session_ctx.get("tip_listed_now") else None,
        }
        ui = {"bucket": None, "focus_phrase": None, "highlights": []}
        return reply, "continue", session_ctx, ui, session_ctx.get("event_draft")

    # Sticky "looking for a meet/playgroup" capture — same self-contained pattern.
    if session_ctx.get("look_meet_active"):
        from app.look_meet import run_look_meet_turn

        reply = sanitize_assistant_message(
            run_look_meet_turn(
                user_message=user_message,
                session_ctx=session_ctx,
                history=history,
                user_jwt=user_jwt,
                home_block_id=home_block_id,
            )
        )
        session_ctx["_orchestrator_turn"] = False
        session_ctx["timing_ms"] = timer.to_dict()
        session_ctx["last_routing"] = {
            "outcome": "look_meet",
            "intent_class": "discovery",
            "tool_called": "save_local_signal" if session_ctx.get("look_meet_saved_now") else None,
        }
        ui = {"bucket": None, "focus_phrase": None, "highlights": []}
        return reply, "continue", session_ctx, ui, session_ctx.get("event_draft")

    if unified_rules_first_enabled():
        discovery = handle_discovery_turn(
            user_message,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            phone_verified=phone_verified,
            home_block_id=home_block_id,
            is_anonymous=is_anonymous,
            history=history,
            user_id=user_id,
            timer=timer,
        )
        if discovery is not None:
            reply, ctx, routing, peers = discovery
            reply = sanitize_assistant_message(reply)
            # Loop breaker: if the rule layer is about to repeat the same reply for
            # the Nth turn, hand off to the orchestrator (LLM) instead of looping.
            if use_orchestrator and discovery_reply_is_stuck(history, reply, ctx):
                reset_sticky_discovery_state(session_ctx)
            else:
                ctx["last_routing"] = routing
                ctx["_orchestrator_turn"] = False
                ctx["timing_ms"] = timer.to_dict()
                # Reflect host-mode state the gate may have just cleared (cancel/pivot/cap)
                # so the persisted context overrides any stale active flag.
                ctx["event_host_active"] = bool(session_ctx.get("event_host_active"))
                ctx["event_host_turns"] = int(session_ctx.get("event_host_turns") or 0)
                if ctx.get("activity_previews"):
                    ctx["peer_matches"] = []
                elif peers:
                    ctx["peer_matches"] = peers
                    ctx.pop("activity_previews", None)
                elif "peer_matches" not in ctx:
                    ctx["peer_matches"] = []
                ui = {
                    "bucket": None,
                    "focus_phrase": None,
                    "highlights": [],
                }
                status = "ready_to_complete" if ctx.get("ready_to_complete") else "continue"
                return reply, status, ctx, ui, ctx.get("event_draft")

        clear_turn_surfaces(session_ctx)
    if use_orchestrator:
        prev_event_id = session_ctx.get("event_id")
        reply, status, turn_ctx, ui, draft = run_turn(
            user_id=user_id,
            session_id=session_id,
            purpose="lana",
            history=history,
            user_message=user_message,
            session_ctx=session_ctx,
            user_jwt=user_jwt,
            persisted_core=persisted_core,
            timer=timer,
        )
        turn_ctx["_orchestrator_turn"] = True
        # Keep sticky host mode across turns; clear it the moment the event publishes
        # (new event_id) so we don't re-enter the flow next turn.
        host_active = bool(session_ctx.get("event_host_active"))
        # Self-engage: if the orchestrator is actively shaping an event_draft, pin host
        # mode even when entry classification was fuzzy or the CTA hint didn't arrive.
        draft_in_progress = isinstance(draft, dict) and any(
            draft.get(k) for k in ("title", "venue_name", "starts_at", "affinity_prompt")
        )
        host_active = host_active or draft_in_progress

        # The orchestrator keeps the event draft in ctx — the 5th return value is None
        # for the `lana` purpose. Source it from ctx so we can attach chips + publish.
        event_draft = draft if isinstance(draft, dict) else turn_ctx.get("event_draft")
        if not isinstance(event_draft, dict) and isinstance(session_ctx.get("event_draft"), dict):
            event_draft = session_ctx.get("event_draft")

        # Drive the in-chat host flow deterministically: tappable suggestions for the
        # current question, one affinity question before finalizing, then auto-publish.
        # (The orchestrator's free-form questions don't reliably emit options, so we
        # inject them here and the FE renders chips.)
        already_published = bool(turn_ctx.get("event_id") or prev_event_id)
        if host_active and not already_published:
            ed: dict[str, Any] = dict(event_draft) if isinstance(event_draft, dict) else {}
            # Chips + affinity are transient per-turn UI — never inherit last turn's
            # set, or the early-out keeps showing stale options for the new question.
            ed.pop("suggestions", None)
            ed.pop("affinity_prompt", None)
            ed.pop("affinity_options", None)
            # The extractor over-eagerly pulls a "title" from the entry phrase ("host a
            # meet" → "Meet"). Drop bare generic words so we still ask for a real name.
            if _is_generic_title(ed.get("title")):
                ed.pop("title", None)

            # Deterministic when-resolution — the LLM extractor mis-guesses the year
            # ("Jun 20" → 2023) and drops the time-of-day. Parse the user's own words /
            # tapped chips into a real date + clock time, then build starts_at ourselves.
            # Persist on turn_ctx (the RETURNED context) — session_ctx mutations here are
            # dropped, which previously lost the date and made it re-ask "when?".
            wd = turn_ctx.get("event_when_date")
            wt = turn_ctx.get("event_when_time")
            nd = _resolve_event_date(user_message)
            if nd:
                wd = nd
            ntime = _resolve_event_time(user_message)
            if ntime:
                wt = ntime
            turn_ctx["event_when_date"] = wd
            turn_ctx["event_when_time"] = wt
            if wd:
                ed["starts_at"] = f"{wd}T{wt or '00:00'}:00"

            # Exact picked place (stamped via /event-venue) — overrides any venue the
            # extractor lifted, and carries the precise pin through to publish.
            ev = session_ctx.get("event_venue")
            if isinstance(ev, dict) and str(ev.get("name") or "").strip():
                ed["venue_name"] = ev["name"]
                ed["venue_address"] = ev.get("address")
                ed["place_id"] = ev.get("place_id")
                ed["venue_lat"] = ev.get("lat")
                ed["venue_lng"] = ev.get("lng")

            _title = str(ed.get("title") or "").strip()
            has_venue = bool(str(ed.get("venue_name") or "").strip())
            # Gate on whether we've ASKED where — not just on a venue being present, since
            # the extractor lifts one from the title ("Playdate at the Park" → "Park") and
            # that used to skip the where-step entirely.
            place_asked = bool(turn_ctx.get("event_place_asked"))
            complete = bool(_title) and bool(wd) and bool(wt) and place_asked and has_venue
            # Ask the affinity question once, gated ONLY on whether we've asked — not on
            # cohort_tags (the extractor auto-fills those, which used to skip the question).
            affinity_done = bool(session_ctx.get("event_affinity_asked"))

            # Event-aware AI suggestions (tailored titles + "who's it for?") — only on the
            # turns that need them (naming the event, or the one affinity gate).
            sugg: dict[str, Any] = {}
            if not _title or (complete and not affinity_done):
                from app.event_suggest import event_suggestions

                sugg = event_suggestions(history=history, user_message=user_message, draft=ed)

            if complete and not affinity_done:
                aff = sugg.get("affinity") if isinstance(sugg.get("affinity"), dict) else {}
                ed["affinity_prompt"] = aff.get("question") or "Who’s it for?"
                ed["affinity_options"] = aff.get("options") or [
                    "Same kid-stage",
                    "Any toddler mom",
                    "Open · all moms",
                ]
                ed["suggestions"] = []
                turn_ctx["event_affinity_asked"] = True
                reply = f"Perfect — **{_title or 'your meetup'}** is all set! One last thing before I post it:"
            elif complete:
                event_id = _auto_publish_event(user_id, user_jwt, ed)
                if event_id:
                    turn_ctx["event_id"] = event_id
                    turn_ctx["event_published_now"] = True
                    turn_ctx["event_affinity_asked"] = False
                    turn_ctx["event_when_date"] = None
                    turn_ctx["event_when_time"] = None
                    turn_ctx["event_place_asked"] = False
                    turn_ctx["event_venue"] = None
                    reply = _event_published_reply(reply, ed)
            else:
                # Deterministic question + chips for the next missing field (title → day →
                # time → place), so the bubble ALWAYS matches the chips.
                if not _title:
                    reply = "Love it! What would you like to call it?"
                    ed["suggestions"] = sugg.get("title_suggestions") or _TITLE_SUGGESTIONS
                elif not wd:
                    reply = f"Got it — **{_title}**. When works for you?"
                    ed["suggestions"] = _when_suggestions()
                elif not wt:
                    reply = f"Great — **{_title}**. What time should it start?"
                    ed["suggestions"] = _START_TIME_SUGGESTIONS
                else:
                    # Where-step — asked once. Drop any venue the extractor lifted from
                    # the title so the user answers explicitly (fixes the skipped "where?").
                    if not place_asked:
                        ed.pop("venue_name", None)
                        turn_ctx["event_place_asked"] = True
                        reply = f"Almost there — **{_title}**. Where in the block would you like to host it?"
                    else:
                        reply = f"Where would you like to host **{_title}**?"
                    nearby = _nearby_host_places(
                        str(session_ctx.get("zip_code") or "").strip() or None,
                        home_block_id,
                        user_id,
                    )
                    base = (nearby + ["My place"]) if nearby else list(_PLACE_SUGGESTIONS)
                    ed["suggestions"] = base + [_SEARCH_PLACE_OPTION]
            turn_ctx["event_draft"] = ed
            draft = ed  # surface to the FE (5th return → response.event_draft)

        published = bool(turn_ctx.get("event_id")) and turn_ctx.get("event_id") != prev_event_id
        turn_ctx["event_host_active"] = host_active and not published
        turn_ctx["event_host_turns"] = (
            0 if (published or not host_active) else int(session_ctx.get("event_host_turns") or 0)
        )
        return reply, status, turn_ctx, ui, draft

    reply, status, turn_ctx, ui_raw, draft_raw = lana_unified_turn(
        history=history,
        user_message=user_message,
        session_ctx=session_ctx,
        user_jwt=user_jwt,
        phone_verified=phone_verified,
        home_block_id=home_block_id,
        is_anonymous=is_anonymous,
    )
    turn_ctx["_orchestrator_turn"] = False
    peers = turn_ctx.pop("peer_matches", None)
    if peers:
        turn_ctx["peer_matches"] = peers
    return reply, status, turn_ctx, ui_raw, draft_raw
