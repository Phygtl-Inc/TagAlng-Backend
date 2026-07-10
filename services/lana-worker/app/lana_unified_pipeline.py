"""Unified Lana: discovery gates first (code), orchestrator for everything else (AI)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.analytics import track
from app.discovery_route import handle_discovery_turn, looks_like_logout
from app.faq_replies import faq_reply, faq_topic
from app.lana_dispatch import lana_unified_turn
from app.layer1_intents import faq_linear_intent
from app.lana_ui import sanitize_assistant_message
from app.lana_paths import unified_rules_first_enabled
from app.loop_guard import discovery_reply_is_stuck, reset_sticky_discovery_state
from app.orchestrator.pipeline import run_turn
from app.orchestrator.progress import READING
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


# The concierge's action `kind` IS a semantic intent decision — she already read whether the
# neighbor wants a place, an event, people, to host, or to share. Map each kind to the discovery
# intent so we route the hand-off by that decision instead of re-classifying the query text (which
# mis-reads a place like "nature trails" as a meet/browse). See _forced_slots_for_kind.
_KIND_TO_INTENT: dict[str, dict[str, Any]] = {
    "seek_tip": {"goal": "save_signal", "linear_intent": "looking.tip", "signal_intent": "tip_seek"},
    "find_activities": {
        "goal": "activities", "linear_intent": "discovery.find_activities",
        "signal_intent": None, "in_discovery": True,
    },
    "find_neighbors": {
        "goal": "peers", "linear_intent": "discovery.find_by_attrs",
        "signal_intent": None, "in_discovery": True,
    },
    "host_meet": {"goal": "save_signal", "linear_intent": "sharing.host", "signal_intent": "host_meet"},
    "share_tip": {"goal": "save_signal", "linear_intent": "sharing.tip", "signal_intent": "tip_share"},
}


def _forced_slots_for_kind(
    kind: str,
    query: str,
    action: Any,
    session_ctx: dict[str, Any],
    *,
    home_block_id: str | None,
    phone_verified: bool,
    history: list[dict[str, Any]],
    timer: "TurnTimer | None",
) -> dict[str, Any] | None:
    """Route a rapport hand-off by the concierge's semantic `kind`, not by re-guessing the noun.

    Parses the query once for its detail fields (what/where), then FORCES the intent to match the
    chosen kind and primes the per-message slot cache handle_discovery_turn reuses. Returns the
    forced slots, or None when the kind isn't routable (so the caller leaves routing to the AI).
    The parse is the same call handle_discovery_turn would make, so this adds no extra model call.
    """
    intent = _KIND_TO_INTENT.get(str(kind or "").strip())
    if intent is None:
        return None
    from app.discovery_slots import discovery_slots_for_turn

    base = discovery_slots_for_turn(
        session_ctx,
        query,
        routing_phase=str(session_ctx.get("routing_phase") or "listening"),
        history=history,
        has_block=bool(home_block_id or session_ctx.get("preview_block_id")),
        has_identity=bool(session_ctx.get("identity_snippet")),
        phone_verified=phone_verified,
        timer=timer,
    )
    slots = dict(base) if isinstance(base, dict) else {}
    slots.update(intent)
    # This is a committed decision, not a guess — clear any browse-vs-meet clarify tie and float
    # confidence so downstream lane gates fire instead of re-asking "what kind of thing?".
    slots["confidence"] = max(float(slots.get("confidence") or 0.0), 0.9)
    slots["abandon"] = False
    slots["clarify"] = None
    slots["clarify_question"] = None
    slots["clarify_options"] = []
    if not str(slots.get("signal_detail") or "").strip():
        topic = str(action.get("topic") or "").strip() if isinstance(action, dict) else ""
        slots["signal_detail"] = topic or None
    return slots


def _reset_rapport_state(session_ctx: dict[str, Any]) -> None:
    """Drop the concierge follow-up capture so the turn falls through to normal routing. Set to
    None (not popped) so the {**old, **new} session merge clears them instead of keeping a stale
    value across the round-trip."""
    for k in (
        "rapport_active", "rapport_answer",
        "rapport_followup_question", "rapport_followup_count", "rapport_reply",
        "rapport_offer_pending", "rapport_pending_action",
    ):
        session_ctx[k] = None


# Coarse "family" of an app-move so we can tell a genuine PIVOT (offer running moms → she asks for a
# playground) from an ACCEPTANCE of the offer. A bare "sure"/"yes" reads as a confident looking.meet
# in context, which is the SAME family as a find_neighbors offer → accept, dispatch the stored action.
_KIND_FAMILY: dict[str, str] = {
    "find_neighbors": "people", "find_activities": "activities",
    "host_meet": "host", "seek_tip": "tip", "share_tip": "tip",
}


def _slots_family(slots: dict[str, Any] | None) -> str | None:
    if not isinstance(slots, dict):
        return None
    from app.layer1_intents import normalize_linear_intent

    linear = normalize_linear_intent(slots.get("linear_intent")) or ""
    signal = str(slots.get("signal_intent") or "")
    goal = str(slots.get("goal") or "")
    if signal == "meet_seek" or goal == "peers" or "find_peers" in linear or "find_by_attrs" in linear or "meet" in linear:
        return "people"
    if goal == "activities" or "find_activities" in linear:
        return "activities"
    if signal == "host_meet" or "sharing.host" in linear:
        return "host"
    if signal in ("tip_seek", "tip_share") or "looking.tip" in linear or "sharing.tip" in linear:
        return "tip"
    return None


def _offer_is_pivot(slots: dict[str, Any] | None, pending_kind: str) -> bool:
    """True when a typed reply to a pending offer is a confident request for something in a DIFFERENT
    family than the offer (a real pivot to release), rather than an acceptance of it. When the family
    can't be told, default to False (accept) — dispatching the offered search beats trapping her."""
    from app.lane_decision import is_confident_off_lane

    if not is_confident_off_lane(slots, native_goals=frozenset({"chat"})):
        return False  # not a confident actionable turn (e.g. "sure"/"idk") → accept the offer
    fam = _slots_family(slots)
    pending_fam = _KIND_FAMILY.get(pending_kind)
    if fam is None or pending_fam is None:
        return False
    return fam != pending_fam


