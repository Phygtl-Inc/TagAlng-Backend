"""decide_turn — the unified conversational policy call (engineering doc §C.1).

One LLM call per turn over one assembled context (recent turns + rolling
summary + identity claims + world state + candidate goals + available
capabilities) returning one structured NextAction. The utterance and chips are
lexicon-enforced (lingo_guard) before anyone sees them, and every decision is
written to lana_audit_log with its `why`.

v1 scope, on purpose:
  * kinds reply/ask_gap/ground_place/bridge_offer/capture_defer are answered
    by the policy directly;
  * `handoff` (and any failure) falls through to the existing discovery /
    orchestrator path — action flows (search, host, auth, signals) keep their
    proven engines until shadow metrics say otherwise.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

KINDS = (
    "reply", "follow_thread", "ask_gap", "ground_place", "bridge_offer",
    "capture_defer", "handoff",
)

_RECENT_TURNS = 12
_TURN_CHARS = 400
_MAX_DEFERRED = 10


@dataclass
class NextAction:
    kind: str
    utterance: str
    chips: list[dict[str, str]] = field(default_factory=list)
    goal_id: str | None = None
    defer_goal_id: str | None = None
    # ground_place only: the action the user ALREADY requested that this
    # grounding serves (e.g. "organize a meet for my squash group" needs the
    # squash spot pinned first). The pipeline carries it into the armed
    # grounding state so the confirmed place dispatches the action directly
    # instead of re-offering it (QA 2026-07-30, the squash/Life Time loop).
    pending_action: str | None = None
    # The person is in pain / ill / wrung out AS THEY WRITE and wants nothing
    # done. The policy judges it (no regex — see the distress rule in the
    # prompt); _apply_distress_gate below ENFORCES it, because a prompt rule
    # alone lost to the dead-end backstop, which rejects any question-free turn
    # (QA 2026-08-03: "my stomach hurts, I barely slept" answered with "is there
    # a favorite blue thing that lifts your mood?").
    distress_turn: bool = False
    why: str = ""
    guardrail: dict[str, Any] = field(default_factory=dict)

    def routing_dict(self) -> dict[str, Any]:
        return {
            "outcome": "decide_turn",
            "kind": self.kind,
            "goal_id": self.goal_id,
            "defer_goal_id": self.defer_goal_id,
            "pending_action": self.pending_action,
            "distress_turn": self.distress_turn,
            "why": self.why,
        }


def policy_model() -> str:
    """Model for the policy decision. Defaults to the synthesizer, so unset changes
    nothing.

    Measured 2026-08-06 against OpenAI directly, identical 7,734-token prompt, rate
    limits at 99.97% headroom and the prefix cache warm:
        gpt-4.1        31.8s / 30.2s / 38.0s   (~4 output tokens/sec)
        gpt-4.1-mini    3.5s /  2.6s           (~10x faster)
    At those numbers gpt-4.1 exceeds a 15s per-attempt budget every single time, so
    the turn burns three timeouts and falls through to the legacy path — which
    discards the policy entirely. A cheaper model that ANSWERS beats a better one
    that times out; this exists so the trade can be made per environment and
    reverted instantly.
    """
    import os

    from app.orchestrator.llm import synthesizer_model

    return os.environ.get("LANA_POLICY_MODEL", "").strip() or synthesizer_model()


def _recent_turns(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in history[-_RECENT_TURNS:]:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")[:_TURN_CHARS]
        if role and content:
            out.append({"role": role, "content": content})
    return out


_MAX_CLAIMS = 6
# Below this cosine similarity a stored claim has nothing to do with what the
# person just said. Still passed to the model — but flagged, so it can tell the
# difference between "they told me something that bears on this" and "I am
# rummaging for an excuse to change the subject".
CLAIM_RELEVANCE_FLOOR = 0.55


def _claims(user_id: str, user_message: str = "") -> list[dict[str, Any]]:
    """What we know about them, most relevant to THIS turn first.

    Unranked, this is a bag of facts with no bearing on the moment, and the
    prompt's "changing topics needs a visible why" rule then pushes the model to
    reach into it for a licence to ask something — which is how a fortnight-old
    "favourite colour: blue" beat "food is my comfort and I'm dieting", said one
    line earlier (QA 2026-08-03).

    Fails open in every direction: no message, no embedding, missing RPC (or a
    pre-20260929 environment) all fall back to the previous unranked read, which
    is no worse than before.
    """
    text = str(user_message or "").strip()
    if text:
        try:
            ranked = _claims_ranked(user_id, text)
            if ranked:
                return ranked
        except Exception:
            logger.exception("decide_claims_rank_failed user=%s", user_id)
    try:
        from app.claims_persist import fetch_active_claim_threads

        return fetch_active_claim_threads(user_id)[:_MAX_CLAIMS]
    except Exception:
        logger.exception("decide_claims_failed user=%s", user_id)
        return []


def _claims_ranked(user_id: str, user_message: str) -> list[dict[str, Any]]:
    """Claims ordered by similarity to this turn, each tagged for the model."""
    from app.auth import service_client
    from app.vec_util import to_pgvector
    from app.vertex_extract import vertex_embed

    literal = to_pgvector(vertex_embed(user_message[:600]))
    if not literal:
        return []
    res = service_client().rpc(
        "rank_claims_by_relevance",
        {"p_user_id": user_id, "p_embedding": literal, "p_limit": _MAX_CLAIMS},
    ).execute()
    rows = [r for r in (res.data or []) if isinstance(r, dict)]
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            sim = float(r.get("similarity") or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        out.append({
            "concept": r.get("concept"),
            "label": r.get("label"),
            "details": r.get("details") or [],
            # Spelled out rather than left as a number: the prompt reasons about
            # this, and a bare 0.31 invites the model to invent a threshold.
            "relates_to_this_turn": sim >= CLAIM_RELEVANCE_FLOOR,
        })
    return out


# What counts as "another personal question" for the annoyance guard.
# `follow_thread` is deliberately absent: asking someone more about the thing
# THEY just raised is ordinary conversation, not the interrogation pattern this
# streak exists to stop.
_ASK_KINDS = ("ask_gap", "ground_place")


def ask_streak(session_ctx: dict[str, Any]) -> int:
    try:
        return max(0, int(session_ctx.get("policy_ask_streak") or 0))
    except (TypeError, ValueError):
        return 0


def _utterance_asks_something(utterance: str) -> bool:
    return any(ch in str(utterance or "") for ch in ("?", "؟"))


def _question_names_its_options(utterance: str) -> bool:
    """A closed question — it lists the answers, so they should be tappable.

    Only the sentence that ends in the question mark counts: an "or" in the
    warm-up ("pizza or pasta, you have taste!") decides nothing. Deliberately
    narrow — it fires on "X, or Y?", not on every question.
    """
    import re

    for sentence in re.split(r"(?<=[.!?])\s+", str(utterance or "")):
        s = sentence.strip()
        if not s.endswith(("?", "؟")):
            continue
        if re.search(r"\bor\b", s, re.I):
            return True
    return False


def turn_asks_personal_question(action: NextAction) -> bool:
    """Did the person just get asked about THEMSELVES this turn?

    Judged on the text they read, NOT on the kind the model chose. Counting
    kinds let the cap be walked straight past: three questions in a row all
    shipped as kind='reply' (prod 2026-08-06, "favourite spot… or do you make it
    at home?" -> "favourite pizza spot… or still exploring?" -> "a place you've
    tried recently?"), and because `reply` fell to the else-branch below, each one
    RESET the counter to zero. The guard believed nothing had been asked.

    bridge_offer / capture_defer are excluded on purpose: "want me to find pizza
    spots near you?" is an offer, and charging it to the interrogation budget
    would suppress exactly the turns we want more of.
    """
    if action.kind in _ASK_KINDS:
        return True
    if action.kind in ("reply", "follow_thread"):
        return _utterance_asks_something(action.utterance)
    return False


def note_ask_streak(session_ctx: dict[str, Any], action: NextAction) -> None:
    """Annoyance-guard input: how many personal questions Lana has asked
    back-to-back. A turn that asks extends the streak; anything else clears it
    (with None, never popped — the session merge resurrects popped keys)."""
    if turn_asks_personal_question(action):
        session_ctx["policy_ask_streak"] = ask_streak(session_ctx) + 1
    else:
        session_ctx["policy_ask_streak"] = None


def parse_next_action(data: Any) -> NextAction | None:
    """Validate the raw LLM object into a NextAction; None when unusable."""
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return None
    utterance = str(data.get("utterance") or "").strip()
    if kind != "handoff" and not utterance:
        return None
    chips: list[dict[str, str]] = []
    raw_chips = data.get("chips")
    if isinstance(raw_chips, list):
        for c in raw_chips[:3]:
            if not isinstance(c, dict):
                continue
            label = str(c.get("label") or "").strip()[:40]
            send = str(c.get("send") or label).strip()[:120]
            if label:
                chips.append({"label": label, "send": send})
    goal_id = str(data.get("goal_id") or "").strip() or None
    defer_goal_id = str(data.get("defer_goal_id") or "").strip() or None
    pending_action = str(data.get("pending_action") or "").strip().lower() or None
    # Only the dispatchable kinds count, and only on a grounding ask — anything
    # else is model noise, dropped rather than trusted downstream.
    if kind != "ground_place" or pending_action not in ("host_meet", "find_neighbors"):
        pending_action = None
    why = str(data.get("why") or "").strip()[:300]
    # The model writes JSON, so a stringy "false"/"no" must not read as truthy.
    raw_distress = data.get("distress_turn")
    if isinstance(raw_distress, str):
        distress = raw_distress.strip().lower() in ("true", "yes", "1")
    else:
        distress = bool(raw_distress)
    return NextAction(
        kind=kind, utterance=utterance, chips=chips,
        goal_id=goal_id, defer_goal_id=defer_goal_id,
        pending_action=pending_action, distress_turn=distress, why=why,
    )


def _apply_distress_gate(action: NextAction) -> NextAction:
    """Someone in pain right now is not a rapport opportunity.

    The prompt tells the policy to answer these turns with `reply` and nothing
    else; this makes it structural, because on a distress turn every remaining
    move on the menu is either a question about their profile or a pitch:

      ask_gap / ground_place -> reply      (drop the ask, keep the warm words)
      bridge_offer           -> capture_defer (the offer WAITS one turn, via
                                defer_goal_id, and comes back flagged
                                deferred_earlier — it is never dropped)

    Chips go too: a chip is how an offer gets made, and this turn makes none.
    `handoff` is returned untouched — safety rails and action engines own their
    turns, and a request never reaches here as distress anyway.

    One turn only, by construction: nothing is written to session_ctx, so the
    next turn is judged fresh. A wrong call costs one turn of silence on our
    goals, never a lane the user has to escape.
    """
    if not action.distress_turn or action.kind == "handoff":
        return action
    if action.kind in ("ask_gap", "ground_place"):
        action.kind = "reply"
        action.goal_id = None
        action.pending_action = None
    elif action.kind == "bridge_offer":
        action.kind = "capture_defer"
        action.defer_goal_id = action.defer_goal_id or action.goal_id
        action.goal_id = None
    action.chips = []
    return action


# How many personal questions may run back-to-back before the next one is held.
# 2 asked, the 3rd waits — the QA loop that prompted this was 3 in a row.
MAX_CONSECUTIVE_ASKS = 2


def audit_offer_goal(action: NextAction, goals: list[dict[str, Any]], user_id: str) -> None:
    """Make an offer's goal_id observable — it is otherwise unverified.

    Two silent failures, both the "the label doesn't match what happened" shape
    that caused three separate bugs on 2026-08-06: a bridge_offer that pitches a
    capability but attaches no goal_id (prod 12:43:57 offered sharing.host with
    goal_id None, so nothing downstream can tell which capability was pitched or
    whether it converted), and a goal_id naming a goal that was never on this
    turn's menu.

    Deliberately log-only. The fix for both is in the prompt; escalating to a
    revision_note would buy a cosmetic field with a second LLM round-trip on a
    hot path, and this defect never reaches the user's screen. If the logs show
    it is still common after the prompt change, THEN make it enforce.
    """
    if action.kind not in ("bridge_offer", "ask_gap", "ground_place"):
        return
    gid = str(action.goal_id or "")
    if not gid:
        if action.kind == "bridge_offer":
            logger.warning(
                "decide_turn_offer_without_goal user=%s utterance=%r",
                user_id, str(action.utterance or "")[:120],
            )
        return
    known = {str(g.get("id") or "") for g in goals}
    if gid not in known:
        logger.warning(
            "decide_turn_goal_not_on_menu user=%s kind=%s goal=%r",
            user_id, action.kind, gid,
        )


def _apply_ask_ceiling(action: NextAction, streak: int) -> NextAction:
    """Hard ceiling on back-to-back personal questions.

    `consecutive_personal_asks` has always been in the payload and the prompt has
    always advised on it ("the higher it is, the stronger the case for giving
    instead of asking") — advice with nothing behind it, so a third and fourth
    ask were still reachable. The question is not dropped: it becomes a
    `capture_defer`, so the goal resurfaces flagged `deferred_earlier` once the
    person has had a turn that wasn't an interrogation.

    `ground_place` is deliberately exempt — "which gym did you mean?" finishes
    something the user already started and is usually the last step before we can
    act, not profile-deepening.
    """
    if action.kind != "ask_gap" or streak < MAX_CONSECUTIVE_ASKS:
        return action
    action.kind = "capture_defer"
    action.defer_goal_id = action.defer_goal_id or action.goal_id
    action.goal_id = None
    return action


_ASK_KINDS_ALL = ("ask_gap", "ground_place", "bridge_offer")


def _revision_note(action: NextAction, *, streak: int) -> str | None:
    """The one corrective retry, shared by every shape violation.

    Each of these is a turn the user would visibly experience as wrong, and each
    is only fixable by rewriting the utterance — a kind downgrade leaves the
    offending sentence on screen. None means the decision is fine as returned.
    """
    decision = json.dumps(
        {"kind": action.kind, "utterance": action.utterance}, ensure_ascii=False
    )
    if action.distress_turn and action.kind in _ASK_KINDS_ALL:
        # QA 2026-08-03: "my stomach hurts, I barely slept" → "is there a
        # favorite blue thing that lifts your mood?".
        return (
            "Your decision for this turn was " + decision + " — but you judged this a "
            "distress turn, and it still asks them something about themselves or "
            "pitches something. Revise it: answer what they actually said with warmth, "
            "and either say nothing further or ask ONLY about the thing THEY raised. "
            "No question about their profile, no offer, no chips. If a goal was worth "
            "making, return kind=capture_defer with it in defer_goal_id so it comes "
            "back later."
        )
    # Judged on the text, not the kind: three questions in a row all shipped as
    # kind='reply' and never tripped this (prod 2026-08-06).
    if turn_asks_personal_question(action) and streak >= MAX_CONSECUTIVE_ASKS:
        return (
            "Your decision for this turn was " + decision + f" — but you have already "
            f"asked {streak} personal questions back-to-back, and this asks another one. "
            "It counts however you labelled it: what matters is that they read a "
            "question about themselves. That reads as an interrogation. Revise it: GIVE "
            "instead of asking — acknowledge what they said, then offer the single most "
            "useful thing you can actually do for them from AVAILABLE CAPABILITIES, with "
            "a chip to accept it. Someone exploring places wants recommendations, not a "
            "fourth question. Keep any queued question for later with kind=capture_defer "
            "and its goal id in defer_goal_id."
        )
    # Chips the person can tap, where the answers are already decided. The prompt
    # asks for these; on its own it did not deliver them (prod 2026-08-06 — "a
    # favourite pizza spot around here, or are you still exploring?" shipped bare
    # and the person typed "Exploring", a word Lana had just put in front of them).
    if not action.chips and not action.distress_turn and action.kind != "handoff":
        if action.kind == "bridge_offer":
            return (
                "Your decision for this turn was " + decision + " — it offers something "
                "but gives them nothing to tap. They then have to guess the words that "
                "trigger it, which is how an offer becomes a dead end. Revise it: keep "
                "the wording and add ONE chip that accepts the offer, whose `send` is a "
                "self-contained message an engine can act on by itself."
            )
        if _question_names_its_options(action.utterance):
            return (
                "Your decision for this turn was " + decision + " — your question names "
                "the possible answers ('X, or Y?') but ships no chips, so they must type "
                "a word you just handed them. Revise it: add one chip per option you "
                "named, label under 28 chars, each `send` a complete answer. Keep the "
                "question exactly as it is."
            )
    if (
        action.kind != "handoff"
        # A distress turn is ALLOWED to end without a question or a chip — that
        # silence IS the decision, and this note would push a question back onto it.
        and not action.distress_turn
        and not action.chips
        and not any(ch in str(action.utterance or "") for ch in ("?", "؟"))
    ):
        # Dead-end backstop. The prompt's prime directive ("never leave a turn as
        # a dead end") had no enforcement — bare acknowledgements shipped as-is
        # (QA 2026-07-30: "thanks for sharing your go-to spot", end of thread).
        return (
            "Your decision for this turn was " + decision + " — it ends the "
            "conversation with no question, no chips and no actionable offer: a dead "
            "end. Revise it: keep the warm acknowledgement, but end on either ONE "
            "gentle follow-up question or ONE concrete offer (with a chip) drawn from "
            "CANDIDATE GOALS / AVAILABLE CAPABILITIES. Only if the person explicitly "
            "declined, said goodbye, or closed the conversation themselves may a plain "
            "close stand — in that case return it unchanged."
        )
    return None


def _system_prompt() -> str:
    from app.context import load_prompt, voice_rules

    return load_prompt("lana_policy_decide.md") + "\n\n---\n\n" + voice_rules()


def name_change_signal(session_ctx: dict[str, Any]) -> dict[str, Any] | None:
    """A rename applied THIS turn, stamped by whichever path persisted it.

    The user has to HEAR the change land. Answering to a new name without a word
    about it is exactly how a wrong one ("Orlando", then "Not") went unnoticed
    for days — the reply sounded fine while the profile was wrong.
    """
    raw = session_ctx.get("nickname_changed")
    if not isinstance(raw, dict):
        return None
    to = str(raw.get("to") or "").strip()
    if not to:
        return None
    return {"from": str(raw.get("from") or "").strip() or None, "to": to}


def decide_turn(
    *,
    user_id: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
    answering_question: str | None = None,
) -> NextAction | None:
    """One policy decision. None means 'no decision' — the caller falls
    through to the legacy path (same effect as kind=handoff)."""
    from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model
    from app.policy.goals import candidate_goals
    from app.policy.world import world_state

    if not llm_configured():
        return None
    import time as _time

    _t0 = _time.monotonic()
    try:
        world = world_state(user_id)
        streak = ask_streak(session_ctx)
        deferred = [
            str(g) for g in (session_ctx.get("deferred_goal_ids") or []) if g
        ]
        _t_world = _time.monotonic() - _t0
        goals = candidate_goals(user_id, world, deferred_goal_ids=deferred)
        _t_goals = _time.monotonic() - _t0
        _known = _claims(user_id, user_message)
        _t_claims = _time.monotonic() - _t0
        # decide_turn's stage time is NOT the model's time: switching the policy to a
        # fast model took the LLM call to ~1.8s while the stage still read 9s, i.e.
        # 7s of context assembly nobody could see (prod-local 2026-08-06). Logged as
        # cumulative offsets so the expensive step is obvious at a glance.
        logger.info(
            "decide_turn_context user=%s world=%dms goals=%dms claims=%dms",
            user_id,
            int(_t_world * 1000),
            int((_t_goals - _t_world) * 1000),
            int((_t_claims - _t_goals) * 1000),
        )
        payload = {
            "message": str(user_message or "")[:1000],
            # The rapport-tile question this message replies to. The tile lives on
            # the home screen, NOT in chat — without this the policy can't tell
            # which ask a "why are you asking?" refers to (QA 2026-07-29: it
            # explained the name ask when the tile had asked about languages).
            "answering_question": (str(answering_question or "").strip()[:300] or None),
            "recent_turns": _recent_turns(history),
            "conversation_summary": str(session_ctx.get("rolling_summary") or "") or None,
            # Ordered by relevance to THIS message, each flagged with whether it
            # bears on the turn at all.
            "known_about_them": _known,
            "world": world,
            "candidate_goals": goals,
            "available_capabilities": [
                g["context"]["capability_id"] for g in goals if g["kind"] == "capability"
            ],
            "consecutive_personal_asks": streak,
            # Hard, not advisory: at the ceiling `ask_gap` is off the menu this
            # turn. Told up front so the model gives instead of asking, rather
            # than being corrected after the fact.
            "may_ask_personal_question": streak < MAX_CONSECUTIVE_ASKS,
            "session_language": session_ctx.get("lang") or "en",
            # Set only on a turn that actually changed their name. Present = say so.
            "name_just_changed": name_change_signal(session_ctx),
        }
        data = llm_json(
            model=policy_model(),
            system=_system_prompt(),
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=700,
            temperature=0.4,
        )
        action = parse_next_action(data)
        if action is None:
            logger.warning("decide_turn_unparseable user=%s", user_id)
            return None
        note = _revision_note(action, streak=streak)
        if note:
            # One corrective retry. Kind downgrades alone can't fix these: the
            # question the user reads lives in the UTTERANCE, so re-asking the
            # model is the only way to change what they actually see. The
            # revised answer stands either way — a deliberate warm close after
            # an explicit goodbye survives by simply repeating itself.
            retry = dict(payload)
            retry["revision_note"] = note
            try:
                data = llm_json(
                    model=policy_model(),
                    system=_system_prompt(),
                    user_payload=json.dumps(retry, ensure_ascii=False),
                    max_tokens=700,
                    temperature=0.4,
                )
                revised = parse_next_action(data)
                if revised is not None and revised.kind != "handoff":
                    logger.info(
                        "decide_turn_revised user=%s kind=%s->%s reason=%s",
                        user_id, action.kind, revised.kind, note[:40],
                    )
                    action = revised
            except Exception:  # noqa: BLE001 — a backstop must never kill the decision
                logger.exception("decide_turn_revision_failed user=%s", user_id)
        # Gates last, and unconditionally: the retry is a request, these are the
        # guarantee. A model that ignores the revision note still cannot ship an
        # ask_gap on a distress turn or past the ceiling.
        action = _apply_distress_gate(action)
        action = _apply_ask_ceiling(action, streak)
        # After the retry AND the gates, so it audits what actually ships — a
        # revision or a gate can change the kind and drop the goal id.
        audit_offer_goal(action, goals, user_id)
        if action.distress_turn:
            logger.info(
                "decide_turn_distress user=%s kind=%s why=%r", user_id, action.kind, action.why,
            )
    except Exception:
        logger.exception("decide_turn_failed user=%s", user_id)
        return None

    if action.kind == "handoff":
        return action

    # Lexicon enforcement — the constitution's hard rules, guaranteed on output.
    from app.lingo_guard import enforce

    guard = enforce(action.utterance, [c["label"] for c in action.chips])
    action.utterance = guard.text
    for chip, clean_label in zip(action.chips, guard.chip_labels):
        chip["label"] = clean_label
    action.guardrail = guard.audit_dict()
    return action


def apply_defer(session_ctx: dict[str, Any], action: NextAction) -> None:
    """capture_defer bookkeeping: park the goal id in session context so
    candidate_goals marks it resurfaceable at the next natural pause."""
    if action.kind != "capture_defer" or not action.defer_goal_id:
        return
    deferred = [str(g) for g in (session_ctx.get("deferred_goal_ids") or []) if g]
    if action.defer_goal_id not in deferred:
        deferred.append(action.defer_goal_id)
    session_ctx["deferred_goal_ids"] = deferred[-_MAX_DEFERRED:]


def audit_decision(
    *,
    session_id: str,
    user_id: str,
    user_message: str,
    action: NextAction,
    shadow: bool,
) -> None:
    """Every decision lands in lana_audit_log with its `why` and the REAL
    guardrail verdict — the eval + shadow-diff substrate."""
    try:
        from app.orchestrator.audit import log_turn

        log_turn(
            session_id=session_id,
            user_id=user_id,
            event_type="decide_turn_shadow" if shadow else "decide_turn",
            module="policy",
            utterance=user_message,
            response=action.utterance,
            guardrail_result=action.guardrail or {"rail": "n/a"},
            routing=action.routing_dict(),
        )
    except Exception:
        logger.exception("decide_turn_audit_failed")


def run_shadow(
    *,
    user_id: str,
    session_id: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
) -> None:
    """Shadow mode: compute + log the policy's decision on a daemon thread while
    the legacy path answers the user. Zero added latency, full decision diff in
    lana_audit_log (legacy turns log their routing already)."""
    import threading

    ctx_snapshot = {
        "deferred_goal_ids": list(session_ctx.get("deferred_goal_ids") or []),
        "rolling_summary": session_ctx.get("rolling_summary"),
        "lang": session_ctx.get("lang"),
        "policy_ask_streak": session_ctx.get("policy_ask_streak"),
    }
    hist_snapshot = [dict(m) for m in history[-_RECENT_TURNS:]]

    def _run() -> None:
        action = decide_turn(
            user_id=user_id,
            session_ctx=ctx_snapshot,
            history=hist_snapshot,
            user_message=user_message,
        )
        if action is not None:
            audit_decision(
                session_id=session_id,
                user_id=user_id,
                user_message=user_message,
                action=action,
                shadow=True,
            )

    threading.Thread(target=_run, daemon=True, name=f"decide-shadow-{session_id[:8]}").start()
