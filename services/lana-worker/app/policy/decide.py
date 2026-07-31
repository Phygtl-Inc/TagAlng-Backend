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

KINDS = ("reply", "ask_gap", "ground_place", "bridge_offer", "capture_defer", "handoff")

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
    why: str = ""
    guardrail: dict[str, Any] = field(default_factory=dict)

    def routing_dict(self) -> dict[str, Any]:
        return {
            "outcome": "decide_turn",
            "kind": self.kind,
            "goal_id": self.goal_id,
            "defer_goal_id": self.defer_goal_id,
            "pending_action": self.pending_action,
            "why": self.why,
        }


def _recent_turns(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in history[-_RECENT_TURNS:]:
        role = str(m.get("role") or "")
        content = str(m.get("content") or "")[:_TURN_CHARS]
        if role and content:
            out.append({"role": role, "content": content})
    return out


def _claims(user_id: str) -> list[dict[str, Any]]:
    try:
        from app.claims_persist import fetch_active_claim_threads

        return fetch_active_claim_threads(user_id)[:8]
    except Exception:
        logger.exception("decide_claims_failed user=%s", user_id)
        return []


_ASK_KINDS = ("ask_gap", "ground_place")


def ask_streak(session_ctx: dict[str, Any]) -> int:
    try:
        return max(0, int(session_ctx.get("policy_ask_streak") or 0))
    except (TypeError, ValueError):
        return 0


def note_ask_streak(session_ctx: dict[str, Any], action: NextAction) -> None:
    """Annoyance-guard input: how many personal questions Lana has asked
    back-to-back. ask_gap/ground_place extend the streak; anything else clears
    it (with None, never popped — the session merge resurrects popped keys)."""
    if action.kind in _ASK_KINDS:
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
    return NextAction(
        kind=kind, utterance=utterance, chips=chips,
        goal_id=goal_id, defer_goal_id=defer_goal_id,
        pending_action=pending_action, why=why,
    )


def _system_prompt() -> str:
    from app.context import load_prompt, voice_rules

    return load_prompt("lana_policy_decide.md") + "\n\n---\n\n" + voice_rules()


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
    try:
        world = world_state(user_id)
        deferred = [
            str(g) for g in (session_ctx.get("deferred_goal_ids") or []) if g
        ]
        goals = candidate_goals(user_id, world, deferred_goal_ids=deferred)
        payload = {
            "message": str(user_message or "")[:1000],
            # The rapport-tile question this message replies to. The tile lives on
            # the home screen, NOT in chat — without this the policy can't tell
            # which ask a "why are you asking?" refers to (QA 2026-07-29: it
            # explained the name ask when the tile had asked about languages).
            "answering_question": (str(answering_question or "").strip()[:300] or None),
            "recent_turns": _recent_turns(history),
            "conversation_summary": str(session_ctx.get("rolling_summary") or "") or None,
            "known_about_them": _claims(user_id),
            "world": world,
            "candidate_goals": goals,
            "available_capabilities": [
                g["context"]["capability_id"] for g in goals if g["kind"] == "capability"
            ],
            "consecutive_personal_asks": ask_streak(session_ctx),
            "session_language": session_ctx.get("lang") or "en",
        }
        data = llm_json(
            model=synthesizer_model(),
            system=_system_prompt(),
            user_payload=json.dumps(payload, ensure_ascii=False),
            max_tokens=700,
            temperature=0.4,
        )
        action = parse_next_action(data)
        if action is None:
            logger.warning("decide_turn_unparseable user=%s", user_id)
            return None
        utt = str(action.utterance or "")
        if (
            action.kind != "handoff"
            and not action.chips
            and not any(ch in utt for ch in ("?", "؟"))
        ):
            # Dead-end backstop. The prompt's prime directive ("never leave a
            # turn as a dead end") had no enforcement — bare acknowledgements
            # shipped as-is (QA 2026-07-30: "thanks for sharing your go-to
            # spot", end of thread). One corrective retry re-raising the rule;
            # the revised answer stands either way, so a deliberate warm close
            # after an explicit decline/goodbye survives by simply repeating.
            retry = dict(payload)
            retry["revision_note"] = (
                "Your decision for this turn was "
                + json.dumps({"kind": action.kind, "utterance": action.utterance},
                             ensure_ascii=False)
                + " — it ends the conversation with no question, no chips and no "
                "actionable offer: a dead end. Revise it: keep the warm "
                "acknowledgement, but end on either ONE gentle follow-up question "
                "or ONE concrete offer (with a chip) drawn from CANDIDATE GOALS / "
                "AVAILABLE CAPABILITIES. Only if the person explicitly declined, "
                "said goodbye, or closed the conversation themselves may a plain "
                "close stand — in that case return it unchanged."
            )
            try:
                data = llm_json(
                    model=synthesizer_model(),
                    system=_system_prompt(),
                    user_payload=json.dumps(retry, ensure_ascii=False),
                    max_tokens=700,
                    temperature=0.4,
                )
                revised = parse_next_action(data)
                if revised is not None and revised.kind != "handoff":
                    logger.info(
                        "decide_turn_deadend_revised user=%s kind=%s->%s",
                        user_id, action.kind, revised.kind,
                    )
                    action = revised
            except Exception:  # noqa: BLE001 — the backstop must never kill the decision
                logger.exception("decide_turn_deadend_retry_failed user=%s", user_id)
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