def _rapport_should_release(
    message: str, session_ctx: dict[str, Any], slots: dict[str, Any] | None
) -> bool:
    """Whether the concierge follow-up capture should hand back to normal routing this turn.

    Reuses the SAME universal-exit machinery every sticky lane uses (lane_should_continue) so we
    do NOT rebuild logout/pivot/safety inside the concierge: abandon, unsafe/out_of_scope, and any
    confident PIVOT to a real find/host/tip intent all release. A warm getting-to-know-you answer
    ('I usually run alone', 'idk', 'both', 'yes') is goal=chat while active_capture=rapport, so it
    stays; a clear 'show me parks' reads as tip_seek and releases to the real search."""
    from app.lane_decision import is_confident_off_lane, lane_should_continue

    def _is_rapport_answer(_msg: str, _ctx: dict[str, Any], s: dict[str, Any] | None) -> bool:
        # Rapport owns chatty getting-to-know-you turns; anything confidently actionable is a pivot.
        return not is_confident_off_lane(s, native_goals=frozenset({"chat"}))

    return not lane_should_continue(
        message, session_ctx, slots, is_valid_answer=_is_rapport_answer, pivot_re=None
    )


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
    if "duplicate_event" in detail or "duplicate key" in detail:
        # The dedupe guard fired — same title + start already posted by this host.
        return f"Looks like you already have that meet — want to edit {name} instead?"
    if "location" in detail or "venue" in detail:
        return (
            f"I have everything for {name} except a spot I can place on the map. "
            "Pick a place or share an address and I'll post it."
        )
    return (
        f"I hit a snag posting {name} just now — give it another try in a moment "
        "and I'll get it up."
    )


def _host_publish_gate(
    *,
    user_message: str,
    ed: dict[str, Any],
    wd: Any,
    user_id: str,
    session_ctx: dict[str, Any],
    turn_ctx: dict[str, Any],
    title: str,
    wants_drop: bool,
) -> tuple[str | None, bool]:
    """Pre-publish sanity gate at the confirm stage (see app.event_guards) — warm
    clarifying questions for a venue implausibly far from the block or a kids event in
    quiet hours, never hard rejections. Returns ``(reply, proceed)``:

    - ``(question/ack, False)`` — show this reply, don't publish this turn;
    - ``(None, True)`` — clear (or confirmed once): the drop may proceed;
    - ``(None, False)`` — not a drop turn; the caller's normal handling applies.

    Confirmation is remembered in session ctx (``event_guards_confirmed``), so one
    confirmation proceeds and a confirmed guard is never re-asked (no loop).
    """
    from app.event_guards import (
        GUARD_KID_HOURS,
        classify_guard_answer,
        pending_event_guard,
    )

    guards_ok: dict[str, Any] = dict(session_ctx.get("event_guards_confirmed") or {})
    turn_ctx["event_guards_confirmed"] = guards_ok
    pending = str(session_ctx.get("event_guard_pending") or "")
    if pending:
        # Last turn asked a clarifying question — read the host's answer.
        turn_ctx["event_guard_pending"] = None
        answer = classify_guard_answer(user_message)
        if answer == "confirm" or (answer is None and wants_drop):
            # The host means it — remember so we never re-ask, and proceed to publish.
            guards_ok[pending] = True
            wants_drop = True
        elif answer == "change":
            turn_ctx["host_stage"] = "confirm"
            turn_ctx["host_aside"] = True  # show Lana's TEXT, not just the card
            if pending == GUARD_KID_HOURS:
                # "Did you mean noon?" → yes: move the start to 12:00 local.
                turn_ctx["event_when_time"] = "12:00"
                if wd:
                    ed["starts_at"] = f"{wd}T12:00:00"
                    from datetime import datetime as _dt, timedelta as _td

                    try:
                        _start = _dt.fromisoformat(ed["starts_at"])
                        _dur = int(ed.get("duration_minutes") or 90)
                        ed["ends_at"] = (_start + _td(minutes=_dur)).isoformat()
                    except (ValueError, TypeError):
                        ed.pop("ends_at", None)
                ed["suggestions"] = ["Drop the meet up"]
                return (
                    f"Done — **{title or 'your meet'}** now starts at noon. "
                    "Tap **Drop the meet up** and I'll post it.",
                    False,
                )
            # Far venue → re-open the where-step for a nearby pick.
            for _k in ("venue_name", "venue_address", "place_id", "venue_lat", "venue_lng"):
                ed.pop(_k, None)
            turn_ctx["event_place_asked"] = False
            turn_ctx["event_venue"] = None
            turn_ctx["event_venue_tried"] = None
            session_ctx["event_venue"] = None
            turn_ctx["host_stage"] = "review"
            ed["suggestions"] = list(_PLACE_SUGGESTIONS) + [_SEARCH_PLACE_OPTION]
            return (
                "No problem — where should it be? Pick a spot below or search a "
                "place and I'll pin the exact one.",
                False,
            )
        # Unclear answer: any free-text edit already merged into the draft above; the
        # guard stays unconfirmed, so the next drop re-checks against the new values.

    if not wants_drop:
        return None, False

    guard = pending_event_guard(ed, user_id, confirmed=guards_ok)
    if guard is not None:
        # Something looks off — ask instead of publishing (or hard-rejecting). The
        # host may genuinely mean it; their confirmation next turn proceeds.
        turn_ctx["event_guard_pending"] = guard["id"]
        turn_ctx["host_stage"] = "confirm"
        turn_ctx["host_aside"] = True  # the question must be visible over the card
        ed["suggestions"] = list(guard["options"])
        return guard["question"], False
    return None, True


