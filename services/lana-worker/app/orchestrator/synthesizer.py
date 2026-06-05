import json
from typing import Any

from app.context import build_system_prompt, load_prompt
from app.lana_ui import merge_event_drafts, parse_event_draft, parse_event_turn_ui, parse_turn_ui, finalize_event_draft
from app.orchestrator.llm import llm_json, router_model, synthesizer_model
from app.orchestrator.memory import format_core_block, format_recent_turns, format_recall_memories
from app.turn_timing import TurnTimer
from app.vertex_event import EVENT_BUCKET_GUIDE


def _synth_model(outcome: str, tool_result: dict[str, Any] | None, *, purpose: str = "") -> str:
    """Synth model for tool/hero turns; router model for simple R/A."""
    if purpose == "event_draft":
        return router_model()
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
    purpose_ids: list[str] | None = None,
    timer: TurnTimer | None = None,
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
    if purpose == "profile_intake" and utterance.strip().startswith("(session start"):
        payload_parts.append(
            'OPENING TURN: First chat line after "Meet Lana". '
            'Say something like: "So — *who are you*, right now?" — warm, one question, invite their story.'
        )
    if purpose == "event_draft":
        ids = purpose_ids or []
        payload_parts.append(
            "EVENT HOSTING: You MUST fill event_draft from USER words every turn "
            "(title, starts_at ISO8601, venue_name). Merge with CURRENT EVENT DRAFT in core block.\n"
            + EVENT_BUCKET_GUIDE
            + "\nAllowed cohort_tags: "
            + (", ".join(ids) if ids else "see get_event_purposes")
            + "\nDo NOT say the event is ready unless title, starts_at, and venue_name are in event_draft."
        )
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

    model = _synth_model(outcome, tool_result, purpose=purpose)
    attempts_box: list[int] = []
    if timer:
        with timer.stage("llm_synth"):
            raw = llm_json(
                model=model,
                system=system,
                user_payload=payload,
                max_tokens=2048,
                temperature=0.55,
                llm_attempts=attempts_box,
            )
        if attempts_box:
            timer.set_count("llm_synth_attempts", attempts_box[0])
    else:
        raw = llm_json(
            model=model,
            system=system,
            user_payload=payload,
            max_tokens=2048,
            temperature=0.55,
        )

    if purpose == "event_draft":
        return _parse_event_synth(
            raw,
            prev_draft=prev_draft,
            tool_result=tool_result,
            valid_purpose_ids=set(purpose_ids or []),
        )
    return _parse_profile_synth(raw)


def synthesize_opening(
    *,
    purpose: str,
    core_block: dict[str, Any],
    purpose_ids: list[str] | None = None,
    timer: TurnTimer | None = None,
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
        purpose_ids=purpose_ids,
        timer=timer,
    )


def _profile_synth_schema() -> str:
    return """{
  "assistant_message": "single-line warm reply",
  "status": "continue",
  "topics_covered": [],
  "topics_to_explore": [],
  "ui": { "bucket": null, "focus_phrase": null, "highlights": [] }
}

Rules: status is continue or ready_to_complete. assistant_message ONE line only. Omit core_patch unless session goal changed."""


def _event_synth_schema() -> str:
    return """{
  "assistant_message": "Your warm reply to the host (one line)",
  "status": "continue",
  "event_draft": {
    "title": "short title from user words or null",
    "description": null,
    "venue_name": "place name or null",
    "starts_at": "ISO8601 timestamptz or null",
    "ends_at": null,
    "duration_minutes": null,
    "max_attendees": null,
    "cohort_tags": [],
    "missing": []
  },
  "ui": {
    "bucket": "activity",
    "focus_phrase": null,
    "highlights": [{ "text": "phrase from user", "bucket": "time" }]
  }
}

status: continue until title + starts_at + venue_name are set; then ready_to_complete.
Fill event_draft from conversation — never leave all null if user gave details."""


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
    valid_purpose_ids: set[str] | None = None,
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
    parsed = parse_event_draft({"event_draft": draft_raw}, valid_purpose_ids=valid_purpose_ids)
    if prev_draft:
        parsed = merge_event_drafts(prev_draft, parsed)
    parsed = finalize_event_draft(parsed)
    missing = parsed["missing"]

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
