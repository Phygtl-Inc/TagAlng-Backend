import json
from typing import Any

from app.context import build_system_prompt, load_prompt
from app.lana_ui import merge_event_drafts, parse_event_draft, parse_event_turn_ui, parse_turn_ui
from app.orchestrator.llm import llm_json, router_model, synthesizer_model
from app.orchestrator.memory import format_core_block, format_recent_turns, format_recall_memories


def _synth_model(outcome: str, tool_result: dict[str, Any] | None) -> str:
    """Synth model for tool/hero turns; router model for simple R/A."""
    if outcome == "T" and tool_result:
        return synthesizer_model()
    if tool_result and tool_result.get("tool") == "recall":
        return synthesizer_model()
    if outcome == "C":
        return synthesizer_model()
    return router_model()


def synthesize_turn(
    *,
    purpose: str,
    utterance: str,
    routing: dict[str, Any],
    core_block: dict[str, Any],
    history: list[dict[str, Any]],
    tool_result: dict[str, Any] | None,
    prev_draft: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    outcome = routing.get("outcome", "R")
    system = build_system_prompt() + "\n\n---\n\n" + load_prompt("orchestrator_synth.md")

    if purpose == "event_draft":
        schema = _event_synth_schema()
    else:
        schema = _profile_synth_schema()

    payload_parts = [
        format_core_block(core_block),
        f"SESSION PURPOSE: {purpose}",
        f"ROUTER OUTCOME: {outcome}",
        f"ROUTING: {json.dumps(routing)}",
        f"TOOL RESULT: {json.dumps(tool_result or {})}",
    ]
    if tool_result and tool_result.get("tool") == "recall":
        memories = tool_result.get("memories") or []
        payload_parts.append("RECALL RESULTS:\n" + format_recall_memories(memories))
    payload_parts.extend(
        [
            "RECENT TURNS:\n" + format_recent_turns(history, limit=6),
            f"USER MESSAGE:\n{utterance.strip()}",
            "Write Lana's reply. Output ONLY JSON:\n" + schema,
        ]
    )
    payload = "\n\n".join(payload_parts)

    model = _synth_model(outcome, tool_result)
    raw = llm_json(model=model, system=system, user_payload=payload, max_tokens=900, temperature=0.55)

    if purpose == "event_draft":
        return _parse_event_synth(raw, prev_draft=prev_draft, tool_result=tool_result)
    return _parse_profile_synth(raw)


def synthesize_opening(
    *,
    purpose: str,
    core_block: dict[str, Any],
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    utterance = "(session start)"
    routing = {"outcome": "R", "intent_class": "companionship", "confidence": 1.0}
    return synthesize_turn(
        purpose=purpose,
        utterance=utterance,
        routing=routing,
        core_block=core_block,
        history=[],
        tool_result=None,
        prev_draft=None,
    )


def _profile_synth_schema() -> str:
    return """{
  "assistant_message": "...",
  "status": "continue" | "ready_to_complete",
  "topics_covered": [],
  "topics_to_explore": [],
  "core_patch": {
    "session": {
      "current_goal": null,
      "last_topic": null,
      "last_captured_inquiry_id": null
    }
  },
  "ui": { "bucket": null, "focus_phrase": null, "highlights": [] }
}"""


def _event_synth_schema() -> str:
    return """{
  "assistant_message": "...",
  "status": "continue" | "ready_to_complete",
  "event_draft": {
    "title": null, "description": null, "venue_name": null,
    "starts_at": null, "ends_at": null, "duration_minutes": null,
    "max_attendees": null, "cohort_tags": [], "missing": []
  },
  "core_patch": {
    "session": {
      "current_goal": null,
      "last_topic": null
    }
  },
  "ui": { "bucket": "activity", "focus_phrase": null, "highlights": [] }
}"""


def _parse_profile_synth(raw: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any], None]:
    assistant_message = str(raw.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "Tell me a bit about you — I'd love to hear your story."
    status = str(raw.get("status", "continue")).lower()
    if status not in ("continue", "ready_to_complete"):
        status = "continue"
    ui = parse_turn_ui(raw)
    core_patch = raw.get("core_patch") if isinstance(raw.get("core_patch"), dict) else None
    ctx = {
        "topics_covered": raw.get("topics_covered") or [],
        "topics_to_explore": raw.get("topics_to_explore") or [],
        "last_status": status,
        "last_ui": ui,
    }
    if core_patch:
        ctx["core_patch"] = core_patch
    return assistant_message, status, ctx, ui, None


def _parse_event_synth(
    raw: dict[str, Any],
    *,
    prev_draft: dict[str, Any] | None,
    tool_result: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    assistant_message = str(raw.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "What are you thinking of hosting on the block?"
    status = str(raw.get("status", "continue")).lower()
    if status not in ("continue", "ready_to_complete"):
        status = "continue"

    draft_raw = raw.get("event_draft")
    if not isinstance(draft_raw, dict):
        draft_raw = {}
    if tool_result and tool_result.get("event_draft"):
        draft_raw = merge_event_drafts(draft_raw, tool_result["event_draft"])
    parsed = parse_event_draft({"event_draft": draft_raw})
    if prev_draft:
        parsed = merge_event_drafts(prev_draft, parsed)

    missing = [f for f in ("title", "starts_at", "venue_name") if not (parsed.get(f) or "").strip()]
    parsed["missing"] = missing
    if missing:
        status = "continue"
    elif status != "ready_to_complete" and not missing:
        status = "ready_to_complete"

    ui = parse_event_turn_ui(raw)
    core_patch = raw.get("core_patch") if isinstance(raw.get("core_patch"), dict) else None
    ctx = {
        "last_status": status,
        "last_ui": ui,
        "event_draft": parsed,
    }
    if core_patch:
        ctx["core_patch"] = core_patch
    if tool_result and tool_result.get("needs_user_confirmation"):
        ctx["pending_confirmation"] = tool_result.get("confirmation_prompt")
    if tool_result and tool_result.get("published"):
        status = "ready_to_complete"
        ctx["event_id"] = tool_result.get("event_id")
        ctx.pop("pending_confirmation", None)

    return assistant_message, status, ctx, ui, parsed
