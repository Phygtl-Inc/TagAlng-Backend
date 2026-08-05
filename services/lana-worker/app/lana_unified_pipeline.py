"""Unified Lana: discovery gates first (code), orchestrator for everything else (AI)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.discovery_route import handle_discovery_turn, looks_like_logout
from app.lana_dispatch import lana_unified_turn
from app.lana_ui import sanitize_assistant_message
from app.lana_paths import decide_turn_mode, unified_rules_first_enabled
from app.loop_guard import discovery_reply_is_stuck, reset_sticky_discovery_state
from app.orchestrator.pipeline import run_turn
from app.orchestrator.progress import READING
from app.reply_compose import compose_reply
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
    "has_time",
    "ends_at",
    "duration_minutes",
    "max_attendees",
    "auto_approve",
    "allow_attendee_share",
    "bring_items",
    "cover_emoji",
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
    # The concierge's structured topic ("badminton") is the committed subject of the
    # offer — authoritative over whatever the query parse mined from the send text.
    topic = str(action.get("topic") or "").strip() if isinstance(action, dict) else ""
    if topic:
        slots["signal_detail"] = topic
    elif not str(slots.get("signal_detail") or "").strip():
        slots["signal_detail"] = None
    # Stamp the dispatch so downstream lanes can trust the structured topic over the
    # model-authored send text (which can come out generic, e.g. "what's happening this
    # weekend" for a badminton offer — the tap must still search badminton).
    slots["_forced_kind"] = str(kind or "").strip()
    return slots


def _wire_ask_gap_action(action: Any) -> None:
    """Hold a policy `ask_gap` to the question that was actually vetted.

    Rapport questions are written by app/rapport_synth.py under a real quality
    bar — no yes/no, no opinions or origin stories, no consumer/brand
    preferences, and one test each must pass: "would the answer change who they
    connect with?". The home tile serves those strings verbatim. The chat path
    got the same question only as a *topic hint* in candidate_goals and then
    wrote its own sentence, so none of those bars applied to what the user read
    — which is how "is there a favorite blue thing that cheers you up?" shipped
    (QA 2026-08-03). A colour is not something a neighbour can share.

    So: the model still writes the warm lead-in, and the QUESTION comes from the
    vetted row. Same division of labour as _wire_ground_place_action below,
    where the model writes the ask and real map data fills the chips.

    An `ask_gap` whose goal_id resolves to no open gap is a question nothing
    vetted — downgraded to `reply`, keeping whatever warmth it opened with.

    Also stamps chat_asked_at, so candidate_goals stops offering this gap and it
    cannot be re-asked next turn.
    """
    if getattr(action, "kind", None) != "ask_gap":
        return
    gid = str(getattr(action, "goal_id", None) or "")
    row = None
    if gid.startswith("gap:"):
        from app.rapport_gaps import get_gap_row

        row = get_gap_row(gid.split(":", 1)[1].strip())
    question = str((row or {}).get("question") or "").strip()
    if not question:
        logging.getLogger(__name__).info("ask_gap_unvetted goal_id=%r -> reply", gid or None)
        action.kind = "reply"
        action.goal_id = None
        action.chips = []
        return
    action.utterance = _merge_vetted_question(str(action.utterance or ""), question)
    from app.rapport_gaps import mark_chat_asked

    mark_chat_asked(str(row.get("gap_row_id") or ""))


def _merge_vetted_question(utterance: str, question: str) -> str:
    """Keep the model's acknowledgement, end on the vetted question.

    The lead-in is the part worth keeping — it's what makes the ask feel like a
    reply rather than a form field. Everything from the model's own question
    mark onward is dropped, because that sentence is the unvetted one.
    """
    said = str(utterance or "").strip()
    if not said:
        return question
    if question.lower() in said.lower():
        return said  # already asking exactly the vetted question
    head = said
    for mark in ("?", "؟"):
        if mark in head:
            head = head.split(mark, 1)[0]
            # Drop the truncated interrogative clause, keep the sentences before it.
            parts = re.split(r"(?<=[.!…])\s+", head.strip())
            head = " ".join(parts[:-1]) if len(parts) > 1 else ""
            break
    head = head.strip()
    return f"{head} {question}".strip() if head else question


def _wire_ground_place_action(action: Any, *, user_id: str, session_ctx: dict[str, Any]) -> None:
    """Connect a policy `ground_place` decision to the REAL grounding rails.

    The policy LLM authors the question but its chips are fiction — it has no
    Places tool, so a tap like "It's the main rec center" used to land as plain
    text with nothing armed, and the turn after dead-ended (QA 2026-07-30, the
    squash case). Here we resolve the affiliation the ask is about, run the same
    Places search the tile flow uses, replace the invented chips with real
    candidates, and arm rapport_grounding — so the next turn's tap or free text
    flows into handle_grounding_confirmation → ground_and_confirm, which ends on
    the bridge offer (intro when co-members exist, create+invite otherwise).

    No resolvable affiliation (capture hasn't landed yet, or the goal is
    ambiguous) → the chips are still stripped (never ship invented places) and
    the question goes out free-text; the ungrounded-circle goal resurfaces once
    capture lands. Empty Places results still arm the pending state with zero
    candidates — the user's ANSWER then drives the search (the existing
    re-search path in handle_grounding_confirmation)."""
    if getattr(action, "kind", None) != "ground_place":
        return
    action.chips = []
    try:
        from app.auth import service_client
        from app.circles_flow import _chip, _home_block_id, _with_escape, ground_options

        key = ""
        gid = str(getattr(action, "goal_id", None) or "")
        if gid.startswith("circle:"):
            key = gid.split(":", 1)[1].strip()
        q = (
            service_client()
            .table("circle_affiliations")
            .select("id, circle_type, circle_key, detail, status, place_ref, place_name")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .is_("place_ref", "null")
        )
        if key:
            q = q.eq("circle_key", key)
        rows = [
            r for r in (q.order("created_at", desc=True).limit(2).execute().data or [])
            if isinstance(r, dict)
        ]
        if not rows or (not key and len(rows) > 1):
            return  # not captured yet / ambiguous — leave a free-text ask
        aff = rows[0]
        options = ground_options(
            user_id, aff, block_id=_home_block_id(user_id), query=None
        )
        candidates = [{**_chip(o), "name": o.get("name")} for o in options]
        # Escape hatch rides along as a chip but never as a candidate — a wrong
        # list must always have a way out (2026-08-03).
        chips = [
            {"label": c["label"], "send": c["send"]} for c in _with_escape(candidates)
        ]
        session_ctx["rapport_active"] = True
        session_ctx["rapport_grounding"] = {
            "affiliation_id": str(aff.get("id") or ""),
            "candidates": candidates,
            "answer_text": "",
            "attempts": 1,
            # The action the user already asked for that this grounding serves
            # (policy-stamped, e.g. host_meet) — the confirmed place then
            # dispatches it directly instead of re-offering it.
            "pending_action": getattr(action, "pending_action", None),
        }
        session_ctx["rapport_followup_question"] = str(
            getattr(action, "utterance", "") or ""
        )
        session_ctx["rapport_offer_pending"] = False
        session_ctx["rapport_pending_action"] = None
        action.chips = chips
        logging.getLogger(__name__).info(
            "ground_place_wired aff=%s key=%s candidates=%d pending_action=%s",
            aff.get("id"), aff.get("circle_key"), len(candidates),
            getattr(action, "pending_action", None),
        )
    except Exception:  # noqa: BLE001 — wiring is an upgrade; the ask still goes out
        logging.getLogger(__name__).exception("ground_place_wire_failed")


def _reset_rapport_state(session_ctx: dict[str, Any]) -> None:
    """Drop the concierge follow-up capture so the turn falls through to normal routing. Set to
    None (not popped) so the {**old, **new} session merge clears them instead of keeping a stale
    value across the round-trip."""
    for k in (
        "rapport_active", "rapport_answer",
        "rapport_followup_question", "rapport_followup_count", "rapport_reply",
        "rapport_offer_pending", "rapport_pending_action", "rapport_grounding",
    ):
        session_ctx[k] = None


def _turn_is_tip_ask(
    ctx: dict[str, Any],
    msg: str,
    *,
    history: list[dict[str, Any]] | None,
    home_block_id: str | None,
    phone_verified: bool,
    timer: TurnTimer | None = None,
) -> bool:
    """Is this turn a place/service recommendation ask (looking.tip)?

    Used to keep decide_turn off recommendation turns. Reads the classifier's verdict,
    which is cached per user message — the same parse handle_discovery_turn reuses, so this
    costs no extra model call. Fails CLOSED (False) so a classifier hiccup leaves the policy
    gate exactly as it was rather than diverting every turn to the engines.
    """
    try:
        from app.discovery_slots import discovery_slots_for_turn
        from app.layer1_intents import SIGNAL_INTENT_BY_LINEAR

        slots = discovery_slots_for_turn(
            ctx,
            msg,
            routing_phase=str(ctx.get("routing_phase") or "listening"),
            history=history,
            has_block=bool(home_block_id or ctx.get("preview_block_id")),
            has_identity=bool(ctx.get("identity_snippet")),
            phone_verified=phone_verified,
            timer=timer,
        )
        if not isinstance(slots, dict):
            return False
        linear = str(slots.get("linear_intent") or "")
        sig = str(slots.get("signal_intent") or "") or SIGNAL_INTENT_BY_LINEAR.get(linear, "")
        return sig == "tip_seek" and float(slots.get("confidence") or 0.0) >= 0.5
    except Exception:  # noqa: BLE001 — a gate helper must never break the turn
        logging.getLogger(__name__).exception("tip_ask_policy_gate_failed")
        return False


def _policy_rapport_reply(
    *,
    user_id: str,
    session_id: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
    timer: Any,
    rapport_question: str | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], Any] | None:
    """Divert a rapport-thread answer's REPLY to the unified policy (decide_turn).

    The rapport branch's bookkeeping (claim saved, gap closed) has already run by
    the time this is called — the policy only authors what Lana says back, so one
    voice (acknowledge → bridge → offer) owns every conversational turn instead of
    the mini-model concierge. Guests included on purpose: a rapport answer is
    exactly the conversational turn the policy exists for, and its chips route
    through the normal pipeline next turn. Returns None — caller falls through to
    the concierge fallback — when the policy is off, hands off, or fails.

    QA 2026-07-29: the concierge answered these turns with the bridge policy
    bypassed entirely — "English is my language of choice" got a hallucinated
    "explore language learning" close instead of an app-move.
    """
    if decide_turn_mode() != "on":
        return None
    if session_ctx.get("event_host_active") or session_ctx.get("pending_confirmation"):
        return None
    from app.policy.decide import (
        apply_defer, ask_streak, audit_decision, decide_turn, note_ask_streak,
    )

    # The message being answered IS a rapport ask (the tile/thread question),
    # which the streak stamp never saw — count it so the policy knows it's
    # already one personal question deep before asking another.
    session_ctx["policy_ask_streak"] = max(ask_streak(session_ctx), 1)
    with timer.stage("decide_turn"):
        action = decide_turn(
            user_id=user_id, session_ctx=session_ctx,
            history=history, user_message=user_message,
            # The tile question lives on the home screen, not in chat history —
            # without it the policy can't tell which ask this message answers.
            answering_question=rapport_question,
        )
    if action is None or action.kind == "handoff":
        return None
    # Before the bookkeeping below: this can downgrade ask_gap -> reply, and a
    # downgraded turn must not count against the ask streak or park a goal.
    _wire_ask_gap_action(action)
    apply_defer(session_ctx, action)
    note_ask_streak(session_ctx, action)
    audit_decision(
        session_id=session_id, user_id=user_id,
        user_message=user_message, action=action, shadow=False,
    )
    # The policy owns the thread from here — clear the rapport capture
    # (None, never popped) so the merge can't resurrect it next turn.
    _reset_rapport_state(session_ctx)
    # A ground_place ask must be backed by real place candidates + armed state,
    # or the answer turn dead-ends (re-arms AFTER the reset above, on purpose).
    _wire_ground_place_action(action, user_id=user_id, session_ctx=session_ctx)
    session_ctx["policy_chips"] = action.chips or None
    session_ctx["policy_chip_msgs"] = [c["send"] for c in action.chips] or None
    session_ctx["last_routing"] = action.routing_dict()
    session_ctx["_orchestrator_turn"] = False
    session_ctx["timing_ms"] = timer.to_dict()
    reply = sanitize_assistant_message(action.utterance)
    ui = {"bucket": None, "focus_phrase": None, "highlights": []}
    return reply, "continue", session_ctx, ui, session_ctx.get("event_draft")


def _grounding_turn_result(
    session_ctx: dict[str, Any], result: dict[str, Any], timer: Any
) -> tuple[str, str, dict[str, Any], dict[str, Any], None]:
    """Package a circles place-grounding turn (circles_flow.handle_grounding_answer /
    _confirmation) as the rapport block's return. Chips pending keeps the capture armed
    so the next reply routes back here; a closed thread clears every rapport key with
    None (never popped — the session merge resurrects popped keys)."""
    ctx = dict(session_ctx)
    ctx["rapport_answer"] = None
    reply = sanitize_assistant_message(str(result.get("reply") or ""))
    pending = result.get("pending")
    options = result.get("options") or []
    offer = result.get("offer")
    if isinstance(pending, dict):
        ctx["rapport_active"] = True
        ctx["rapport_grounding"] = pending
        ctx["rapport_reply"] = {"options": options, "action": None}
        ctx["rapport_followup_question"] = reply
        ctx["rapport_offer_pending"] = False
        ctx["rapport_pending_action"] = None
    elif isinstance(offer, dict) and str(offer.get("send") or "").strip():
        # Grounding closed WITH a bridge offer (rapport-bridge shape): arm the
        # existing offer rails so a chip tap or a typed "sure" dispatches the
        # stored action deterministically, and a decline closes warmly — the
        # same accept/decline/pivot branch every concierge offer already uses.
        _reset_rapport_state(ctx)
        ctx["rapport_active"] = True
        ctx["rapport_offer_pending"] = True
        ctx["rapport_pending_action"] = {
            "kind": str(offer.get("kind") or ""),
            "label": str(offer.get("label") or ""),
            "send": str(offer.get("send") or ""),
            "topic": str(offer.get("topic") or ""),
        }
        ctx["rapport_reply"] = {
            "options": [],
            "action": {"label": offer.get("label"), "send": offer.get("send")},
        }
    else:
        _reset_rapport_state(ctx)
    ctx["_orchestrator_turn"] = False
    ctx["timing_ms"] = timer.to_dict()
    ctx["last_routing"] = {
        "outcome": "circle_grounding",
        "intent_class": "identity",
        "tool_called": "ground_circle_affiliation" if result.get("grounded") else None,
    }
    ui = {"bucket": None, "focus_phrase": None, "highlights": []}
    return reply, "continue", ctx, ui, None


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


def _recover_when_from_draft(ed: dict[str, Any], wd: Any, wt: Any) -> tuple[Any, Any]:
    """Back-fill missing when-keys from the draft's persisted starts_at.

    The date is always safe to recover. The clock is recovered ONLY when the draft
    says the host really gave one (has_time, stamped where starts_at is built): a
    date-only draft carries a midnight placeholder, and the extractor fabricates a
    time when the host said none — recovering either would launder it into
    event_when_time, satisfy the confirm gate, and publish a "12 AM" meet (#56).
    Drafts from before the flag existed have no key: trust a non-midnight clock
    (a host-given time mid-flow at deploy), never a midnight one."""
    if (wd and wt) or not ed.get("starts_at"):
        return wd, wt
    from datetime import datetime as _dt_recover

    try:
        _existing = _dt_recover.fromisoformat(str(ed["starts_at"]))
    except (ValueError, TypeError):
        return wd, wt
    if not wd:
        wd = _existing.date().isoformat()
    _ht = ed.get("has_time")
    if not wt and (_ht is True or (_ht is None and _existing.strftime("%H:%M") != "00:00")):
        wt = _existing.strftime("%H:%M")
    return wd, wt


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
        return compose_reply(
            goal=(
                "The host's event is fully drafted but posting was rejected because "
                "their account isn't verified yet. Reassure them the event is ready "
                "and ask them to verify their email so you can publish it."
            ),
            facts=[f"The event: {name}"],
            fallback=(
                f"{name} is all set, but I can't post it until your account is verified. "
                "Verify your email and I'll publish it right away."
            ),
        )
    if "location" in detail or "venue" in detail:
        return compose_reply(
            goal=(
                "The host's event couldn't post because its spot can't be placed on "
                "the map. Ask them to pick a place or share an address so you can "
                "post it."
            ),
            facts=[f"The event: {name}"],
            fallback=(
                f"I have everything for {name} except a spot I can place on the map. "
                "Pick a place or share an address and I'll post it."
            ),
        )
    return compose_reply(
        goal=(
            "Posting the host's event just failed for a temporary reason. Own the "
            "hiccup honestly and ask them to try again in a moment — never fake "
            "success."
        ),
        facts=[f"The event: {name}"],
        fallback=(
            f"I hit a snag posting {name} just now — give it another try in a moment "
            "and I'll get it up."
        ),
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
_PLACE_SUGGESTIONS = ["The playground", "The park", "My place", "Somewhere nearby"]
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
    # The chip label was lexicon-scrubbed to "Somewhere nearby" (chips post their label
    # back) — accept it alongside the old phrasing, never instead of it.
    "somewhere nearby", "nearby",
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
    if not ed.get("cover_emoji") and cfg.get("cover_emoji"):
        ed["cover_emoji"] = cfg["cover_emoji"]
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
            # Default to the first name; keep the full list so the review card can offer
            # the alternates as tap-to-rename chips ("or call it …").
            ed["title"] = titles[0]
            ed["title_suggestions"] = titles[:3]
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
        return "That's everything — tap **Looks good** and I'll post it for your neighbors."
    return f"Just need {' · '.join(need)} — tell me and I'll add it, or fill it in below."


def _apply_host_brain(
    brain: dict[str, Any],
    ed: dict[str, Any],
    turn_ctx: dict[str, Any],
    session_ctx: dict[str, Any],
    settings: dict[str, Any],
) -> None:
    """Apply the LLM host-turn brain's extraction to the draft. Understanding is the LLM's job;
    this just records it — last write wins, so a correction ("don't call it that — call it X",
    "actually let's do the park") replaces the old value instead of being silently dropped.
    The brain only emits a field its LATEST message stated or changed (null otherwise), so an
    unrelated turn can't clobber a value the host already gave.

    redo = slots the host asked to change WITHOUT giving the new value ("let's pick a
    different time") — clear the slot and its step flags so the flow re-collects it.
    Cleared first, so a value the same message DID carry still lands afterwards."""
    for slot in brain.get("redo") or []:
        if slot == "title":
            ed.pop("title", None)
        elif slot == "when":
            ed.pop("starts_at", None)
            ed.pop("has_time", None)
            ed.pop("ends_at", None)
            turn_ctx["event_when_date"] = None
            turn_ctx["event_when_time"] = None
        elif slot == "place":
            for k in ("venue_name", "place_id", "venue_lat", "venue_lng", "venue_address"):
                ed.pop(k, None)
            turn_ctx["event_place_asked"] = False
            turn_ctx["event_venue"] = None
            turn_ctx["event_venue_tried"] = None
            session_ctx["event_venue"] = None
    title = str(brain.get("title") or "").strip()
    # Reject a generic name in both raw ("meetup") and article-led ("a meetup") form.
    title_generic = _is_generic_title(title) or _is_generic_title(
        re.sub(r"^(?:a|an|the)\s+", "", title, flags=re.I)
    )
    if title and not title_generic:
        ed["title"] = title[:80]
    place = str(brain.get("place") or "").strip()
    prev_place = str(ed.get("venue_name") or "").strip()
    if place and place.lower() != prev_place.lower():
        if prev_place:
            # The place CHANGED — the old venue's pin (place_id/lat/lng/address) and the
            # event_venue stash are stale now. Drop them, or the re-stamp at the top of the
            # next host turn puts the old spot back and publish pins the wrong coordinates.
            # The auto-resolve step re-pins the new name on the next turn.
            for k in ("place_id", "venue_lat", "venue_lng", "venue_address"):
                ed.pop(k, None)
            turn_ctx["event_venue"] = None
            session_ctx["event_venue"] = None
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
    note = f"🎉 Done — **{title}** is live in your area. Neighbors who match can RSVP now."
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
    timer.emit_seed(READING)
    session_ctx = {
        **session_ctx,
        "phone_verified": phone_verified,
        "unified_mode": True,
    }
    # Language mirroring (QA 2026-07-08: Brazilian moms got English). Detect once per
    # turn BEFORE any gate can answer — the sticky lanes and discovery return early, so
    # this is the single choke point where session_ctx["lang"] gets set. Sticky: a
    # confident es/pt message persists the language; only an explicit "in english
    # please" (or "en español"/"em português") flips it; ambiguous turns keep it.
    from app.i18n import resolve_session_lang

    # Chip-tap language pin, part 2: when the incoming message is EXACTLY one of the chip
    # payloads offered last turn, it's app-authored canonical English — not the user
    # switching language. Pin the session language for this turn: skip the heuristic here
    # and have apply_ai_lang ignore the classifier's verdict (the flag is re-derived every
    # turn, so a genuinely typed next message unpins immediately).
    _offered = session_ctx.get("_offered_chip_msgs") or []
    session_ctx["_lang_pinned_turn"] = bool(
        str(user_message or "").strip() and str(user_message or "").strip() in _offered
    )
    if not session_ctx["_lang_pinned_turn"]:
        resolve_session_lang(session_ctx, user_message)
    # Set when a rapport concierge turn hands off (zero-tap dispatch) so we can log where the
    # re-driven request actually routed — diagnostic for "yes → wrong lane" mis-routes.
    rapport_handoff_send: str | None = None

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
            # Rapport concierge capture releases on logout too — same universal exit, one path.
            "rapport_active", "rapport_answer", "rapport_followup_question",
            "rapport_followup_count", "rapport_reply", "rapport_offer_pending",
            "rapport_pending_action", "rapport_grounding",
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

    # Overlap the turn's DB context load with the classifier LLM calls below: both
    # load_user_context and the memory prefetch depend only on user_id + the raw
    # message, never on a classification, so by the time run_turn joins the future
    # the ~5s of Supabase round-trips have already happened under the classifier's
    # wall-clock. Skipped while a sticky capture owns the session — those lanes
    # answer without the orchestrator (and on a release, run_turn simply falls back
    # to loading inline). Spawned AFTER the verified-block guarantee above so a
    # just-assigned home block is visible to the background read.
    ctx_prefetch = None
    if use_orchestrator and not isinstance(session_ctx.get("rapport_answer"), dict) and not any(
        session_ctx.get(k)
        for k in (
            "pass_along_active",
            "tip_share_active",
            "look_meet_active",
            "activity_browse_active",
            "rapport_active",
        )
    ):
        from app.orchestrator.pipeline import start_ctx_prefetch

        try:
            ctx_prefetch = start_ctx_prefetch(user_id=user_id, user_message=user_message)
        except Exception:  # noqa: BLE001 — overlap is an optimization, never a blocker
            logging.getLogger(__name__).exception("ctx_prefetch_spawn_failed")
            ctx_prefetch = None

    # A "By the way…" tile answer owns the turn: save the claim, close the gap, and reply via
    # the concierge engine (acknowledge her answer + one grounded follow-up), NOT the classifier
    # — a bare answer ("I love trying new restaurants") would be misread as a fresh chat intent.
    rapport = session_ctx.get("rapport_answer")
    # A reply to the concierge's own follow-up is a rapport ANSWER — but it must NOT be a separate
    # sticky path that re-implements logout / pivots / safety. So it's a capture the SAME classifier
    # owns (active_capture=rapport): re-classify the turn and RELEASE to normal routing on any pivot,
    # abandon, or unsafe (logout is caught above). Only a genuine getting-to-know-you answer
    # ("I usually run alone", "idk", "yes") stays and gets a concierge reply. The seed turn (first
    # tile answer, rapport_answer set) never re-classifies — it always seeds the flow. (One
    # exception: a place-GROUNDING seed classifies unmatched free text once, below — its answer
    # feeds a Places search, so a decline/pivot must be caught before it becomes a query.)
    if not isinstance(rapport, dict) and session_ctx.get("rapport_active"):
        from app.discovery_slots import discovery_slots_for_turn

        grounding = session_ctx.get("rapport_grounding")
        if isinstance(grounding, dict):
            # Place-grounding chips are pending ("is that OrangeTheory on Narcoossee?").
            # A chip tap / named candidate confirms deterministically — never re-classified
            # (same principle as the offer-chip dispatch below). Anything else classifies
            # once: abandon → close warmly keeping their words; a confident pivot to a
            # real request → release to normal routing; else one more search with their text.
            from app.circles_flow import (
                handle_grounding_confirmation,
                match_grounding_candidate,
            )

            matched = match_grounding_candidate(grounding.get("candidates"), user_message)
            abandon = False
            release = False
            if not matched:
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
                abandon = bool(isinstance(rap_slots, dict) and rap_slots.get("abandon"))
                release = not abandon and _rapport_should_release(
                    user_message, session_ctx, rap_slots
                )
            if release:
                _reset_rapport_state(session_ctx)  # normal routing owns the turn (slots cached)
            else:
                result = handle_grounding_confirmation(
                    user_id,
                    grounding,
                    user_message,
                    session_ctx=session_ctx,
                    abandon=abandon,
                )
                auto_offer = (
                    result.get("offer") if isinstance(result.get("offer"), dict) else None
                )
                auto_send = str((auto_offer or {}).get("send") or "").strip()
                if auto_offer is not None and auto_offer.get("auto") and auto_send:
                    # The grounding served an action the user ALREADY asked for
                    # (policy stamped pending_action on the ground_place turn) —
                    # never re-offer their own request: dispatch it now with the
                    # place pre-filled, exactly like an offer accept, and carry
                    # the community-save announcement as a preamble to the
                    # engine's reply (one bubble, no extra confirm loop).
                    # Not gated on `grounded`: when the place could NOT be pinned
                    # the request still stands, and it dispatches place-less
                    # rather than dying with the grounding (2026-08-03).
                    forced_slots = _forced_slots_for_kind(
                        str(auto_offer.get("kind") or ""), auto_send, auto_offer,
                        session_ctx,
                        home_block_id=home_block_id, phone_verified=phone_verified,
                        history=history, timer=timer,
                    )
                    if forced_slots is not None:
                        session_ctx["_discovery_slots"] = forced_slots
                        session_ctx["_discovery_slots_for"] = auto_send
                    logging.getLogger(__name__).info(
                        "grounding_auto_dispatch kind=%s send=%r forced=%s",
                        auto_offer.get("kind"), auto_send, forced_slots is not None,
                    )
                    _reset_rapport_state(session_ctx)
                    session_ctx["_turn_preamble"] = (
                        str(result.get("reply") or "").strip() or None
                    )
                    rapport_handoff_send = auto_send
                    user_message = auto_send
                    rapport = None  # fall through to the discovery gates below
                else:
                    return _grounding_turn_result(session_ctx, result, timer)
        elif session_ctx.get("rapport_offer_pending"):
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
    # Circles: a place-grounding question ("which spot is it?") owns its seed turn — the
    # answer is a place NAME to resolve against the map, not an identity fact for the claims
    # extractor. Handled BEFORE the concierge block so a pivot can fall through to normal
    # routing. A tile-chip tap grounds deterministically (never re-classified); any other
    # text classifies ONCE with the tile question as context — the same three arms as the
    # pending-chips turn above: abandon ("none of these") closes warmly, a confident pivot
    # to a real request ("show me events") releases with the gap left open to re-ask later,
    # and only a genuine place answer drives the Places search.
    if isinstance(rapport, dict) and str(rapport.get("gap_row_id") or "").strip():
        grounding_gap_id = str(rapport.get("gap_row_id")).strip()
        grounding_gap = None
        try:
            from app.rapport_gaps import get_gap_row

            candidate_row = get_gap_row(grounding_gap_id)
            if candidate_row and candidate_row.get("affiliation_ref"):
                grounding_gap = candidate_row
        except Exception:  # noqa: BLE001 — fall back to the normal concierge path
            logging.getLogger(__name__).exception("rapport_grounding_lookup_failed")
        if grounding_gap:
            from app.circles_flow import (
                handle_grounding_answer,
                match_grounding_candidate,
            )
            from app.discovery_slots import discovery_slots_for_turn

            stored_opts = grounding_gap.get("grounding_options")
            tapped = match_grounding_candidate(
                stored_opts if isinstance(stored_opts, list) else None, user_message
            )
            abandon = False
            release = False
            if not tapped:
                # The seed turn has no rapport_* ctx yet — stamp the tile's ask (plus
                # the spots its chips offered) as the pending question so the
                # classifier doesn't judge "none of these" context-blind. Every exit
                # (_grounding_turn_result / the reset below) normalizes these keys.
                tile_q = str(rapport.get("question") or "").strip()
                if tile_q:
                    opt_names = ", ".join(
                        str(o.get("name") or "").strip()
                        for o in (stored_opts if isinstance(stored_opts, list) else [])
                        if isinstance(o, dict) and str(o.get("name") or "").strip()
                    )
                    session_ctx["rapport_active"] = True
                    session_ctx["rapport_followup_question"] = (
                        f"{tile_q} (offered place options: {opt_names})"
                        if opt_names else tile_q
                    )
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
                abandon = bool(isinstance(rap_slots, dict) and rap_slots.get("abandon"))
                release = not abandon and _rapport_should_release(
                    user_message, session_ctx, rap_slots
                )
            if release:
                # Their real request drives normal routing (slots cached). The gap
                # stays OPEN — they ignored the ask rather than engaging it, so the
                # tile may try again later.
                _reset_rapport_state(session_ctx)
                rapport = None
            else:
                from app.rapport_gaps import mark_answered

                try:
                    mark_answered(grounding_gap_id)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception("rapport_grounding_close_failed")
                result = handle_grounding_answer(
                    user_id,
                    grounding_gap,
                    user_message,
                    session_ctx=session_ctx,
                    abandon=abandon,
                )
                return _grounding_turn_result(session_ctx, result, timer)

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
        # ONE VOICE owns the reply: with the unified policy live, the answer to a
        # rapport-thread turn comes from decide_turn (acknowledge → bridge → offer) —
        # the bookkeeping above (claim saved, gap closed) already happened, so the
        # policy only has to talk. The concierge below stays as the fallback voice
        # (policy off, handoff, or any failure) so this turn can never go silent.
        policy_turn = _policy_rapport_reply(
            user_id=user_id, session_id=session_id, session_ctx=session_ctx,
            history=history, user_message=user_message, timer=timer,
            rapport_question=question or None,
        )
        if policy_turn is not None:
            return policy_turn
        # A tile answer is NOT fresh onboarding — reply via the concierge engine, which
        # acknowledges HER actual answer and asks one grounded follow-up (with tappable
        # chips). Never the heritage-first profile-intake engine, which re-asks covered
        # threads like heritage regardless of what's already known. See rapport_reply.py.
        prior_followups = int(session_ctx.get("rapport_followup_count") or 0)
        # Tell the concierge which language Lana currently speaks with her, so an answer
        # naming another language can become a switch offer (the accept chip routes through
        # the classifier's set_preferred_lang like any explicit request — no new machinery).
        current_lang_name: str | None = None
        try:
            from app.i18n import lang_display_name
            from app.lang_pref import get_user_preferred_language

            current_lang_name = lang_display_name(get_user_preferred_language(user_id))
        except Exception:  # noqa: BLE001 — a missed offer beats a failed reply
            logging.getLogger(__name__).exception("rapport_lang_context_failed")
        concierge = rapport_concierge_reply(
            answer_text=user_message,
            question=question or None,
            saved_label=saved_label,
            saved_bucket=saved_bucket,
            saved=saved_any,
            prior_followups=prior_followups,
            current_lang_name=current_lang_name,
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
            _reset_rapport_state(session_ctx)
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
            # A concierge turn supersedes the seed answer and any pending grounding chips
            # (e.g. she left them hanging and answered a NEW tile question) — cleared with
            # None, never popped, or the session merge would resurrect them and hijack the
            # next reply.
            ctx["rapport_answer"] = None
            ctx["rapport_grounding"] = None
            reply = sanitize_assistant_message(str(concierge.get("reply") or ""))
            options = concierge.get("options")
            # A language-switch offer arms the classifier's lang_pref_offer context for the
            # next few turns, so a free-typed accept ("lets talk in urdu") actually persists
            # the default via set_preferred_lang — not just a conversational promise. TTL'd
            # in language_preference_post_turn so it never becomes a sticky lane.
            lang_offer = concierge.get("language_offer") or []
            if lang_offer:
                ctx["lang_offer_langs"] = lang_offer
                # Decremented every turn by language_preference_post_turn, INCLUDING this
                # offer turn — 4 leaves three real user turns to land the accept.
                ctx["lang_offer_ttl"] = 4
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
                # A warm close ends the capture — cleared with None via the shared reset,
                # never popped: a popped key resurrects from the stored ctx on merge, which
                # kept the previous turn's chips (e.g. a language offer's accept/keep pair)
                # rendering under the close AND the capture armed forever.
                _reset_rapport_state(ctx)
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
                    slots=browse_slots,
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

    def _utterance_is_unsafe_backstop(msg: str) -> bool:
        try:
            from app.orchestrator.guardrails import utterance_is_unsafe

            # utterance_is_unsafe returns (matched, kind) — returning the raw
            # tuple here made this ALWAYS truthy ((False, None) is truthy), so
            # the policy gate silently skipped decide_turn on EVERY typed chat
            # turn and legacy paths answered instead (found 2026-07-30, the
            # bridge QA: none of the policy behavior ever showed in chat).
            matched, _kind = utterance_is_unsafe(msg)
            return bool(matched)
        except Exception:  # noqa: BLE001 — a broken import must fail SAFE (skip policy)
            return True

    # ── Unified conversational policy (decide_turn, engineering doc §C.1) ──────
    # LANA_DECIDE_TURN: off (default) | shadow | on. Shadow logs the policy's
    # would-be decision to lana_audit_log on a daemon thread while the legacy
    # path answers — the cutover diff. On answers conversational turns directly;
    # `handoff` and every failure fall through to the legacy engines below, so
    # action flows (search, host, auth, signals) keep their proven paths.
    _decide_mode = decide_turn_mode()
    if _decide_mode == "shadow":
        try:
            from app.policy.decide import run_shadow

            run_shadow(
                user_id=user_id, session_id=session_id,
                session_ctx=session_ctx, history=history, user_message=user_message,
            )
        except Exception:  # noqa: BLE001 — shadow is observability, never a blocker
            logging.getLogger(__name__).exception("decide_shadow_spawn_failed")
    elif (
        _decide_mode == "on"
        and phone_verified
        and str(session_ctx.get("routing_phase") or "") in ("", "listening")
        and not session_ctx.get("event_host_active")
        and not session_ctx.get("pending_confirmation")
        # A concrete engine offer is armed and this reply answers it: "want me to ask your
        # neighbors too?" / "want me to take that posting down?". Those accepts and declines
        # WRITE (or withdraw) a posting, so they belong to the engine that armed them — a
        # policy reply here would answer in prose and silently drop the action, leaving the
        # offer armed for a later, unrelated turn.
        and not session_ctx.get("tip_ask_offer_pending")
        and not session_ctx.get("posting_manage_pending")
        # Regex unsafe backstop stays ahead of the policy — safety turns belong
        # to the legacy rails (the policy prompt also hands them off, belt+braces).
        and not _utterance_is_unsafe_backstop(user_message)
        # A tapped LEGACY chip is an engine command ("Widen the search") — never
        # the policy's turn. Taps on the policy's own chips stay with the policy.
        and not (
            str(user_message or "").strip() in (session_ctx.get("_offered_chip_msgs") or [])
            and str(user_message or "").strip()
            not in (session_ctx.get("policy_chip_msgs") or [])
        )
        # A dispatched offer/grounding action carries forced slots for exactly
        # this message — a committed engine command, never the policy's turn.
        and not (
            isinstance(session_ctx.get("_discovery_slots"), dict)
            and session_ctx["_discovery_slots"].get("_forced_kind")
            and str(session_ctx.get("_discovery_slots_for") or "")
            == str(user_message or "").strip()
        )
        # A recommendation ask ("recommend me a doctor nearby") is an ACTION turn: it runs
        # a real neighbor-tip + Places lookup and can post the ask to neighbors. The policy
        # prompt is told to hand those off, but on QA 2026-08-04 it answered one with
        # "I'll keep an ear out and let you know if a neighbor recommends a doctor" — a
        # listening promise nothing had armed, with no places and no offer, while the
        # engine (handler=None in the turn log) never ran. Skip deterministically on the
        # classifier's own verdict rather than trusting the prompt.
        and not _turn_is_tip_ask(
            session_ctx, user_message,
            history=history, home_block_id=home_block_id,
            phone_verified=phone_verified, timer=timer,
        )
    ):
        from app.policy.decide import apply_defer, audit_decision, decide_turn, note_ask_streak

        with timer.stage("decide_turn"):
            _action = decide_turn(
                user_id=user_id, session_ctx=session_ctx,
                history=history, user_message=user_message,
            )
        if _action is not None and _action.kind != "handoff":
            # Ahead of the bookkeeping: may downgrade ask_gap -> reply.
            _wire_ask_gap_action(_action)
            apply_defer(session_ctx, _action)
            note_ask_streak(session_ctx, _action)
            audit_decision(
                session_id=session_id, user_id=user_id,
                user_message=user_message, action=_action, shadow=False,
            )
            # A ground_place ask must be backed by real place candidates + armed
            # grounding state, or the answer turn dead-ends (QA 2026-07-30).
            _wire_ground_place_action(_action, user_id=user_id, session_ctx=session_ctx)
            session_ctx["policy_chips"] = _action.chips or None
            session_ctx["policy_chip_msgs"] = [c["send"] for c in _action.chips] or None
            session_ctx["last_routing"] = _action.routing_dict()
            session_ctx["_orchestrator_turn"] = False
            session_ctx["timing_ms"] = timer.to_dict()
            reply = sanitize_assistant_message(_action.utterance)
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
        # Host fast path: when the host stage machine below will provably own this
        # turn, run_turn skips its router + synthesizer (their routing stamp, reply,
        # and UI all get overwritten by the host block). "Provably" is deliberately
        # narrow — every condition mirrors a gate the host block itself checks:
        #  · event_host_active and no published event_id (the host block's own gate);
        #  · discovery slots computed FOR THIS MESSAGE read as hosting — the exact
        #    check the back-out cleanup below uses, so the release branch (which
        #    would need the synth reply) cannot fire;
        #  · not the confirm stage and no pending_confirmation — publish turns keep
        #    the full path, where the router may own the create_event call.
        # Anything else takes the unchanged full path.
        from app.layer1_intents import slots_indicate_hosting_signal as _host_slots

        _slots_fresh = (
            str(session_ctx.get("_discovery_slots_for") or "")
            == str(user_message or "").strip()
        )
        host_fast = (
            bool(session_ctx.get("event_host_active"))
            and not prev_event_id
            and not session_ctx.get("pending_confirmation")
            and str(session_ctx.get("host_stage") or "") in ("", "review", "setup")
            and _slots_fresh
            and _host_slots(session_ctx.get("_discovery_slots") or {})
        )
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
            ctx_prefetch=ctx_prefetch,
            host_fast_path=host_fast,
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
                    ed.pop("has_time", None)
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
            wd, wt = _recover_when_from_draft(ed, wd, wt)
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
                # Truthful clock flag: midnight above is a date-only placeholder unless
                # the host actually said a time. Persisted to events.has_time at publish;
                # cards render date-only when False (#56).
                ed["has_time"] = bool(wt)
                # Rebuild ends_at off our corrected start — the LLM extractor mis-years
                # ends_at (e.g. 2023) while we compute the real future date here, which
                # used to ship a wrong-year ends_at through to publish. No clock time →
                # no ends_at: midnight + 90min is a fiction the meet page would render
                # as a real duration.
                from datetime import datetime as _dt, timedelta as _td

                if wt:
                    try:
                        _start = _dt.fromisoformat(ed["starts_at"])
                        _dur = int(ed.get("duration_minutes") or 90)
                        ed["ends_at"] = (_start + _td(minutes=_dur)).isoformat()
                    except (ValueError, TypeError):
                        ed.pop("ends_at", None)
                else:
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
            # need no pin; they resolve to the host's block. Raw coordinates count as a
            # pin too: "Use my current location" sends lat/lng with NO place_id, and
            # without this arm the auto-resolve below Google-searched the literal name
            # "My current location" biased to the home block and re-pinned the event
            # there (the Lake Nona mispin) — the device coordinates are authoritative.
            has_pin = bool(str(ed.get("place_id") or "").strip()) or (
                ed.get("venue_lat") is not None and ed.get("venue_lng") is not None
            )
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
                    reply = compose_reply(
                        goal=(
                            "The host's meet is drafted and shown on a review card. "
                            "Present it and tell them to tap **Looks good** to set it "
                            "up, or **Let me tweak** to change anything (mention both "
                            "buttons by those exact names)."
                        ),
                        facts=[f"The drafted meet's name: {_title}"],
                        fallback=(
                            f"Here's your meet — **{_title}**. Take a look: tap **Looks good** "
                            "to set it up, or **Let me tweak** to change anything."
                        ),
                    )
                else:
                    _ensure_setup_config(
                        ed, history=history, user_message=user_message, timer=timer
                    )
                    _seed_setup_defaults(ed)
                    turn_ctx["host_stage"] = "setup"
                    reply = compose_reply(
                        goal=(
                            "The host is starting to set up a meet and a setup card is "
                            "shown below your message. Invite them to add a name, a "
                            "date & time, and a place in the card below — or to just "
                            "tell you here so you fill them in."
                        ),
                        fallback=(
                            "Let's set it up! Add a name, a date & time, and a place below — or "
                            "just tell me here and I'll fill them in."
                        ),
                        cache=True,
                    )
            elif stage == "review":
                if _is_host_confirm(user_message) or _is_host_drop(user_message):
                    _ensure_setup_config(
                        ed, history=history, user_message=user_message, timer=timer
                    )
                    _seed_setup_defaults(ed)
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    reply = compose_reply(
                        goal=(
                            "The host approved their meet's review and a quick-setup "
                            "card is shown below. Tell them to set capacity, sharing, "
                            "approval, and what to bring there, then drop the meet for "
                            "their neighbors."
                        ),
                        fallback=(
                            "Quick set-up — set capacity, sharing, approval, and what to "
                            "bring, then drop it for your neighbors."
                        ),
                        cache=True,
                    )
                else:
                    # A free-text edit was already merged into the draft above; stay in review.
                    turn_ctx["host_stage"] = "review"
                    ed["suggestions"] = []
                    reply = (
                        compose_reply(
                            goal=(
                                "The host asked to tweak their drafted meet. Invite them "
                                "to say what to change about it."
                            ),
                            facts=[f"The meet's name: {_title}"],
                            fallback=f"Sure — tell me what to change about **{_title}**.",
                        )
                        if _is_host_tweak(user_message)
                        else compose_reply(
                            goal=(
                                "You just applied the host's edit to their drafted meet "
                                "(the updated card is shown). Confirm the update and ask "
                                "if it looks right now."
                            ),
                            facts=[f"The meet's name: {_title}"],
                            fallback=f"Updated **{_title}** — does this look right?",
                        )
                    )
            elif stage == "setup":
                if (_is_host_confirm(user_message) or _is_host_drop(user_message)) and blockers_done:
                    # The sparse-opening path never runs _ensure_review_draft (the entry
                    # gate needs a date/venue up front), so a carousel-built draft reaches
                    # confirm with no description. The draft is final here — backfill the
                    # card blurb now so the confirm card and the published event show one.
                    # Description ONLY: the host's chosen title is never touched.
                    if not str(ed.get("description") or "").strip():
                        from app.event_suggest import event_suggestions

                        with timer.stage("llm_event_suggest"):
                            _sugg = event_suggestions(
                                history=history, user_message=user_message, draft=ed
                            )
                        if _sugg.get("description"):
                            ed["description"] = _sugg["description"]
                    turn_ctx["host_stage"] = "confirm"
                    ed["suggestions"] = []
                    reply = compose_reply(
                        goal=(
                            "The host finished their meet's setup and the final confirm "
                            "card is shown. Tell them it's all set — one last look, then "
                            "they can drop it for their neighbors."
                        ),
                        facts=[f"The meet's name: {_title}"],
                        fallback=(
                            f"It's all set — **{_title}**. One last look, then drop it "
                            "for your neighbors."
                        ),
                    )
                elif _is_host_confirm(user_message) or _is_host_drop(user_message):
                    # Carousel submitted but a blocker is still missing — hold in setup and
                    # say exactly what's needed (the FE cards should have collected these).
                    need = _host_blockers_needed(_title, wd, wt, venue_resolvable)
                    turn_ctx["host_stage"] = "setup"
                    ed["suggestions"] = []
                    reply = compose_reply(
                        goal=(
                            "The host tried to post their meet but a required detail is "
                            "still missing. Tell them exactly which detail(s) you still "
                            "need before it can post — only the ones in the facts."
                        ),
                        facts=[f"Still missing: {', '.join(need)}"],
                        fallback="I just need " + " · ".join(need) + " to post it.",
                    )
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
                    # keyword gates. Apply the extraction last-write-wins (a correction replaces
                    # the old value) and show the reply as an aside (text + compact draft card)
                    # so it's visible over the carousel. The deterministic nudge is only a
                    # fallback for when the LLM is unavailable.
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
                        _apply_host_brain(brain, ed, turn_ctx, session_ctx, settings)
                        reply = brain["reply"]
                        turn_ctx["host_aside"] = True
                    else:
                        reply = _host_fallback_nudge(need)
            else:  # stage == "confirm" → publish when the host drops it
                # Two ways to say "post it": the confirm card's CTA (the FE always sends
                # its canonical English payload — "Drop the meet up" — whatever locale the
                # label is rendered in), and the same ask TYPED free-form in any language
                # ("publícalo", "pode postar"). The CTA matchers catch the first; the host
                # brain's `publish` read catches the second — an AI signal, not a word list.
                drop_asked = _is_host_drop(user_message) or _is_host_confirm(user_message)
                brain = None
                if not drop_asked:
                    # Free text at confirm — an inline edit, a redo ask, a question, or a
                    # publish ask in the host's own words. The brain reads it in any
                    # phrasing: corrections land last-write-wins, and a slot asked to
                    # change WITHOUT its new value ("I want a different spot") comes back
                    # in redo and clears here.
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
                        _apply_host_brain(brain, ed, turn_ctx, session_ctx, settings)
                    # Re-derive the blockers AFTER the brain applied edits/redos — the
                    # locals above predate them (and the router's clear_fields path may
                    # have already blanked a slot before the stage machine ran).
                    _title = str(ed.get("title") or "").strip()
                    wd = turn_ctx.get("event_when_date")
                    wt = turn_ctx.get("event_when_time")
                    venue_resolvable = bool(str(ed.get("venue_name") or "").strip())
                    # Honor the AI's publish read only while the draft is still complete —
                    # a turn that also cleared a blocker must route back to setup instead.
                    drop_asked = bool(
                        brain
                        and brain.get("publish")
                        and _title
                        and wd
                        and wt
                        and venue_resolvable
                    )
                if drop_asked and not phone_verified:
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
                        reply = compose_reply(
                            goal=(
                                "The guest host's meet is fully set, but posting it "
                                "needs a verified email. Celebrate that it's ready, "
                                "then ask for their email so you can verify and post "
                                "it."
                            ),
                            facts=[f"The meet's name: {_title or 'your event'}"],
                            fallback=(
                                f"Perfect — **{_title or 'your event'}** is all set! "
                                "To post it I just need to verify your email — what's your email?"
                            ),
                        )
                    else:
                        reply = compose_reply(
                            goal=(
                                "The host is mid email-verification with their finished "
                                "meet waiting. Tell them to send one more message once "
                                "verified and you'll post it right away."
                            ),
                            facts=[f"The meet's name: {_title or 'your event'}"],
                            fallback=(
                                "Finishing verification — send one more message and I'll "
                                f"post **{_title or 'your event'}** right away."
                            ),
                        )
                elif drop_asked:
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
                                reply = compose_reply(
                                    goal=(
                                        "The guest host's meet is fully set, but "
                                        "posting it needs a verified email. Celebrate "
                                        "that it's ready, then ask for their email so "
                                        "you can verify and post it."
                                    ),
                                    facts=[f"The meet's name: {_title or 'your event'}"],
                                    fallback=(
                                        f"Perfect — **{_title or 'your event'}** is all set! "
                                        "To post it I just need to verify your email — what's your email?"
                                    ),
                                )
                            else:
                                reply = compose_reply(
                                    goal=(
                                        "The host is mid email-verification with their "
                                        "finished meet waiting. Tell them to send one "
                                        "more message once verified and you'll post it "
                                        "right away."
                                    ),
                                    facts=[f"The meet's name: {_title or 'your event'}"],
                                    fallback=(
                                        "Finishing verification — send one more message and I'll "
                                        f"post **{_title or 'your event'}** right away."
                                    ),
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
                    # Free text that wasn't a publish ask (brain already ran + applied
                    # above). A draft still complete afterwards holds at confirm; a cleared
                    # blocker falls back to the setup carousel — the confirm card has no
                    # pickers, so holding would strand the host with a hole they can't fill.
                    if _title and wd and wt and venue_resolvable:
                        turn_ctx["host_stage"] = "confirm"
                        reply = (
                            brain["reply"]
                            if brain
                            else "Tap **Drop the meet up** when you're ready and I'll post it."
                        )
                    else:
                        _ensure_setup_config(
                            ed, history=history, user_message=user_message, timer=timer
                        )
                        _seed_setup_defaults(ed)
                        turn_ctx["host_stage"] = "setup"
                        need = _host_blockers_needed(_title, wd, wt, venue_resolvable)
                        reply = brain["reply"] if brain else _host_fallback_nudge(need)
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
