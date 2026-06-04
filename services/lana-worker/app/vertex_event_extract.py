import json
import os
from typing import Any

from app.lana_ui import merge_event_drafts, parse_event_draft
from app.models import EventDraft, MappedSpan

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
    {{ "text": "exact phrase from host words", "bucket": "time" | "venue" | "audience" | "activity" | "constraint" | "capacity" | "purpose" }}
  ],
  "assistant_message": "Short warm closing — event is ready to publish"
}}

Rules:
- title required; infer from story if needed
- venue_name: named place or area, never street address
- cohort_tags: 0-3 ids from allowed list only
- spans: 3-10 highlight phrases for UI coloring
- NEVER invent events the host did not describe

Transcript:
"""


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

    prompt = EVENT_EXTRACT_PROMPT.replace("{purpose_ids}", ", ".join(purpose_ids) or "see get_event_purposes")
    response = client.models.generate_content(
        model=model,
        contents=prompt + transcript.strip(),
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    data = json.loads((response.text or "{}").strip())
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
