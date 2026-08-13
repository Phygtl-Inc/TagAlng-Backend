"""Session complete extract via orchestrator LLM."""

from typing import Any

from app.models import EventDraft, ExtractedClaim, MappedSpan
from app.orchestrator.llm import extractor_model, llm_json
from app.vertex_event_extract import EVENT_EXTRACT_PROMPT, parse_event_extract_data
from app.vertex_extract import EXTRACT_PROMPT, parse_profile_extract_data


def _extract_model() -> str:
    """Provider-correct model for the /complete extract — see llm.extractor_model.

    Kept as a thin alias so both extraction paths (this one and the per-turn
    incremental claims/circles pass) resolve through ONE definition; they drifted
    apart once already, leaving the per-turn pass on the router tier.
    """
    return extractor_model()


def claude_extract_profile_from_transcript(
    transcript: str,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    data = llm_json(
        model=_extract_model(),
        system=(
            "You extract structured identity claims from TagAlng profile intake transcripts. "
            "Output only valid JSON. Keep strings on one line. Escape quotes inside strings."
        ),
        user_payload=EXTRACT_PROMPT + transcript.strip(),
        max_tokens=4096,
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
    data = llm_json(
        model=_extract_model(),
        system=(
            "You extract structured event drafts from TagAlng host transcripts. "
            "Output only valid JSON. Keep strings on one line."
        ),
        user_payload=prompt + transcript.strip(),
        max_tokens=4096,
        temperature=0.2,
    )
    return parse_event_extract_data(
        data,
        purpose_ids=purpose_ids,
        previous_draft=previous_draft,
    )