_TITLE_SUGGESTIONS = ["Playdate at the park", "Weekend playgroup", "Morning meetup"]
# A "what time to start?" question (the day is already chosen) → a concrete clock
# spread, so one tap resolves the time instead of re-offering days.
_START_TIME_SUGGESTIONS = ["9 AM", "12 PM", "3 PM", "6 PM"]


def _when_suggestions() -> list[str]:
    """Concrete upcoming weekend dates ("Sat Jun 20", "Sun Jun 21", + next weekend)
    so a "when?" answer lands on a REAL date instead of a vague "this weekend" the
    extractor can't pin to a calendar day. Computed from the host's LOCAL clock
    (event tz), not the server's UTC day."""
    from datetime import timedelta

    from app.event_when import event_local_now

    today = event_local_now().date()
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
    # Word-bounded "drop" — a bare substring match read "MWF 7am before preschool
    # dropoff" as the publish CTA and swallowed the schedule it carried.
    return bool(re.search(r"\bdrop\b", n)) or "post it" in n or "publish" in n or "go live" in n


def _is_host_tweak(msg: str) -> bool:
    n = _norm_cta(msg)
    return "tweak" in n or "let me change" in n


# Conversational affirmations ("yes that works", "looks good", "perfect"). The review /
# confirm cards ask "does this look right?" — a plain yes MUST be read as a state
# transition BEFORE any field extraction, never consumed as an "edit" (the production
# "Updated **X** — does this look right?" forever-loop: 0/16 conversational hosts ever
# posted). Deterministic and fast: the WHOLE message must be affirmation words (so
# "yes, at 4pm" still carries info and flows through extraction) and contain at least
# one unambiguous affirm anchor (so a bare "that" or "sounds" never matches).
_AFFIRM_VOCAB = frozenset({
    "yes", "yep", "yeah", "yup", "ya", "sure", "ok", "okay", "k", "kk", "perfect",
    "great", "awesome", "lovely", "nice", "cool", "fine", "good", "right", "correct",
    "exactly", "sounds", "looks", "that", "thats", "that's", "this", "it", "its",
    "it's", "is", "works", "work", "worked", "for", "to", "me", "us", "all", "set",
    "done", "deal", "then", "lets", "let's", "do", "go", "ahead", "please", "thanks",
    "thank", "you", "lgtm", "love",
})
_AFFIRM_ANCHORS = frozenset({
    "yes", "yep", "yeah", "yup", "ya", "sure", "ok", "okay", "perfect", "great",
    "awesome", "lovely", "nice", "good", "works", "work", "right", "correct",
    "exactly", "fine", "cool", "lgtm", "love", "set", "done", "deal",
})


def _is_host_affirm(msg: str) -> bool:
    words = re.sub(r"[^a-z']+", " ", _norm_cta(msg)).split()
    if not words or len(words) > 6:
        return False
    return all(w in _AFFIRM_VOCAB for w in words) and any(w in _AFFIRM_ANCHORS for w in words)


def _classify_host_reply(msg: str) -> str:
    """Deterministic read of the host's reply to a review / setup / confirm card —
    BEFORE field extraction: 'drop' (publish CTA), 'tweak', 'affirm' (button label or
    conversational yes), or 'other' (an edit / question that flows through extraction)."""
    if _is_host_drop(msg):
        return "drop"
    if _is_host_tweak(msg):
        return "tweak"
    if _is_host_confirm(msg) or _is_host_affirm(msg):
        return "affirm"
    return "other"


def _ask_one_missing(need: list[str]) -> str:
    """The host said yes but a blocker is still missing — ask for exactly ONE field
    (the first missing), never the whole list and never the unchanged card again."""
    first = need[0] if need else "a detail"
    if first == "a name":
        return "Love it — one thing before I post: what should we call it?"
    if first == "a date & time":
        return "Great — one thing before I post: when should it be? Give me a day and a time."
    return "Great — one thing before I post: where should we meet? Name a spot and I'll pin it."


# A bare ZIP (5 digits, optional +4) is an AREA, never a venue — "I'll use 34786 as the
# meeting spot" must keep the venue empty and re-ask for a real place.
_ZIP_TOKEN_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def _is_zip_token(venue: Any) -> bool:
    return bool(_ZIP_TOKEN_RE.match(str(venue or "").strip()))


def _detect_recurrence(text: str) -> list[int] | None:
    """Weekday numbers (Mon=0) for a RECURRING cadence the host expressed ("MWF 7am",
    "wednesdays 4pm", "every tuesday", "weekly") — or None when the message carries no
    recurrence. Recurring meets aren't supported yet, so the flow grounds the draft on
    the FIRST occurrence instead of letting the extractor null the whole draft or emit
    a non-ISO starts_at ("next Wednesday 16:00:00")."""
    t = str(text or "").lower()
    days: set[int] = set()
    for name, wd in _WEEKDAYS.items():
        if re.search(rf"\b{name}s\b", t):  # plural: "wednesdays", "mondays", "sats"
            days.add(wd)
        if re.search(rf"\bevery\s+(?:other\s+)?{name}\b", t):
            days.add(wd)
    if re.search(r"\bmwf\b", t):
        days.update({0, 2, 4})
    if re.search(r"\btths?\b|\bt/th\b", t):
        days.update({1, 3})
    # Slash-joined weekday runs: "mon/wed/fri", "tue/thu"
    run = re.search(
        r"\b(?:mon|tues?|wed|thur?s?|fri|sat|sun)(?:\s*/\s*(?:mon|tues?|wed|thur?s?|fri|sat|sun))+\b",
        t,
    )
    if run:
        for part in re.split(r"\s*/\s*", run.group(0)):
            wd = _WEEKDAYS.get(part.strip()[:3])
            if wd is not None:
                days.add(wd)
    if days:
        return sorted(days)
    if re.search(r"\bweekly\b|\bevery week\b|\beach week\b", t):
        return []  # recurrence with no named day — note it, no first date to compute
    return None


