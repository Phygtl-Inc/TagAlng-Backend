import json
import os
from typing import Any

from app.context import build_system_prompt
from app.lana_ui import (
    event_draft_blockers,
    finalize_event_draft,
    merge_event_drafts,
    parse_event_draft,
    parse_event_turn_ui,
)
from app.orchestrator.json_util import parse_json_object
from app.turn_timing import TurnTimer

EVENT_BUCKET_GUIDE = """
UI buckets for event highlights (pick one per focus):
- time — when: day, date, start time, duration
- venue — place name or area (block-level, never street address)
- audience — who it is for (new moms, runners, families)
- activity — what kind of hang (brunch, run, playdate)
- constraint — house rules (peanut-free, babies welcome, casual)
- capacity — max people, size limit
- purpose — maps to event purpose chip (use only when suggesting cohort_tags)
"""

EVENT_TURN_SUFFIX = f"""
You are in a live **host an event** chat. The user describes an activity they want to host on their block.
Extract structured fields into event_draft on every turn. Use conversation history + latest message.

{EVENT_BUCKET_GUIDE}

Allowed cohort_tags (Purpose chips — pick 0-3 that fit best):
{{purpose_ids}}

When asking a follow-up, QUOTE a short phrase from what the user said (focus_phrase) in assistant_message.
Warm tone; at most 1-2 questions per turn. Ask only when a **blocker** is missing: title, when (starts_at), or place (venue_name).
If the user gave a rich description (time + place + vibe), set status "ready_to_complete" — do not interrogate.

Output ONLY valid JSON (no markdown):
{{
  "assistant_message": "Your reply (include quoted focus phrase when clarifying)",
  "status": "continue",
  "event_draft": {{
    "title": "short event title or null",
    "description": "full friendly description for the event page or null",
    "venue_name": "place name without street number or null",
    "starts_at": "ISO8601 timestamptz if inferable else null",
    "ends_at": "ISO8601 timestamptz if inferable else null",
    "duration_minutes": 90,
    "max_attendees": 12,
    "cohort_tags": ["coffee_stroller"],
    "missing": ["starts_at"]
  }},
  "ui": {{
    "bucket": "time",
    "focus_phrase": "short exact quote from USER text you are asking about (null if none)",
    "highlights": [
      {{ "text": "phrase from user story", "bucket": "time" }}
    ]
  }}
}}

Use status "ready_to_complete" when title, starts_at, and venue_name are all set; otherwise "continue".

Rules:
- status "continue" — missing title, starts_at, or venue_name; set ui.focus_phrase to the phrase you clarify.
- status "ready_to_complete" — title + when + venue are inferable (venue can be a named place like "Lake Nona Commons").
- highlights: 2-8 short phrases from the USER's words (latest message weighted), each with an event bucket.
- cohort_tags: only ids from the allowed list above; suggest best Purpose chips, host may override in UI.
- missing: list field names still unknown (e.g. ["starts_at"]).
- Never ask for or store street address, race, exact age, sex.
- Never promise to invite people or run the event for them — the host decides and publishes.
"""

EVENT_OPENING = """
The user opened **Host an event**. No prior chat.
Invite them to describe the event in their own words — you will highlight details and fill the form.

Output ONLY valid JSON:
{
  "assistant_message": "...",
  "status": "continue",
  "event_draft": {
    "title": null,
    "description": null,
    "venue_name": null,
    "starts_at": null,
    "ends_at": null,
    "duration_minutes": null,
    "max_attendees": null,
    "cohort_tags": [],
    "missing": ["title", "starts_at", "venue_name"]
  },
  "ui": {
    "bucket": null,
    "focus_phrase": null,
    "highlights": []
  }
}
"""


def _vertex_client():
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    return genai.Client(vertexai=True, project=project, location=location)


