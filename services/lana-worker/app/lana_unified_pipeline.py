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


def _inject_event_quick_replies(draft: dict[str, Any], question: str = "") -> None:
    """Tappable options for the current question. Reads the question Lana actually
    asked (so 'what time in the afternoon?' → afternoon clock times), falling back to
    the next missing blocker (title → when → place). Always present so chips show."""
    import re

    if draft.get("affinity_prompt"):
        return  # a deliberate affinity gate owns the chips this turn

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
        draft["suggestions"] = _TITLE_SUGGESTIONS
    # Fallback: next missing blocker (title → when → place).
    elif not has("title"):
        draft["suggestions"] = _TITLE_SUGGESTIONS
    elif not has("starts_at"):
        draft["suggestions"] = _when_suggestions()
    elif not has("venue_name"):
        draft["suggestions"] = _PLACE_SUGGESTIONS


def _event_published_reply(reply: str, draft: dict[str, Any]) -> str:
    title = str((draft or {}).get("title") or "your event").strip() or "your event"
    note = f"🎉 Done — **{title}** is live on your block. Neighbors who match can RSVP now."
    base = str(reply or "").strip()
    return f"{base}\n\n{note}" if base else note


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
            complete = _event_draft_complete(ed)
            affinity_done = bool(ed.get("cohort_tags")) or bool(
                session_ctx.get("event_affinity_asked")
            )
            if complete and not affinity_done:
                # Ask "who's it for?" once with tappable options before going live.
                ed["affinity_prompt"] = "Who’s it for — anyone with a similar kid-stage?"
                ed["affinity_options"] = ["Same kid-stage", "Any toddler mom", "Open · all moms"]
                ed["suggestions"] = []
                turn_ctx["event_affinity_asked"] = True
            elif complete:
                event_id = _auto_publish_event(user_id, user_jwt, ed)
                if event_id:
                    turn_ctx["event_id"] = event_id
                    turn_ctx["event_published_now"] = True
                    turn_ctx["event_affinity_asked"] = False
                    reply = _event_published_reply(reply, ed)
            else:
                _inject_event_quick_replies(ed, reply)
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
