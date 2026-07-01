"""Unified Lana: discovery gates first (code), orchestrator for everything else (AI)."""

from __future__ import annotations

import logging
import re
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
    "bring_items",
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

# Block-local answers that resolve to the host's own block centroid — these need no
# exact Google pin (there's no Places entry for "my backyard"). Any OTHER venue the
# host names (a business, a park by name, a street) must be pinned via place-search so
# we store the exact spot guests can navigate to — not a bare name we'd blind-geocode.
_GENERIC_PLACES = {
    "my place", "my home", "my house", "home", "at home", "our place", "my apartment",
    "my backyard", "backyard", "the backyard", "my yard", "the yard",
    "the park", "park", "the playground", "playground", "the pool", "the clubhouse",
    "the community center", "community center", "the courtyard", "the lobby",
    "the block", "on the block", "somewhere on the block", "my block", "the green",
}


def _is_generic_place(venue: Any) -> bool:
    return str(venue or "").strip().lower().strip(".!?") in _GENERIC_PLACES

# Host join-settings chip options (capacity / approval / share).
_CAPACITY_SUGGESTIONS = ["Up to 6", "Up to 10", "Up to 15", "Open · no limit"]
_APPROVAL_SUGGESTIONS = ["Anyone can join", "I'll approve each request"]
_SHARE_SUGGESTIONS = ["Yes, let them share", "My invites only"]

# Default capacity seeded into the quick-setup carousel (host can tweak the slider).
_SETUP_DEFAULT_MAX = 8


def _seed_setup_defaults(ed: dict[str, Any]) -> None:
    """Pre-fill the quick-setup carousel (capacity / sharing / approval / bring) with
    sensible defaults so the host can just tap through — see the C-4-EVENT-P2B mockup."""
    if ed.get("max_attendees") is None:
        ed["max_attendees"] = _SETUP_DEFAULT_MAX
    if ed.get("auto_approve") is None:
        ed["auto_approve"] = False  # require-approval ON by default
    if ed.get("allow_attendee_share") is None:
        ed["allow_attendee_share"] = True
    if ed.get("bring_items") is None:
        ed["bring_items"] = []


def _ensure_setup_config(
    ed: dict[str, Any],
    *,
    history: list[dict[str, Any]],
    user_message: str,
    timer: Any,
) -> None:
    """Attach the AI-tailored setup-card config to the draft (once), so the FE renders one
    scrollable carousel of questions fit to THIS event ("How many moms?" vs "How many
    dads?", bring items that match the activity). Pre-fills bring chips + capacity from the
    AI's suggestions. Idempotent — recomputing on later setup turns would waste an LLM call
    and clobber the host's edits."""
    if ed.get("event_setup"):
        return
    from app.event_setup_suggest import setup_suggestions

    with timer.stage("llm_event_setup"):
        cfg = setup_suggestions(history=history, user_message=user_message, draft=ed)
    ed["event_setup"] = cfg
    if not ed.get("bring_items") and cfg.get("bring_suggestions"):
        ed["bring_items"] = list(cfg["bring_suggestions"])
    if ed.get("max_attendees") is None and cfg.get("capacity_default"):
        try:
            ed["max_attendees"] = int(cfg["capacity_default"])
        except (TypeError, ValueError):
            pass


def _ensure_review_draft(
    ed: dict[str, Any],
    *,
    history: list[dict[str, Any]],
    user_message: str,
    timer: Any,
) -> None:
    """Draft a title + one-line description (like the "Drafted by Lana" card) when the
    opening message gave content but no explicit name/blurb — so the review shows a real
    title + description instead of a bare draft. Best-effort; skips the LLM if both exist."""
    have_title = bool(str(ed.get("title") or "").strip()) and not _is_generic_title(ed.get("title"))
    have_desc = bool(str(ed.get("description") or "").strip())
    if have_title and have_desc:
        return
    from app.event_suggest import event_suggestions

    with timer.stage("llm_event_suggest"):
        sugg = event_suggestions(history=history, user_message=user_message, draft=ed)
    if not have_title:
        titles = sugg.get("title_suggestions") or []
        if titles:
            ed["title"] = titles[0]
    if not have_desc and sugg.get("description"):
        ed["description"] = sugg["description"]


# CTA strings the FE sends from the host review / setup / confirm cards. Matched loosely
# (substring) so the carousel's "Looks good · next" and the "Drop the meet up" button both
# land, the same way hosting_cta.py keys off button labels.
def _norm_cta(msg: str) -> str:
    return str(msg or "").strip().lower()


def _is_host_confirm(msg: str) -> bool:
    n = _norm_cta(msg)
    return "looks good" in n or n in {"yes", "perfect", "next", "all set", "sounds good"}