def _format_history(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "(no messages yet)"
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        who = "User" if role == "user" else "Lana"
        lines.append(f"{who}: {m.get('content', '').strip()}")
    return "\n".join(lines)


def _purpose_ids_block(purpose_ids: list[str]) -> str:
    if not purpose_ids:
        return "(load from get_event_purposes — use best match ids like coffee_stroller, faith_small_group)"
    return ", ".join(purpose_ids)


def reconcile_orchestrator_event_turn(
    *,
    ctx_pack: dict[str, Any],
    history: list[dict[str, Any]],
    utterance: str,
    prev_draft: dict[str, Any] | None,
    synth_draft: dict[str, Any] | None,
    ui: dict[str, Any],
    status: str,
    tool_result: dict[str, Any] | None,
    pending_confirmation: bool,
    timer: TurnTimer | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Fill event_draft from legacy extract when synth/tool left blockers or empty UI."""
    from app.context import format_user_context

    draft = merge_event_drafts(prev_draft, synth_draft or {})
    if tool_result and tool_result.get("event_draft"):
        draft = merge_event_drafts(draft, tool_result["event_draft"])

    needs_extract = bool(event_draft_blockers(draft)) or not (ui.get("highlights"))
    if needs_extract:
        purpose_ids = ctx_pack.get("event_purpose_ids") or []
        user_block = format_user_context(ctx_pack, "event_draft")
        if timer:
            with timer.stage("llm_event_extract"):
                extracted, extracted_ui = extract_event_draft_from_chat(
                    user_context_block=user_block,
                    purpose_ids=purpose_ids,
                    history=history,
                    user_message=utterance,
                    previous_draft=draft,
                )
        else:
            extracted, extracted_ui = extract_event_draft_from_chat(
                user_context_block=user_block,
                purpose_ids=purpose_ids,
                history=history,
                user_message=utterance,
                previous_draft=draft,
            )
        draft = merge_event_drafts(draft, extracted)
        if extracted_ui.get("highlights"):
            ui = extracted_ui

    draft = finalize_event_draft(draft)
    if draft["missing"] or pending_confirmation:
        status = "continue"
    elif status != "ready_to_complete":
        status = "ready_to_complete"
    return status, ui, draft


def extract_event_draft_from_chat(
    *,
    user_context_block: str,
    purpose_ids: list[str],
    history: list[dict[str, Any]],
    user_message: str,
    previous_draft: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Legacy-quality extract: merge conversation into event_draft + ui highlights."""
    payload = "\n\n".join(
        [
            user_context_block,
            "CURRENT EVENT DRAFT (merge updates into this):\n"
            + json.dumps(previous_draft or {}, ensure_ascii=False),
            "CONVERSATION SO FAR:\n" + _format_history(history),
            f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            "Extract event_draft and ui.highlights from the user's words. "
            "Do not invent title, time, or venue not supported by the transcript.",
        ]
    )
    data = _call_event_lana(payload, purpose_ids)
    valid = set(purpose_ids)
    merged = merge_event_drafts(
        previous_draft,
        parse_event_draft(data.get("event_draft"), valid_purpose_ids=valid),
    )
    return finalize_event_draft(merged), parse_event_turn_ui(data)


def _parse_event_turn(
    data: Any,
    *,
    previous_draft: dict[str, Any] | None,
    valid_purpose_ids: set[str],
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_turn_json")
    assistant_message = str(data.get("assistant_message", "")).strip()[:1200]
    if not assistant_message:
        assistant_message = "Describe your event in your own words — I'll help fill in the details."
    status = str(data.get("status", "continue")).lower()
    if status not in ("continue", "ready_to_complete"):
        status = "continue"

    incoming = parse_event_draft(data.get("event_draft"), valid_purpose_ids=valid_purpose_ids)
    merged = merge_event_drafts(previous_draft, incoming)
    if not merged.get("title") or not merged.get("venue_name") or not merged.get("starts_at"):
        status = "continue"
    elif status == "continue" and merged.get("title") and merged.get("venue_name") and merged.get("starts_at"):
        status = "ready_to_complete"

    ui = parse_event_turn_ui(data)
    ctx = {
        "last_status": status,
        "last_ui": ui,
        "event_draft": merged,
    }
    return assistant_message, status, ctx, ui, merged


def _call_event_lana(
    payload: str,
    purpose_ids: list[str],
    *,
    attempts_out: list[int] | None = None,
) -> Any:
    client = _vertex_client()
    model = os.environ.get("VERTEX_LANA_MODEL", os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"))
    from google.genai import types

    suffix = EVENT_TURN_SUFFIX.replace("{purpose_ids}", _purpose_ids_block(purpose_ids))
    system = build_system_prompt() + "\n\n" + suffix

    def _generate(user: str) -> str:
        response = client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                temperature=0.45,
                response_mime_type="application/json",
                system_instruction=system,
            ),
        )
        return response.text or ""

    attempts = 1
    text = _generate(payload)
    try:
        data = parse_json_object(text)
    except (json.JSONDecodeError, ValueError):
        attempts = 2
        text = _generate(
            payload
            + "\n\nYour previous reply was invalid JSON. "
            "Return ONE compact JSON object with event_draft and assistant_message."
        )
        data = parse_json_object(text)
    if attempts_out is not None:
        attempts_out[:] = [attempts]
    return data


def lana_event_opening(
    user_context_block: str,
    purpose_ids: list[str],
    *,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = "\n\n".join([user_context_block, EVENT_OPENING])
    attempts_box: list[int] = []
    if timer:
        with timer.stage("llm_event_turn"):
            data = _call_event_lana(payload, purpose_ids, attempts_out=attempts_box)
        if attempts_box:
            timer.set_count("llm_event_attempts", attempts_box[0])
    else:
        data = _call_event_lana(payload, purpose_ids)
    return _parse_event_turn(data, previous_draft=None, valid_purpose_ids=set(purpose_ids))


def lana_event_turn(
    user_context_block: str,
    purpose_ids: list[str],
    history: list[dict[str, Any]],
    user_message: str,
    previous_draft: dict[str, Any] | None,
    *,
    timer: TurnTimer | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = "\n\n".join(
        [
            user_context_block,
            "CURRENT EVENT DRAFT (merge updates into this):\n"
            + json.dumps(previous_draft or {}, ensure_ascii=False),
            "CONVERSATION SO FAR:\n" + _format_history(history),
            f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            "Reply as Lana. Update event_draft and ui.highlights from the user's words.",
        ]
    )
    attempts_box: list[int] = []
    if timer:
        with timer.stage("llm_event_turn"):
            data = _call_event_lana(payload, purpose_ids, attempts_out=attempts_box)
        if attempts_box:
            timer.set_count("llm_event_attempts", attempts_box[0])
    else:
        data = _call_event_lana(payload, purpose_ids)
    return _parse_event_turn(
        data,
        previous_draft=previous_draft,
        valid_purpose_ids=set(purpose_ids),
    )