def _first_recurrence_date(days: list[int], today: Any) -> str | None:
    """The first occurrence of a recurring cadence — the nearest named weekday strictly
    AFTER today (a "wednesdays 4pm" typed on Wednesday means starting next week, not a
    meet posted for the past hour)."""
    from datetime import timedelta

    if not days:
        return None
    delta = min(((d - today.weekday() - 1) % 7) + 1 for d in days)
    return (today + timedelta(days=delta)).isoformat()


def _friendly_when(wd: str, wt: str | None) -> str:
    """"Fri Jul 10, 7 AM" — a human phrase for the recurrence first-occurrence ask."""
    from datetime import datetime as _dt

    try:
        d = _dt.fromisoformat(f"{wd}T{wt or '00:00'}:00")
    except (ValueError, TypeError):
        return wd
    day = f"{d.strftime('%a %b')} {d.day}"
    if not wt:
        return day
    clock = d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return f"{day}, {clock}"


def _host_blockers_needed(title: str, wd: Any, wt: Any, venue_ok: bool) -> list[str]:
    """The blocker phrases still missing before a meet can post — title, when, place."""
    need: list[str] = []
    if not title:
        need.append("a name")
    if not (wd and wt):
        need.append("a date & time")
    if not venue_ok:
        need.append("a place")
    return need


def _host_fallback_nudge(need: list[str]) -> str:
    """Deterministic safety net ONLY for when the LLM host-turn brain is unavailable — never the
    primary path. Kept minimal so a degraded turn still moves forward instead of dead-ending."""
    if not need:
        return "That's everything — tap **Looks good** and I'll drop it on your block."
    return f"Just need {' · '.join(need)} — tell me and I'll add it, or fill it in below."


def _apply_host_brain(
    brain: dict[str, Any],
    ed: dict[str, Any],
    turn_ctx: dict[str, Any],
    session_ctx: dict[str, Any],
    settings: dict[str, Any],
    existing_title: str,
) -> None:
    """Apply the LLM host-turn brain's extraction to the draft — monotonically (never clobber a
    real value the host already gave). Understanding is the LLM's job; this just records it."""
    title = str(brain.get("title") or "").strip()
    have_real_title = bool(existing_title) and not _is_generic_title(existing_title)
    # Reject a generic name in both raw ("meetup") and article-led ("a meetup") form.
    title_generic = _is_generic_title(title) or _is_generic_title(
        re.sub(r"^(?:a|an|the)\s+", "", title, flags=re.I)
    )
    if title and not have_real_title and not title_generic:
        ed["title"] = title[:80]
    place = str(brain.get("place") or "").strip()
    if _is_zip_token(place):
        place = ""  # a bare ZIP is never a venue — keep asking for a real place
    if place and not str(ed.get("venue_name") or "").strip():
        ed["venue_name"] = place[:80]
        turn_ctx["event_place_asked"] = True
    cap = brain.get("capacity")
    if isinstance(cap, int):
        ed["max_attendees"] = cap
        settings["max_attendees"] = cap
        settings["_cap_set"] = True
        session_ctx["event_settings"] = settings
    if isinstance(brain.get("auto_approve"), bool):
        ed["auto_approve"] = brain["auto_approve"]
    if isinstance(brain.get("allow_share"), bool):
        ed["allow_attendee_share"] = brain["allow_share"]


def _parse_event_settings(message: str, settings: dict[str, Any]) -> None:
    """Read capacity / approval / share signals from the user's tap (or words) into the
    settings dict. Matches the chip labels; harmless on unrelated messages."""
    import re

    m = str(message or "").lower()
    # capacity
    rng = re.search(r"\b(\d{1,3})\s*(?:-|to|–)\s*(\d{1,3})\b", m)
    if re.search(r"no limit|unlimited|\bopen\b|any number|as many", m):
        settings["max_attendees"] = None  # unlimited
        settings["_cap_set"] = True
    elif rng and re.search(r"people|neighbou?r|folks|guest|ppl|of us|group|attendee", m):
        # A range in a headcount context ("5-7 people") → cap at the upper bound. Guarded to a
        # people-word so a time range ("3-5pm") can't be misread as capacity.
        settings["max_attendees"] = int(rng.group(2))
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


