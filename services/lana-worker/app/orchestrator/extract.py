"""Session complete extract via Claude Sonnet (orchestrator stack)."""

from typing import Any

from app.models import EventDraft, ExtractedClaim, MappedSpan
from app.orchestrator.claude import claude_json, synthesizer_model
from app.vertex_event_extract import EVENT_EXTRACT_PROMPT, parse_event_extract_data
from app.vertex_extract import EXTRACT_PROMPT, parse_profile_extract_data


def claude_extract_profile_from_transcript(
    transcript: str,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    data = claude_json(
        model=synthesizer_model(),
        system="You extract structured identity claims from TagAlng profile intake transcripts. Output only valid JSON.",
        user_payload=EXTRACT_PROMPT + transcript.strip(),
        max_tokens=2048,
        temperature=0.2,
    )
    return parse_profile_extract_data(data)


def claude_extract_event_from_transcript(
    transcript: str,
    *,
    purpose_ids: list[str],
    previous_draft: dict[str, Any] | None = None,
) -> tuple[EventDraft, str, str | None, list[MappedSpan]]:
    prompt = EVENT_EXTRACT_PROMPT.replace("{purpose_ids}", ", ".join(purpose_ids) or "see get_event_purposes")
    data = claude_json(
        model=synthesizer_model(),
        system="You extract structured event drafts from TagAlng host transcripts. Output only valid JSON.",
        user_payload=prompt + transcript.strip(),
        max_tokens=2048,
        temperature=0.2,
    )
    return parse_event_extract_data(
        data,
        purpose_ids=purpose_ids,
        previous_draft=previous_draft,
    )
