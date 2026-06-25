"""Unified Lana: discovery gates first (code), orchestrator for everything else (AI)."""

from __future__ import annotations

from typing import Any

from app.discovery_route import handle_discovery_turn, looks_like_logout
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
    "auto_approve",
    "allow_attendee_share",
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


def _auto_publish_event(
    user_id: str, user_jwt: str, draft: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Create the event via create_event. Returns (event_id, None) on success or
    (None, error_detail) on failure (never raises — a publish problem must not break
    the chat turn, but the caller surfaces the reason instead of faking success)."""
    import logging

    from fastapi import HTTPException

    try:
        from app.event_publish import publish_event
        from app.models import EventDraft

        clean = {k: v for k, v in draft.items() if k in _EVENT_DRAFT_FIELDS}
        event_id = publish_event(user_id, user_jwt, EventDraft(**clean)) or None
        return event_id, None
    except HTTPException as exc:
        logging.getLogger(__name__).warning("auto_publish_event_failed: %s", exc.detail)
        return None, str(exc.detail)
    except Exception as exc:  # noqa: BLE001 - publish is best-effort; chat continues
        logging.getLogger(__name__).exception("auto_publish_event_failed")
        return None, str(exc) or "unknown_error"


def _publish_failure_reply(error: str | None, title: str) -> str:
    """Honest message when create_event is rejected — never fake a successful post."""
    name = f"**{title}**" if title else "your event"
    detail = (error or "").lower()
    if "phone_not_verified" in detail or ":403" in detail or "not_authenticated" in detail:
        return (
            f"{name} is all set, but I can't post it until your account is verified. "
            "Verify your email and I'll publish it right away."
        )
    if "location" in detail or "venue" in detail:
        return (
            f"I have everything for {name} except a spot I can place on the map. "
            "Pick a place or share an address and I'll post it."
        )
    return (
        f"I hit a snag posting {name} just now — give it another try in a moment "
        "and I'll get it up."
    )


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

# Host join-settings chip options (capacity / approval / share).
_CAPACITY_SUGGESTIONS = ["Up to 6", "Up to 10", "Up to 15", "Open · no limit"]
_APPROVAL_SUGGESTIONS = ["Anyone can join", "I'll approve each request"]
_SHARE_SUGGESTIONS = ["Yes, let them share", "My invites only"]


def _parse_event_settings(message: str, settings: dict[str, Any]) -> None:
    """Read capacity / approval / share signals from the user's tap (or words) into the
    settings dict. Matches the chip labels; harmless on unrelated messages."""
    import re

    m = str(message or "").lower()
    # capacity
    if re.search(r"no limit|unlimited|\bopen\b|any number|as many", m):
        settings["max_attendees"] = None  # unlimited
        settings["_cap_set"] = True
    else:
        num = re.search(r"\b(\d{1,3})\b", m)
        if num:
            settings["max_attendees"] = int(num.group(1))
            settings["_cap_set"] = True
    # approval (require approval = NOT auto_approve)
    if re.search(r"\bapprove\b|approval|i'?ll approve|vet|screen", m):
        settings["auto_approve"] = False
    elif re.search(r"anyone can join|open join|anyone|no approval|just join", m):
        settings["auto_approve"] = True
    # attendee share
    if re.search(r"invites only|my invites|don'?t share|keep it private|private|no share", m):
        settings["allow_attendee_share"] = False
    elif re.search(r"let them share|can share|share it|invite others|yes.*share|share.*yes", m):
        settings["allow_attendee_share"] = True


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

    # A logout request must escape any sticky capture mode — otherwise "log me out" gets
    # swallowed as an item/tip/meet answer and does nothing. Clear the flags so the turn
    # falls through to discovery's logout handler.
    if looks_like_logout(user_message):
        for _k in (
            "pass_along_active", "tip_share_active", "look_meet_active",
            "activity_browse_active", "event_host_active"
        ):
            session_ctx[_k] = False
        # Drop any half-built capture draft + step flags so the stale card and its chips
        # don't keep rendering after the user has left the flow via logout.
        for _k in (
            "event_draft", "item_draft", "tip_draft", "look_draft", "browse_draft",
            "event_when_date", "event_when_time", "event_place_asked", "event_venue",
            "event_settings", "event_cap_asked", "event_approval_asked",
            "event_share_asked", "event_affinity_asked",
        ):
            session_ctx[_k] = None

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

    # Sticky "looking for a meet/playgroup" capture — same self-contained pattern, but it
    # must not trap the user: if they pivot away (a new request once the card is ready, or
    # an explicit switch to another intent), drop the unsaved capture and fall through to
    # normal routing so the new request is handled fresh.
    if session_ctx.get("look_meet_active"):
        from app.discovery_slots import discovery_slots_for_turn
        from app.look_meet import (
            look_meet_should_release,
            reset_look_meet_state,
            run_look_meet_turn,
        )

        # Classify the turn so the capture releases on a semantic abandon or a pivot to
        # another intent — the AI's read, not just hard-coded cancel words (mirrors how
        # event-host releases). Cached by message, so handle_discovery_turn reuses it
        # after a release without a second model call.
        pivot_slots = discovery_slots_for_turn(
            session_ctx,
            user_message,
            routing_phase=str(session_ctx.get("routing_phase") or "listening"),
            history=history,
            has_block=bool(home_block_id or session_ctx.get("preview_block_id")),
            has_identity=bool(session_ctx.get("identity_snippet")),
            phone_verified=phone_verified,
            timer=timer,
        )
        if look_meet_should_release(user_message, session_ctx, pivot_slots):
            reset_look_meet_state(session_ctx)
        else:
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

    # Sticky agentic "what's happening" browse — ask interest, show the block's real events,
    # re-filter on follow-ups ("show me cricket ones"). A different ACTIVITY stays in-flow as
    # a refine; only a pivot to another intent (find people / a meet / RSVP) or abandon releases.
    if session_ctx.get("activity_browse_active"):
        from app.activity_browse import (
            activity_browse_should_release,
            reset_activity_browse_state,
            run_activity_browse_turn,
        )
        from app.discovery_slots import discovery_slots_for_turn

        browse_slots = discovery_slots_for_turn(
            session_ctx,
            user_message,
            routing_phase=str(session_ctx.get("routing_phase") or "listening"),
            history=history,
            has_block=bool(home_block_id or session_ctx.get("preview_block_id")),
            has_identity=bool(session_ctx.get("identity_snippet")),
            phone_verified=phone_verified,
            timer=timer,
        )
        if activity_browse_should_release(user_message, session_ctx, browse_slots):
            reset_activity_browse_state(session_ctx)
        else:
            reply = sanitize_assistant_message(
                run_activity_browse_turn(
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
                "outcome": "activity_browse",
                "intent_class": "discovery",
                "tool_called": None,
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
            # This turn renders the structured host card — not a discovery / "thinking"
            # turn. Drop the orchestrator's leaked eyebrow ("GENERAL · reflecting on …")
            # and any stale peer-preview cards so neither bleeds in beside the host prompt.
            ui = {"bucket": None, "focus_phrase": None, "highlights": []}
            turn_ctx["peer_matches"] = []
            # Chips + affinity are transient per-turn UI — never inherit last turn's
            # set, or the early-out keeps showing stale options for the new question.
            ed.pop("suggestions", None)
            ed.pop("affinity_prompt", None)
            ed.pop("affinity_options", None)
            # The extractor over-eagerly pulls a "title" from the entry phrase ("host a
            # meet" → "Meet"). Drop bare generic words so we still ask for a real name.
            if _is_generic_title(ed.get("title")):
                ed.pop("title", None)

            # Host asked to redo a slot they'd already filled ("don't call it X", "change
            # the time"). The merge already blanked the field on the draft; here we also
            # reset the matching step flags so the deterministic flow re-asks that slot
            # instead of marching on to the next one (the "stuck on where?" loop).
            cleared = session_ctx.get("event_cleared_fields") or []
            if cleared:
                if "starts_at" in cleared:
                    # Drop the resolved date/time so the when/time steps re-engage.
                    turn_ctx["event_when_date"] = None
                    turn_ctx["event_when_time"] = None
                    ed.pop("starts_at", None)
                    ed.pop("ends_at", None)
                if "venue_name" in cleared:
                    # Re-open the where-step and forget the picked pin so the venue isn't
                    # silently re-stamped from event_venue below.
                    turn_ctx["event_place_asked"] = False
                    turn_ctx["event_venue"] = None
                    session_ctx["event_venue"] = None
                    ed.pop("venue_name", None)
                if "max_attendees" in cleared:
                    turn_ctx["event_cap_asked"] = False
                    settings_box = dict(session_ctx.get("event_settings") or {})
                    settings_box.pop("max_attendees", None)
                    settings_box.pop("_cap_set", None)
                    session_ctx["event_settings"] = settings_box
                    ed.pop("max_attendees", None)
                # title clears itself — an empty title re-enters the "what to call it?" step.
                turn_ctx["event_cleared_fields"] = None

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
                # Rebuild ends_at off our corrected start — the LLM extractor mis-years
                # ends_at (e.g. 2023) while we compute the real future date here, which
                # used to ship a wrong-year ends_at through to publish.
                from datetime import datetime as _dt, timedelta as _td

                try:
                    _start = _dt.fromisoformat(ed["starts_at"])
                    _dur = int(ed.get("duration_minutes") or 90)
                    ed["ends_at"] = (_start + _td(minutes=_dur)).isoformat()
                except (ValueError, TypeError):
                    ed.pop("ends_at", None)

            # Exact picked place (stamped via /event-venue) — overrides any venue the
            # extractor lifted, and carries the precise pin through to publish.
            ev = session_ctx.get("event_venue")
            if isinstance(ev, dict) and str(ev.get("name") or "").strip():
                ed["venue_name"] = ev["name"]
                ed["venue_address"] = ev.get("address")
                ed["place_id"] = ev.get("place_id")
                ed["venue_lat"] = ev.get("lat")
                ed["venue_lng"] = ev.get("lng")

            # Join settings (capacity / approval / share) — parsed from the user's chip
            # taps, kept in session_ctx so the extractor's redraw can't drop them.
            settings = dict(session_ctx.get("event_settings") or {})
            _parse_event_settings(user_message, settings)
            session_ctx["event_settings"] = settings
            if settings.get("_cap_set"):
                ed["max_attendees"] = settings.get("max_attendees")
            if "auto_approve" in settings:
                ed["auto_approve"] = settings["auto_approve"]
            if "allow_attendee_share" in settings:
                ed["allow_attendee_share"] = settings["allow_attendee_share"]

            _title = str(ed.get("title") or "").strip()
            has_venue = bool(str(ed.get("venue_name") or "").strip())
            # Gate on whether we've ASKED where — not just on a venue being present, since
            # the extractor lifts one from the title ("Playdate at the Park" → "Park") and
            # that used to skip the where-step entirely.
            place_asked = bool(turn_ctx.get("event_place_asked"))
            # If the host named a real venue inline ("...at Tampines Park") before we
            # asked, honor it instead of re-asking — re-asking a place they already gave
            # is the "ignores info I gave" bug. Only drop a venue the extractor merely
            # lifted FROM THE TITLE ("Playdate at the Park" → "Park").
            if has_venue and not place_asked:
                import re as _re

                _v = str(ed.get("venue_name") or "").strip()
                _title_words = set(_re.findall(r"[a-z]+", _title.lower()))
                _venue_words = set(_re.findall(r"[a-z]+", _v.lower()))
                if _venue_words and _venue_words <= _title_words:
                    ed.pop("venue_name", None)
                    has_venue = False
                else:
                    turn_ctx["event_place_asked"] = True
                    place_asked = True
            # Settings are asked once each (capacity → approval → share), after place.
            cap_asked = bool(turn_ctx.get("event_cap_asked"))
            approval_asked = bool(turn_ctx.get("event_approval_asked"))
            share_asked = bool(turn_ctx.get("event_share_asked"))
            settings_done = cap_asked and approval_asked and share_asked
            complete = (
                bool(_title) and bool(wd) and bool(wt) and place_asked and has_venue and settings_done
            )
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
                event_id, publish_error = _auto_publish_event(user_id, user_jwt, ed)
                if event_id:
                    turn_ctx["event_id"] = event_id
                    turn_ctx["event_published_now"] = True
                    turn_ctx["event_affinity_asked"] = False
                    turn_ctx["event_when_date"] = None
                    turn_ctx["event_when_time"] = None
                    turn_ctx["event_place_asked"] = False
                    turn_ctx["event_venue"] = None
                    turn_ctx["event_settings"] = None
                    turn_ctx["event_cap_asked"] = False
                    turn_ctx["event_approval_asked"] = False
                    turn_ctx["event_share_asked"] = False
                    # Verification (if any) is done — drop the resume markers so a later
                    # turn doesn't re-enter the verify funnel after a clean publish.
                    turn_ctx["host_publish_pending"] = None
                    turn_ctx["pending_post_verify"] = None
                    reply = _event_published_reply(reply, ed)
                else:
                    # Publish was rejected — tell the user why instead of letting the
                    # orchestrator's "all set!" text fake success. Host mode stays active
                    # (set below), so once they verify / fix the spot a follow-up retries.
                    detail = (publish_error or "").lower()
                    not_verified = (
                        "phone_not_verified" in detail
                        or "not_authenticated" in detail
                        or ":403" in detail
                    )
                    if not_verified and not phone_verified:
                        # Proactive verify: the event is fully built but the guest isn't
                        # verified yet. Drive verification right here (email → OTP) instead
                        # of dead-ending on a "can't post" message with nothing to tap.
                        # Host mode + the complete draft stay set, so the turn after
                        # verification re-enters this branch and publishes for real.
                        # The discovery gate's signup/verify handlers own the email/OTP
                        # turns (see handle_discovery_turn's host-verify escape).
                        turn_ctx["requires_phone_verification"] = True
                        turn_ctx["host_publish_pending"] = True
                        if not session_ctx.get("host_publish_pending"):
                            turn_ctx["routing_phase"] = "await_signup_phone"
                            reply = (
                                f"Perfect — **{_title or 'your event'}** is all set! "
                                "To post it I just need to verify your email — what's your email?"
                            )
                        else:
                            # Already mid-verify (JWT can lag a turn after OTP) — hold the
                            # event and nudge, don't re-ask for the email.
                            reply = (
                                "Finishing verification — send one more message and I'll "
                                f"post **{_title or 'your event'}** right away."
                            )
                    else:
                        if "location" in detail or "venue" in detail:
                            # Re-open the where-step so they can pick a resolvable place.
                            turn_ctx["event_place_asked"] = False
                            ed.pop("venue_name", None)
                        reply = _publish_failure_reply(publish_error, _title)
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
                elif not place_asked or not has_venue:
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
                elif not cap_asked:
                    reply = f"Great — **{_title}**. How many can come?"
                    ed["suggestions"] = _CAPACITY_SUGGESTIONS
                    turn_ctx["event_cap_asked"] = True
                elif not approval_asked:
                    reply = "Who can join — open, or you approve each request?"
                    ed["suggestions"] = _APPROVAL_SUGGESTIONS
                    turn_ctx["event_approval_asked"] = True
                else:  # not share_asked
                    reply = "Last bit — can attendees invite others?"
                    ed["suggestions"] = _SHARE_SUGGESTIONS
                    turn_ctx["event_share_asked"] = True
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
