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
    why: str = ""
    guardrail: dict[str, Any] = field(default_factory=dict)

    def routing_dict(self) -> dict[str, Any]:
        return {
            "outcome": "decide_turn",
            "kind": self.kind,
            "goal_id": self.goal_id,
            "defer_goal_id": self.defer_goal_id,
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
    why = str(data.get("why") or "").strip()[:300]
    return NextAction(
        kind=kind, utterance=utterance, chips=chips,
        goal_id=goal_id, defer_goal_id=defer_goal_id, why=why,
    )


def _system_prompt() -> str:
    from app.context import lingo_constitution, load_prompt

    return load_prompt("lana_policy_decide.md") + "\n\n---\n\n" + lingo_constitution()


def decide_turn(
    *,
    user_id: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_message: str,
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
            "recent_turns": _recent_turns(history),
            "conversation_summary": str(session_ctx.get("rolling_summary") or "") or None,
            "known_about_them": _claims(user_id),
            "world": world,
            "candidate_goals": goals,
            "available_capabilities": [
                g["context"]["capability_id"] for g in goals if g["kind"] == "capability"
            ],
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
