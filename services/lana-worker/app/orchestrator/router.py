import json
from typing import Any

from app.context import load_prompt
from app.orchestrator.llm import llm_json, router_model
from app.orchestrator.memory import format_core_block, format_recent_turns


ROUTER_SCHEMA = """
{
  "outcome": "R" | "A" | "T" | "C",
  "intent_class": "identity" | "discovery" | "activity" | "marketplace" | "tier" | "companionship" | "off_topic",
  "confidence": 0.0,
  "tool_to_call": null,
  "tool_args": null,
  "missing_slots": [],
  "sentiment": "neutral",
  "needs_confirmation": false,
  "thinking": "brief rationale"
}
"""


def route_turn(
    *,
    purpose: str,
    utterance: str,
    core_block: dict[str, Any],
    history: list[dict[str, Any]],
    guardrail_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system = load_prompt("orchestrator_router.md")
    payload = "\n\n".join(
        [
            format_core_block(core_block),
            f"SESSION PURPOSE: {purpose}",
            "RECENT TURNS:\n" + format_recent_turns(history, limit=6),
            f"USER MESSAGE (latest):\n{utterance.strip()}",
            f"GUARDRAIL FLAGS: {json.dumps(guardrail_flags or {})}",
            "Classify and route. Output ONLY JSON matching:\n" + ROUTER_SCHEMA,
        ]
    )
    raw = llm_json(model=router_model(), system=system, user_payload=payload, max_tokens=512)
    return _normalize_router(raw, utterance=utterance)


def _normalize_router(raw: dict[str, Any], *, utterance: str) -> dict[str, Any]:
    outcome = str(raw.get("outcome", "R")).upper()[:1]
    if outcome not in ("R", "A", "T", "C"):
        outcome = "R"
    confidence = float(raw.get("confidence", 0.5))
    try:
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.5

    tool = raw.get("tool_to_call")
    if tool is not None:
        tool = str(tool).strip() or None

    # Enforce capture on C outcome
    if outcome == "C" and not tool:
        tool = "capture_inquiry"
        raw.setdefault("tool_args", {})
        args = raw["tool_args"] if isinstance(raw.get("tool_args"), dict) else {}
        args.setdefault("raw_query", utterance.strip())
        args.setdefault("extracted_category", raw.get("intent_class", "other"))
        args.setdefault("sentiment", raw.get("sentiment", "neutral"))
        raw["tool_args"] = args

    return {
        "outcome": outcome,
        "intent_class": str(raw.get("intent_class", "companionship"))[:32],
        "confidence": confidence,
        "tool_to_call": tool,
        "tool_args": raw.get("tool_args") if isinstance(raw.get("tool_args"), dict) else None,
        "missing_slots": raw.get("missing_slots") if isinstance(raw.get("missing_slots"), list) else [],
        "sentiment": str(raw.get("sentiment", "neutral"))[:32],
        "needs_confirmation": bool(raw.get("needs_confirmation", False)),
        "thinking": str(raw.get("thinking", ""))[:500],
    }