def _is_host_drop(msg: str) -> bool:
    n = _norm_cta(msg)
    return "drop" in n or "post it" in n or "publish" in n or "go live" in n


def _is_host_tweak(msg: str) -> bool:
    n = _norm_cta(msg)
    return "tweak" in n or "let me change" in n


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


def _nearby_host_place_rows(
    zip_code: str | None, block_id: str | None, user_id: str | None
) -> list[dict[str, Any]]:
    """Real nearby venues to host at (Google Places around the block), WITH the exact
    pin (name/place_id/lat/lng/address) — so a tapped suggestion can be stamped directly
    instead of bouncing the host into "Which X exactly?". [] with no key or no results,
    so the where-step falls back to the generic chips."""
    try:
        from app.places import search_places

        return search_places(
            query="park playground community center",
            zip_code=zip_code,
            block_id=block_id,
            user_id=user_id,
            limit=3,
        )
    except Exception:  # noqa: BLE001 - best-effort
        return []


def _where_step_chips(
    session_ctx: dict[str, Any], home_block_id: str | None, user_id: str | None,
    turn_ctx: dict[str, Any],
) -> list[str]:
    """Where-step chips: real nearby places (+ "My place") or the generic fallback, plus
    Search. Stashes the nearby places' pins on turn_ctx keyed by their chip label, so the
    next turn can stamp the exact spot when the host taps one (see the auto-pin above)."""
    rows = _nearby_host_place_rows(
        str(session_ctx.get("zip_code") or "").strip() or None,
        home_block_id,
        user_id,
    )
    names: list[str] = []
    candidates: dict[str, Any] = {}
    for r in rows:
        nm = str((r or {}).get("name") or "").strip()[:60]
        if nm and nm not in names and str((r or {}).get("place_id") or "").strip():
            names.append(nm)
            candidates[nm.lower()] = {**r, "name": nm}
    if candidates:
        turn_ctx["event_place_candidates"] = candidates
    base = (names + ["My place"]) if names else list(_PLACE_SUGGESTIONS)
    return base + [_SEARCH_PLACE_OPTION]

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


# Cheap gate: does this message plausibly mention a date/time at all? Used to skip the
# LLM when-resolver on turns that clearly carry none (e.g. tapping a capacity/approval/
# share chip), which otherwise paid for one LLM round-trip on EVERY host turn.
_TEMPORAL_TOKEN_RE = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"today|tonight|tomorrow|weekend|week|month|noon|midnight|morning|afternoon|"
    r"evening|night|next|this|am|pm)\b"
    r"|\d{1,2}\s*(?:am|pm)"  # "10am", "9 pm"
    r"|\d{1,2}:\d{2}"  # "9:30"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b",  # "28th"
    re.I,
)


