import os
from typing import Any

from app.lana_ui import merge_event_drafts, parse_event_draft
from app.models import EventDraft, MappedSpan
from app.orchestrator.json_util import parse_json_object

EVENT_EXTRACT_PROMPT = """You are an event extraction model for TagAlng host flow.

Read the full conversation between Lana and the host. Produce a structured event draft for create_event.

Allowed cohort_tags (Purpose ids only):
{purpose_ids}

Output ONLY valid JSON (no markdown):
{{
  "event_draft": {{
    "title": "short title",
    "description": "friendly description including constraints like dietary notes",
    "venue_name": "named place without street number",
    "starts_at": "ISO8601 timestamptz",
    "ends_at": "ISO8601 timestamptz or null",
    "duration_minutes": 90,
    "max_attendees": 12,
    "cohort_tags": ["coffee_stroller"],
    "missing": []
  }},
  "mapped_summary": "One sentence preview of the scheduled event",
  "spans": [
    { "text": "exact phrase from host words", "bucket": "time" }
  ],
  "assistant_message": "Short warm closing — event is ready to publish"
}}

Rules:
- title required; infer from story if needed
- venue_name: named place or area, never street address — and NEVER a bare ZIP code
  (a ZIP like "34786" is an area, not a meeting spot; leave venue_name null)
- TODAY is {today}. Resolve relative dates against it: "tomorrow" is exactly TODAY + 1
  day; a bare weekday is the SOONEST such future day (if TODAY is Wednesday,
  "thursday" is tomorrow, never next week)
- starts_at / ends_at: strict ISO 8601 ("2026-07-09T07:00:00") or null — NEVER prose
  like "next Wednesday 16:00:00"
- a recurring cadence ("MWF", "wednesdays", "every tuesday") is NOT a reason to blank
  the draft: still extract title/venue/time from the host's words, and set starts_at to
  the first future occurrence (ISO) or null
- cohort_tags: 0-3 ids from allowed list only
- spans: 3-10 highlight phrases for UI coloring
- NEVER invent events the host did not describe

Transcript:
"""


def event_extract_prompt(purpose_ids: list[str]) -> str:
    """The extract prompt with its placeholders filled — purpose ids and TODAY anchored
    to the host's local day (see event_when.event_local_now), so relative dates in the
    transcript ground on the day the host actually typed them."""
    from app.event_when import event_local_now

    return EVENT_EXTRACT_PROMPT.replace(
        "{purpose_ids}", ", ".join(purpose_ids) or "see get_event_purposes"
    ).replace("{today}", event_local_now().strftime("%A, %Y-%m-%d"))


def _vertex_client():
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    return genai.Client(vertexai=True, project=project, location=location)


def vertex_extract_event_from_transcript(
    transcript: str,
    *,
    purpose_ids: list[str],
    previous_draft: dict[str, Any] | None = None,
) -> tuple[EventDraft, str, str | None, list[MappedSpan]]:
    client = _vertex_client()
    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    from google.genai import types

    prompt = event_extract_prompt(purpose_ids)
    response = client.models.generate_content(
        model=model,
        contents=prompt + transcript.strip(),
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    data = parse_json_object(response.text or "")
    return parse_event_extract_data(data, purpose_ids=purpose_ids, previous_draft=previous_draft)


def parse_event_extract_data(
    data: Any,
    *,
    purpose_ids: list[str],
    previous_draft: dict[str, Any] | None = None,
) -> tuple[EventDraft, str, str | None, list[MappedSpan]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_extract_json")
    valid = set(purpose_ids)
    incoming = parse_event_draft(data.get("event_draft"), valid_purpose_ids=valid)
    merged = merge_event_drafts(previous_draft, incoming)
    draft = EventDraft(**merged)

    if not draft.title:
        raise ValueError("event_title_required")

    closing = str(data.get("assistant_message", "")).strip()[:800]
    if not closing:
        closing = "Your event looks great — review the details and publish when you're ready."

    mapped_summary = str(data.get("mapped_summary", "")).strip()[:800] or None
    spans_raw = data.get("spans") or []
    spans: list[MappedSpan] = []
    if isinstance(spans_raw, list):
        for item in spans_raw[:12]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()[:160]
            if not text:
                continue
            bucket = str(item.get("bucket", "activity")).strip().lower()[:32]
            spans.append(MappedSpan(text=text, bucket=bucket))

    if not mapped_summary and spans:
        mapped_summary = ", ".join(s.text for s in spans[:6])

    return draft, closing, mapped_summary, spans