def _resolve_event_date(text: str, *, today: Any = None) -> str | None:
    """Deterministically resolve a date phrase → ISO 'YYYY-MM-DD' for the NEXT occurrence
    (correct year), since the LLM extractor mis-guesses the year. None if no date found.
    Grounded on the host's LOCAL day (event tz) — the server's UTC day is tomorrow every
    evening, which shifted "tomorrow"/"thursday" one day forward."""
    import re
    from datetime import datetime, timedelta

    from app.event_when import event_local_now

    t = str(text or "").lower()
    today = today or event_local_now().date()

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
    timer.emit(READING)
    session_ctx = {
        **session_ctx,
        "phone_verified": phone_verified,
        "unified_mode": True,
    }
    # Set when a rapport concierge turn hands off (zero-tap dispatch) so we can log where the
    # re-driven request actually routed — diagnostic for "yes → wrong lane" mis-routes.
    rapport_handoff_send: str | None = None

    # Wipe per-turn surfaces (…_listed_now, …_published_now, saved cards) up front, so
    # a one-shot card from a prior turn never leaks into this one — the early host /
    # pass-along / tip gates return before the discovery-path clear would run.
    # tapped_goal arrives ON this turn's request (stamped by main.py from the chip's
    # structured payload) and is itself turn-scoped — hold it across the wipe and
    # re-stamp so this turn can consume it.
    tapped_goal = session_ctx.get("tapped_goal")
    clear_turn_surfaces(session_ctx)
    if isinstance(tapped_goal, dict):
        session_ctx["tapped_goal"] = tapped_goal

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
            # Rapport concierge capture releases on logout too — same universal exit, one path.
            "rapport_active", "rapport_answer", "rapport_followup_question",
            "rapport_followup_count", "rapport_reply", "rapport_offer_pending",
            "rapport_pending_action",
        ):
            session_ctx[_k] = None

    # Product FAQ gate — QA 2026-07-08: 4/4 direct questions (safety, who-is-this-for,
    # childcare, ZIP privacy) went unanswered, each swallowed by a funnel line or an event
    # dump. A direct question outranks a chip-primed capture (intent_hint just set
    # activity_browse/pass_along/tip flags on the ctx) and every sticky flow below, so it
    # is answered HERE — before any gate can consume the turn. Flow state is deliberately
    # left untouched: each answer ends by re-offering the ongoing goal, so the interrupted
    # flow resumes next turn. Same deterministic detector the discovery route uses.
    _faq = faq_linear_intent(user_message)
    if _faq is not None:
        track(
            "faq_answered",
            user_id=user_id,
            event_properties={"topic": faq_topic(_faq)},
        )
        ctx = dict(session_ctx)
        ctx["active_intent"] = _faq
        ctx["_orchestrator_turn"] = False
        ctx["timing_ms"] = timer.to_dict()
        ctx["last_routing"] = {
            "outcome": "faq_answer",
            "intent_class": "help",
            "tool_called": None,
        }
        ui = {"bucket": None, "focus_phrase": None, "highlights": []}
        return (
            sanitize_assistant_message(faq_reply(_faq) or ""),
            "continue",
            ctx,
            ui,
            ctx.get("event_draft"),
        )

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

    # A STRUCTURED chip tap (goal payload with kind + topic, stamped by main.py) is a committed
    # app-move: force the intent to the chip's semantic kind so the goal can never be lost to
    # re-parsing the display text, and release any rapport capture — the tap IS the acceptance.
    # Plain text messages (no payload) keep the legacy string-match path below untouched.
    if isinstance(tapped_goal, dict) and str(tapped_goal.get("kind") or "").strip() in _KIND_TO_INTENT:
        _tap_kind = str(tapped_goal.get("kind") or "").strip()
        forced_slots = _forced_slots_for_kind(
            _tap_kind, user_message, tapped_goal, session_ctx,
            home_block_id=home_block_id, phone_verified=phone_verified,
            history=history, timer=timer,
        )
        if forced_slots is not None:
            session_ctx["_discovery_slots"] = forced_slots
            session_ctx["_discovery_slots_for"] = user_message.strip()
            logging.getLogger(__name__).info(
                "tapped_goal forced_intent kind=%s topic=%r -> goal=%s linear=%s signal=%s",
                _tap_kind, tapped_goal.get("topic"), forced_slots.get("goal"),
                forced_slots.get("linear_intent"), forced_slots.get("signal_intent"),
            )
        _reset_rapport_state(session_ctx)

    # A "By the way…" tile answer owns the turn: save the claim, close the gap, and reply via
    # the concierge engine (acknowledge her answer + one grounded follow-up), NOT the classifier
    # — a bare answer ("I love trying new restaurants") would be misread as a fresh chat intent.
    rapport = session_ctx.get("rapport_answer")
    # A reply to the concierge's own follow-up is a rapport ANSWER — but it must NOT be a separate
    # sticky path that re-implements logout / pivots / safety. So it's a capture the SAME classifier
    # owns (active_capture=rapport): re-classify the turn and RELEASE to normal routing on any pivot,
    # abandon, or unsafe (logout is caught above). Only a genuine getting-to-know-you answer
    # ("I usually run alone", "idk", "yes") stays and gets a concierge reply. The seed turn (first
    # tile answer, rapport_answer set) never re-classifies — it always seeds the flow.
    if not isinstance(rapport, dict) and session_ctx.get("rapport_active"):
        from app.discovery_slots import discovery_slots_for_turn

        if session_ctx.get("rapport_offer_pending"):
            # She's responding to a pending app-move offer ("Want to meet other park moms?"). Decide
            # accept / decline / pivot, then DISPATCH the concierge's stored action ourselves on accept
            # — deterministically, whether she TAPPED the chip or typed "sure"/"yes". We never re-hand
            # the acceptance to the classifier (a bare "sure" reads as a topic-less looking.meet →
            # "when works for you?" timing loop) or to the concierge (a reply writer — it narrates
            # "you're already connected" instead of running the search). The stored action already
            # carries the right kind + topic.
            pending_action = session_ctx.get("rapport_pending_action")
            pending_send = (
                str(pending_action.get("send") or "").strip()
                if isinstance(pending_action, dict) else ""
            )
            pending_kind = (
                str(pending_action.get("kind") or "").strip()
                if isinstance(pending_action, dict) else ""
            )
            tapped = bool(pending_send) and user_message.strip() == pending_send
            decision = "accept" if tapped else "close"
            if pending_send and not tapped:
                rap_slots = discovery_slots_for_turn(
                    session_ctx,
                    user_message,
                    routing_phase=str(session_ctx.get("routing_phase") or "listening"),
                    history=history,
                    has_block=bool(home_block_id or session_ctx.get("preview_block_id")),
                    has_identity=bool(session_ctx.get("identity_snippet")),
                    phone_verified=phone_verified,
                    timer=timer,
                )
                if isinstance(rap_slots, dict) and rap_slots.get("abandon"):
                    decision = "close"  # "no thanks" / bailed → warm close
                elif _offer_is_pivot(rap_slots, pending_kind):
                    decision = "pivot"  # a different real request → normal routing
                else:
                    decision = "accept"  # "sure" / "yes" / "ok" → run the offered search
            if decision == "accept" and pending_send:
                logging.getLogger(__name__).info(
                    "rapport_offer_accept dispatch kind=%s send=%r tapped=%s",
                    pending_kind, pending_send, tapped,
                )
                rapport_handoff_send = pending_send
                user_message = pending_send
                forced_slots = _forced_slots_for_kind(
                    pending_kind, pending_send, pending_action, session_ctx,
                    home_block_id=home_block_id, phone_verified=phone_verified,
                    history=history, timer=timer,
                )
                if forced_slots is not None:
                    session_ctx["_discovery_slots"] = forced_slots
                    session_ctx["_discovery_slots_for"] = pending_send
                    logging.getLogger(__name__).info(
                        "rapport_offer_accept forced_intent kind=%s -> goal=%s linear=%s signal=%s",
                        pending_kind, forced_slots.get("goal"),
                        forced_slots.get("linear_intent"), forced_slots.get("signal_intent"),
                    )
                _reset_rapport_state(session_ctx)
                rapport = None  # skip the concierge block; fall through to the discovery gates below
            elif decision == "pivot":
                _reset_rapport_state(session_ctx)  # her real request drives normal routing
                rapport = None
            else:
                # Decline (or a stored offer we can't dispatch) → let the concierge close warmly.
                # Clear the offer so it isn't re-dispatched, but keep the capture for the reply.
                session_ctx["rapport_offer_pending"] = None
                session_ctx["rapport_pending_action"] = None
                followup_q = str(session_ctx.get("rapport_followup_question") or "").strip()
                rapport = {"gap_row_id": None, "question": followup_q or None}
        else:
            rap_slots = discovery_slots_for_turn(
                session_ctx,
                user_message,
                routing_phase=str(session_ctx.get("routing_phase") or "listening"),
                history=history,
                has_block=bool(home_block_id or session_ctx.get("preview_block_id")),
                has_identity=bool(session_ctx.get("identity_snippet")),
                phone_verified=phone_verified,
                timer=timer,
            )
            if _rapport_should_release(user_message, session_ctx, rap_slots):
                _reset_rapport_state(session_ctx)  # fall through to normal routing (slots cached)
            else:
                followup_q = str(session_ctx.get("rapport_followup_question") or "").strip()
                rapport = {"gap_row_id": None, "question": followup_q or None}
    if isinstance(rapport, dict):
        from app.claims_persist import (
            latest_claim_id,
            try_upsert_claims_from_message,
        )
        from app.rapport_gaps import mark_answered
        from app.rapport_reply import rapport_concierge_reply

        gap_row_id = str(rapport.get("gap_row_id") or "").strip()
        question = str(rapport.get("question") or "").strip()
        claim_id: str | None = None
        saved_any = False
        saved_label: str | None = None
        saved_bucket: str | None = None
        try:
            res = try_upsert_claims_from_message(user_id, user_message, allow_rapport_gap=False)
            saved_any = res.saved > 0
            if saved_any:
                claim_id = latest_claim_id(user_id)
                saved_label = res.primary_label
                saved_bucket = res.primary_bucket
        except Exception:  # noqa: BLE001 — never fail the turn on a persist hiccup
            logging.getLogger(__name__).exception("rapport_answer_persist_failed")
        if gap_row_id:
            try:
                mark_answered(gap_row_id, answer_claim_id=claim_id)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception("rapport_answer_close_gap_failed")
        # A tile answer is NOT fresh onboarding — reply via the concierge engine, which
        # acknowledges HER actual answer and asks one grounded follow-up (with tappable
        # chips). Never the heritage-first profile-intake engine, which re-asks covered
        # threads like heritage regardless of what's already known. See rapport_reply.py.
        prior_followups = int(session_ctx.get("rapport_followup_count") or 0)
        concierge = rapport_concierge_reply(
            answer_text=user_message,
            question=question or None,
            saved_label=saved_label,
            saved_bucket=saved_bucket,
            saved=saved_any,
            prior_followups=prior_followups,
        )
        # ZERO-TAP HAND-OFF: only when she is ACCEPTING an app-move we put in front of her LAST turn
        # (rapport_offer_pending). Then the concierge returns an ACTION carrying a concrete first-person
        # query, and we re-drive THIS turn with it through the REAL pipeline so actual places / events /
        # neighbors appear NOW. A proactive app-move — on the seed turn, or any turn where no offer was
        # pending — is an OFFER, not a command: it renders as a tap-to-go chip (the `else` branch)
        # instead of surprise-running a search she never asked for. (A typed "find me X" never reaches
        # here — it releases via _rapport_should_release above and routes normally.)
        action = concierge.get("action")
        action_send = str(action.get("send") or "").strip() if isinstance(action, dict) else ""
        offer_was_pending = bool(session_ctx.get("rapport_offer_pending"))
        if action_send and offer_was_pending:
            action_kind = str(action.get("kind") or "").strip() if isinstance(action, dict) else ""
            logging.getLogger(__name__).info(
                "rapport_handoff dispatch kind=%s send=%r (accepted_msg=%r)",
                action_kind, action_send, user_message,
            )
            rapport_handoff_send = action_send
            user_message = action_send
            for _k in (
                "rapport_active", "rapport_answer", "rapport_followup_question",
                "rapport_followup_count", "rapport_reply", "rapport_offer_pending",
                "rapport_pending_action",
            ):
                session_ctx.pop(_k, None)
            # Route by the concierge's SEMANTIC decision (kind), not by re-classifying the noun:
            # force the intent to match the chosen kind and prime the slot cache the discovery
            # gates below reuse. This is what keeps "find me a playground" in the places lane
            # instead of the meet/browse lane. Falls back to AI routing if the kind isn't mapped.
            forced_slots = _forced_slots_for_kind(
                action_kind, action_send, action, session_ctx,
                home_block_id=home_block_id, phone_verified=phone_verified,
                history=history, timer=timer,
            )
            if forced_slots is not None:
                session_ctx["_discovery_slots"] = forced_slots
                session_ctx["_discovery_slots_for"] = action_send
                logging.getLogger(__name__).info(
                    "rapport_handoff forced_intent kind=%s -> goal=%s linear=%s signal=%s",
                    action_kind, forced_slots.get("goal"),
                    forced_slots.get("linear_intent"), forced_slots.get("signal_intent"),
                )
            # fall through to the normal pipeline with user_message = her concrete request
        else:
            # She is NOT accepting a pending offer, so nothing auto-runs. Reply in-thread and, when
            # the concierge proposed a next move, render it as a TAP-TO-GO chip she chooses:
            #  - a proactive app-move OFFER (action set) → one action chip; arm rapport_offer_pending
            #    so her acceptance next turn (a soft "yes" that stays) dispatches for real.
            #  - a personal follow-up QUESTION (options) → suggested one-tap answers.
            #  - a warm close (neither) → clear the capture.
            # Arming the one-turn continuation keeps her next reply routing back here, not to the
            # classifier (which would misread a bare answer as a fresh intent). A typed find/host/get
            # request still releases via _rapport_should_release above; tapping an offer chip posts a
            # topic-named request that routes for real. The count backstops runaway qualifying.
            ctx = dict(session_ctx)
            ctx.pop("rapport_answer", None)
            reply = sanitize_assistant_message(str(concierge.get("reply") or ""))
            options = concierge.get("options")
            if action_send:
                ctx["rapport_reply"] = {"options": [], "action": action}
                ctx["rapport_active"] = True
                ctx["rapport_followup_question"] = reply
                ctx["rapport_followup_count"] = prior_followups + 1
                ctx["rapport_offer_pending"] = True
                # Remember the exact action so tapping the chip dispatches it deterministically next
                # turn — no re-classify (mis-lands the lane) and no second concierge call (which can
                # hallucinate "you're already connected" instead of running the search).
                ctx["rapport_pending_action"] = action
            elif isinstance(options, list) and options:
                ctx["rapport_reply"] = {"options": options, "action": None}
                ctx["rapport_active"] = True
                ctx["rapport_followup_question"] = reply
                ctx["rapport_followup_count"] = prior_followups + 1
                ctx["rapport_offer_pending"] = False
            else:
                ctx.pop("rapport_reply", None)
                ctx.pop("rapport_active", None)
                ctx.pop("rapport_followup_question", None)
                ctx.pop("rapport_followup_count", None)
                ctx.pop("rapport_offer_pending", None)
                ctx.pop("rapport_pending_action", None)
            ctx["_orchestrator_turn"] = False
            ctx["timing_ms"] = timer.to_dict()
            ctx["last_routing"] = {
                "outcome": "rapport_answer",
                "intent_class": "identity",
                "tool_called": "extract_identity_claims",
            }
            # No heritage bucket / focus-phrase eyebrow — that was the profile engine's artifact.
            ui = {"bucket": None, "focus_phrase": None, "highlights": []}
            return reply, "continue", ctx, ui, None

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
                    user_id=user_id,
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
            if rapport_handoff_send is not None:
                logging.getLogger(__name__).info(
                    "rapport_handoff routed send=%r -> routing=%s previews=%s peers=%s",
                    rapport_handoff_send, routing,
                    bool(ctx.get("activity_previews")), len(peers or []),
                )
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
            # The host flow is driven deterministically by host_stage — not the orchestrator's
            # intent guess, which flip-flops (activity → identity turn to turn) because the router
            # enum has no "hosting" value. Stamp a stable hosting label so the transcript / inbox
            # reflect what's actually happening instead of that noise.
            turn_ctx["last_routing"] = {
                "outcome": "R",
                "intent_class": "hosting",
                "confidence": 1.0,
                "tool_to_call": None,
                "capture_fired": False,
                "event_fast_path": True,
            }
            # Conversational-aside flag: set when Lana answers a question mid-host (coordination
            # / advice) so the FE shows her TEXT + a compact draft card instead of the setup
            # carousel that would hide the reply. Default off; the carousel returns next turn.
            turn_ctx["host_aside"] = False
            # Chips + affinity are transient per-turn UI — never inherit last turn's
            # set, or the early-out keeps showing stale options for the new question.
            ed.pop("suggestions", None)
            ed.pop("affinity_prompt", None)
            ed.pop("affinity_options", None)
            # The extractor over-eagerly pulls a "title" from the entry phrase ("host a
            # meet" → "Meet"). Drop bare generic words so we still ask for a real name.
            if _is_generic_title(ed.get("title")):
                ed.pop("title", None)
            # A bare ZIP that slipped in as the venue ("I'll use 34786 as the meeting
            # spot") is an area, not a meetable place — drop it so the where-step keeps
            # asking for a real spot instead of pinning a 5-digit code to the map.
            if _is_zip_token(ed.get("venue_name")):
                ed.pop("venue_name", None)

            # Classify the host's reply BEFORE any extraction: a clear affirmation
            # ("yes that works", "looks good", "perfect") is a STATE TRANSITION on the
            # card Lana just showed — it must never be consumed by the when-resolver or
            # the host brain as an "edit" that re-renders the same card (the production
            # confirm loop: 0/16 conversational hosts ever reached a posted event).
            reply_cls = _classify_host_reply(user_message)

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

            # Skip the LLM date resolver on a pure card reply (affirm/tweak/drop carries
            # no date), and when the draft already has a start AND this message carries
            # no temporal words — otherwise it ran on EVERY host turn (incl. tapping
            # a capacity/approval/share chip), adding one LLM round-trip each time.
            if reply_cls != "other" or (wd and wt and not _has_temporal_tokens(user_message)):
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
            # Recurring cadence ("MWF 7am", "wednesdays 4pm", "every tuesday") — weekly
            # meets aren't supported yet, and the extractor used to null the whole draft
            # (or emit prose starts_at) on these. Keep everything else the message gave
            # and ground the draft on the FIRST occurrence; the reply says so below.
            recur_days = _detect_recurrence(user_message) if reply_cls == "other" else None
            if recur_days is not None:
                from app.event_when import event_local_now

                if not nd:
                    nd = _first_recurrence_date(recur_days, event_local_now().date())
                if not ntime:
                    ntime = _resolve_event_time(user_message)
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
                # First host turn after extraction. Rich opening with blockers known → drafted
                # review (P2); otherwise straight to the batched setup carousel.
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
                        "Let's set it up! Add a name, a date & time, and a place below — or "
                        "just tell me here and I'll fill them in."
                    )
            elif stage == "review":
                if reply_cls in ("affirm", "drop") and not blockers_done:
                    # The host said yes but a blocker regressed (a cleared field) — ask
                    # for exactly ONE missing thing, never the unchanged review card.
                    turn_ctx["host_stage"] = "review"
                    turn_ctx["host_aside"] = True
                    ed["suggestions"] = []
                    reply = _ask_one_missing(
                        _host_blockers_needed(_title, wd, wt, venue_resolvable)
                    )
                elif reply_cls in ("affirm", "drop"):
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
                        if reply_cls == "tweak"
                        else f"Updated **{_title}** — does this look right?"
                    )
            elif stage == "setup":
                if reply_cls in ("affirm", "drop") and blockers_done:
                    turn_ctx["host_stage"] = "confirm"
                    ed["suggestions"] = []
                    reply = (
                        f"It's all set — **{_title}**. One last look, then drop it on the block."
                    )
                elif reply_cls in ("affirm", "drop"):
                    # Affirmed but a blocker is still missing — hold in setup and ask for
                    # exactly ONE missing field (never the whole list, never a re-loop).
                    need = _host_blockers_needed(_title, wd, wt, venue_resolvable)
                    turn_ctx["host_stage"] = "setup"
                    turn_ctx["host_aside"] = True
                    ed["suggestions"] = []
                    reply = _ask_one_missing(need)
                elif _norm_cta(user_message) in ("continue setting up", "continue setup"):
                    # The FE "Continue setting up" button — a deterministic tap that brings the
                    # setup carousel back (host_aside stays off so the card shows, not an aside).
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    reply = _host_fallback_nudge(
                        _host_blockers_needed(_title, wd, wt, venue_resolvable)
                    )
                else:
                    # Free-text during setup — the host is TALKING, not tapping. Hand the whole
                    # turn to the LLM brain: it reads the message in ANY phrasing, extracts what
                    # it carries (title / place / capacity / prefs), and writes the reply — no
                    # keyword gates. Apply the extraction monotonically and show the reply as an
                    # aside (text + compact draft card) so it's visible over the carousel. The
                    # deterministic nudge is only a fallback for when the LLM is unavailable.
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    need = _host_blockers_needed(_title, wd, wt, venue_resolvable)
                    from app.host_turn import host_turn_brain

                    with timer.stage("llm_host_turn"):
                        brain = host_turn_brain(
                            history=history,
                            user_message=user_message,
                            draft=ed,
                            needed=need,
                        )
                    if brain:
                        _apply_host_brain(brain, ed, turn_ctx, session_ctx, settings, _title)
                        reply = brain["reply"]
                        turn_ctx["host_aside"] = True
                    else:
                        reply = _host_fallback_nudge(need)
            else:  # stage == "confirm" → sanity guards, then publish when the host drops it (or plainly says yes)
                # Pre-publish guards (far venue / kid quiet-hours) run BEFORE the verify
                # gate, so the clarifying question is asked up front and the post-verify
                # auto-publish (discovery_route) only ever posts a guard-cleared draft.
                # A plain affirmation counts as a drop — yes → publish, same as the button.
                _wants_drop = (
                    reply_cls in ("affirm", "drop")
                    or _is_host_drop(user_message)
                    or _is_host_confirm(user_message)
                )
                if _wants_drop and not blockers_done:
                    # Yes, but a blocker regressed — ask for exactly the ONE missing
                    # field instead of attempting a publish that would be rejected.
                    turn_ctx["host_stage"] = "confirm"
                    turn_ctx["host_aside"] = True
                    ed["suggestions"] = []
                    guard_reply, proceed_drop = (
                        _ask_one_missing(
                            _host_blockers_needed(_title, wd, wt, venue_resolvable)
                        ),
                        False,
                    )
                else:
                    guard_reply, proceed_drop = _host_publish_gate(
                        user_message=user_message,
                        ed=ed,
                        wd=wd,
                        user_id=user_id,
                        session_ctx=session_ctx,
                        turn_ctx=turn_ctx,
                        title=_title,
                        wants_drop=_wants_drop,
                    )
                if guard_reply is not None:
                    reply = guard_reply
                elif proceed_drop and not phone_verified:
                    # Guest dropping the meet: DON'T attempt create_event first — it would
                    # 403 (auto_publish_event_failed: create_event_failed:403). Gate on auth
                    # up front: ask to sign up / log in and mark the finished draft
                    # host_publish_pending so the post-verify path (discovery_route) publishes
                    # it and shows the event-created screen once the account is verified.
                    turn_ctx["host_stage"] = "confirm"
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
                elif proceed_drop:
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
                        turn_ctx["event_guard_pending"] = None
                        turn_ctx["event_guards_confirmed"] = None
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
            # Recurrence acknowledged in Lana's own words: weekly meets aren't a thing
            # yet, so the draft holds the FIRST occurrence — say so instead of silently
            # dropping the cadence (or worse, blanking the draft).
            if recur_days is not None and wd and not turn_ctx.get("event_published_now"):
                reply = (
                    "Weekly meets are coming — for now let's pick the first one: "
                    f"**{_friendly_when(wd, wt)}**. " + reply
                )
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