def _has_temporal_tokens(text: str) -> bool:
    return bool(_TEMPORAL_TOKEN_RE.search(str(text or "")))


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

    # Guarantee the home block is persisted the moment a user is verified — independent of
    # what they do this turn (browse, host, ask a question). Signing up always collects a
    # ZIP, so a verified user should NEVER be left blockless; this closes the gap where the
    # block was only saved on the peer-match turn. Idempotent + no-op once a block exists or
    # when nothing is known yet (anonymous guest with no ZIP — they get asked in-flow).
    if phone_verified and not home_block_id:
        try:
            from app.discovery_route import ensure_home_block_for_verified_user

            assigned = ensure_home_block_for_verified_user(user_jwt, session_ctx=session_ctx)
            if assigned:
                home_block_id = assigned  # use it for the rest of THIS turn too
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("verified_block_assign_failed")

    # Sticky "pass along an item" capture owns the whole turn (deterministic flow +
    # structured extraction), the same way event_host_active does. It releases on
    # save, cancel, or a turn cap, so other flows are never affected.
    if session_ctx.get("pass_along_active"):
        from app.discovery_slots import discovery_slots_for_turn
        from app.pass_along import (
            pass_along_should_release,
            reset_pass_along_state,
            run_pass_along_turn,
        )

        # Classify so the capture releases on a semantic abandon ("no i dont wanna
        # giveaway") or a pivot to another lane ("find me a jogging partner") — the AI's
        # read, not just cancel keywords. Same pattern as look_meet / activity_browse.
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
        if pass_along_should_release(user_message, session_ctx, pivot_slots):
            reset_pass_along_state(session_ctx)
        else:
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
        from app.discovery_slots import discovery_slots_for_turn
        from app.tip_share import (
            reset_tip_share_state,
            run_tip_share_turn,
            tip_share_should_release,
        )

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
        if tip_share_should_release(user_message, session_ctx, pivot_slots):
            reset_tip_share_state(session_ctx)
        else:
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
        # If the host gate released this turn (the user pivoted/abandoned), do NOT let a
        # lingering draft re-pin host mode — that would re-trap the user we just freed.
        host_released = bool(session_ctx.get("host_released_this_turn"))
        session_ctx["host_released_this_turn"] = None
        host_active = bool(session_ctx.get("event_host_active"))
        # Self-engage: if the orchestrator is actively shaping an event_draft, pin host
        # mode even when entry classification was fuzzy or the CTA hint didn't arrive.
        _draft_for_progress = draft if isinstance(draft, dict) else session_ctx.get("event_draft")
        draft_in_progress = isinstance(_draft_for_progress, dict) and any(
            _draft_for_progress.get(k) for k in ("title", "venue_name", "starts_at", "affinity_prompt")
        )
        host_active = host_active or (draft_in_progress and not host_released)
        # Deterministic back-out cleanup — works no matter who wrote the reply. A leaked
        # event_host_active must never trap the user: when host mode is on but the flow has
        # made NO progress (no title/venue/date yet) AND the classifier does not read THIS
        # turn as hosting, the user has effectively left (e.g. a conversational abandon the
        # orchestrator answered without touching the flag). Clear the flag so the host card
        # can't re-appear next turn. Real draft progress protects the multi-step build; the
        # entry turn is safe because it classified AS hosting. AI read, not keywords
        # (see [[no-sticky-flows]] / [[sticky-lane-release-inversion]]).
        if host_active and not draft_in_progress and not host_released:
            from app.layer1_intents import slots_indicate_hosting_signal

            if not slots_indicate_hosting_signal(session_ctx.get("_discovery_slots") or {}):
                from app.discovery_route import _release_host_mode

                _release_host_mode(session_ctx)
                host_active = False

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
            # Recover from the draft's persisted starts_at when the session keys were
            # dropped mid-flow (a de-stick release nulls event_when_date but the orchestrator
            # rebuilds starts_at from history — so the card kept the date while the step-gate
            # thought it was missing and re-asked "when?"). The draft is the single source of
            # truth; back-fill from it before re-deriving from the current message.
            if (not wd or not wt) and ed.get("starts_at"):
                from datetime import datetime as _dt_recover

                try:
                    _existing = _dt_recover.fromisoformat(str(ed["starts_at"]))
                    if not wd:
                        wd = _existing.date().isoformat()
                    if not wt:
                        wt = _existing.strftime("%H:%M")
                except (ValueError, TypeError):
                    pass
            # AI-first when-resolution: the LLM (anchored on today's date inside
            # resolve_event_when) handles ordinals ("28th June"), negation ("not on
            # friday"), and relative phrasing the old regex choked on, and already
            # snapped any past date to its next future occurrence. It returns None only
            # when the LLM is unavailable/errors — then we fall back to the regex
            # resolver. Trusting the model when it DID run also avoids the regex
            # re-matching a stray weekday ("friday" in "not on friday").
            from app.event_when import resolve_event_when

            # Skip the LLM date resolver when the draft already has a start AND this message
            # carries no temporal words — otherwise it ran on EVERY host turn (incl. tapping
            # a capacity/approval/share chip), adding one LLM round-trip each time.
            if wd and wt and not _has_temporal_tokens(user_message):
                when = {}
            else:
                with timer.stage("llm_event_when"):
                    when = resolve_event_when(
                        history=history, user_message=user_message, draft=ed
                    )
            if when is None:
                nd = _resolve_event_date(user_message)
                ntime = _resolve_event_time(user_message)
            else:
                nd = when.get("date")
                ntime = when.get("time")
            if nd:
                wd = nd
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
            # A named venue ("KFC", "Foxtail Coffee") is only resolvable once the host has
            # picked the exact place (place_id) — otherwise publish would blind-geocode the
            # name to *some* matching spot. Block-local answers ("my place", "the park")
            # need no pin; they resolve to the host's block.
            has_pin = bool(str(ed.get("place_id") or "").strip())
            # Auto-pin a tapped nearby suggestion. Those chips are real Google places we
            # surfaced WITH a pin (place_id/lat/lng), but the chip only sends its NAME, so
            # the extractor leaves it unpinned and the host gets bounced into "Which X
            # exactly?". Match the tapped label to the stashed candidate and stamp it
            # directly — same shape as the /event-venue Search pick.
            if place_asked and not has_pin:
                _cands = session_ctx.get("event_place_candidates")
                _row = (
                    _cands.get(str(user_message).strip().lower())
                    if isinstance(_cands, dict)
                    else None
                )
                if isinstance(_row, dict) and str(_row.get("place_id") or "").strip():
                    ed["venue_name"] = str(_row.get("name") or user_message).strip()
                    ed["venue_address"] = _row.get("address")
                    ed["place_id"] = _row.get("place_id")
                    ed["venue_lat"] = _row.get("lat")
                    ed["venue_lng"] = _row.get("lng")
                    has_venue = True
                    has_pin = True
            # Generic-place escape — when the host taps/types a block-local answer
            # ("My place", "the park", "somewhere on the block") at the where-step, the
            # LLM extractor leaves venue_name pinned to a previously-named spot
            # ("Randal Park Community Center"): "My place" isn't a "place name" per the
            # extractor prompt, so it returns null and the monotonic merge keeps the old
            # venue — trapping the host in the "Which X exactly?" disambiguation loop.
            # A generic place needs no Google pin (it resolves to the host's block), so
            # capture it deterministically from the user's own message here. Gate on
            # place_asked (don't grab a stray "the park" from an earlier step) and skip
            # when a precise pin already exists (a Search pick shouldn't be clobbered).
            if place_asked and not has_pin and _is_generic_place(user_message):
                ed["venue_name"] = str(user_message).strip()
                has_venue = True
            # Auto-resolve a venue the host named inline ("...at Foxtail", "KFC") to the
            # single nearest Google place (biased to their block), so we pin it immediately
            # instead of bouncing them into "Which X exactly?". Generic block-local answers
            # ("my place", "the park") need no pin. Tried at most once per name; the host can
            # still change it from the review card (the search picker is the tweak path).
            if (
                has_venue
                and not has_pin
                and not _is_generic_place(ed.get("venue_name"))
                and str(ed.get("venue_name") or "").strip()
                != str(session_ctx.get("event_venue_tried") or "").strip()
            ):
                _vname = str(ed.get("venue_name") or "").strip()
                turn_ctx["event_venue_tried"] = _vname
                from app.places import search_places

                try:
                    with timer.stage("places_autoresolve"):
                        _hits = search_places(
                            query=_vname,
                            zip_code=str(session_ctx.get("zip_code") or "").strip() or None,
                            block_id=home_block_id,
                            user_id=user_id,
                            limit=1,
                        )
                except Exception:  # noqa: BLE001 - best-effort; falls back to the picker
                    _hits = []
                _top = _hits[0] if _hits else None
                if isinstance(_top, dict) and str(_top.get("place_id") or "").strip():
                    ed["venue_name"] = str(_top.get("name") or _vname).strip()
                    ed["venue_address"] = _top.get("address")
                    ed["place_id"] = _top.get("place_id")
                    ed["venue_lat"] = _top.get("lat")
                    ed["venue_lng"] = _top.get("lng")
                    has_pin = True
                    place_asked = True
                    turn_ctx["event_place_asked"] = True
                    turn_ctx["event_venue"] = {
                        "name": ed["venue_name"],
                        "address": ed.get("venue_address"),
                        "place_id": ed.get("place_id"),
                        "lat": ed.get("venue_lat"),
                        "lng": ed.get("venue_lng"),
                    }

            stage = str(session_ctx.get("host_stage") or "")

            # On the entry turn, when the opening message already carried real content
            # (a time and/or a place), draft a title + one-line description so the review
            # reads like the "Drafted by Lana" card — instead of dropping into the carousel.
            if stage not in ("review", "setup", "confirm") and (wd or has_venue):
                _ensure_review_draft(
                    ed, history=history, user_message=user_message, timer=timer
                )
                _title = str(ed.get("title") or "").strip()
                if _is_generic_title(_title):
                    _title = ""

            # A named venue no longer needs a precise Google pin to proceed — publish
            # geocodes the name near the host's block, and auto-resolve above enriches it
            # with an exact pin when the Maps key is set. A venue NAME is enough to advance.
            venue_resolvable = has_venue
            blockers_done = (
                bool(_title) and bool(wd) and bool(wt) and place_asked and venue_resolvable
            )

            # Stage machine: review → setup (batched carousel) → confirm → publish. Nothing
            # is ever asked one field per turn — when the opening message is sparse we jump
            # straight to the setup carousel, which collects the missing title / when / where
            # TOGETHER with capacity / sharing / approval / bring in a single card.
            if stage not in ("review", "setup", "confirm"):
                # First host turn after extraction. Rich opening (blockers already known) →
                # show the drafted review (P2); sparse opening → straight to the batched
                # setup carousel so the host fills everything at once.
                ed["suggestions"] = []
                if blockers_done:
                    turn_ctx["host_stage"] = "review"
                    reply = (
                        f"Here's your meet — **{_title}**. Take a look: tap **Looks good** "
                        "to set it up, or **Let me tweak** to change anything."
                    )
                else:
                    _ensure_setup_config(
                        ed, history=history, user_message=user_message, timer=timer
                    )
                    _seed_setup_defaults(ed)
                    turn_ctx["host_stage"] = "setup"
                    reply = (
                        "Let's set it up — fill in the details below and I'll drop it on "
                        "your block."
                    )
            elif stage == "review":
                if _is_host_confirm(user_message) or _is_host_drop(user_message):
                    _ensure_setup_config(
                        ed, history=history, user_message=user_message, timer=timer
                    )
                    _seed_setup_defaults(ed)
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    reply = (
                        "Quick set-up — set capacity, sharing, approval, and what to "
                        "bring, then drop it on your block."
                    )
                else:
                    # A free-text edit was already merged into the draft above; stay in review.
                    turn_ctx["host_stage"] = "review"
                    ed["suggestions"] = []
                    reply = (
                        f"Sure — tell me what to change about **{_title}**."
                        if _is_host_tweak(user_message)
                        else f"Updated **{_title}** — does this look right?"
                    )
            elif stage == "setup":
                if (_is_host_confirm(user_message) or _is_host_drop(user_message)) and blockers_done:
                    turn_ctx["host_stage"] = "confirm"
                    ed["suggestions"] = []
                    reply = (
                        f"It's all set — **{_title}**. One last look, then drop it on the block."
                    )
                elif _is_host_confirm(user_message) or _is_host_drop(user_message):
                    # Carousel submitted but a blocker is still missing — hold in setup and
                    # say exactly what's needed (the FE cards should have collected these).
                    need: list[str] = []
                    if not _title:
                        need.append("a name")
                    if not (wd and wt):
                        need.append("a date & time")
                    if not venue_resolvable:
                        need.append("a place")
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    reply = "I just need " + " · ".join(need) + " to post it."
                else:
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    reply = "Set the details below, then tap **Looks good**."
            else:  # stage == "confirm" → publish when the host drops it
                if _is_host_drop(user_message) or _is_host_confirm(user_message):
                    event_id, publish_error = _auto_publish_event(user_id, user_jwt, ed)
                    if event_id:
                        turn_ctx["event_id"] = event_id
                        turn_ctx["event_published_now"] = True
                        turn_ctx["host_stage"] = None
                        turn_ctx["event_when_date"] = None
                        turn_ctx["event_when_time"] = None
                        turn_ctx["event_place_asked"] = False
                        turn_ctx["event_venue"] = None
                        turn_ctx["event_venue_tried"] = None
                        turn_ctx["event_place_candidates"] = None
                        turn_ctx["event_settings"] = None
                        # Verification (if any) is done — drop the resume markers so a later
                        # turn doesn't re-enter the verify funnel after a clean publish.
                        turn_ctx["host_publish_pending"] = None
                        turn_ctx["pending_post_verify"] = None
                        reply = _event_published_reply(reply, ed)
                    else:
                        # Publish rejected — surface the reason; hold at confirm so a retry
                        # (after verify / fixing the spot) republishes.
                        detail = (publish_error or "").lower()
                        not_verified = (
                            "phone_not_verified" in detail
                            or "not_authenticated" in detail
                            or ":403" in detail
                        )
                        turn_ctx["host_stage"] = "confirm"
                        if not_verified and not phone_verified:
                            turn_ctx["requires_phone_verification"] = True
                            turn_ctx["host_publish_pending"] = True
                            if not session_ctx.get("host_publish_pending"):
                                turn_ctx["routing_phase"] = "await_signup_phone"
                                reply = (
                                    f"Perfect — **{_title or 'your event'}** is all set! "
                                    "To post it I just need to verify your email — what's your email?"
                                )
                            else:
                                reply = (
                                    "Finishing verification — send one more message and I'll "
                                    f"post **{_title or 'your event'}** right away."
                                )
                        else:
                            if "location" in detail or "venue" in detail:
                                # Re-open the where-step so they can pick a resolvable place.
                                turn_ctx["event_place_asked"] = False
                                turn_ctx["event_venue_tried"] = None
                                turn_ctx["host_stage"] = "review"
                                ed.pop("venue_name", None)
                            reply = _publish_failure_reply(publish_error, _title)
                else:
                    turn_ctx["host_stage"] = "confirm"
                    ed["suggestions"] = []
                    reply = "Tap **Drop the meet up** when you're ready and I'll post it."
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
