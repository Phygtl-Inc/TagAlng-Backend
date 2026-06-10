from typing import Literal

from pydantic import BaseModel, Field


class HighlightSpan(BaseModel):
    text: str
    bucket: str = "general"


class LanaTurnUi(BaseModel):
    bucket: str | None = None
    focus_phrase: str | None = None
    highlights: list[HighlightSpan] = Field(default_factory=list)


class JointMomentCandidate(BaseModel):
    user_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None


class PeerMatchRow(BaseModel):
    peer_user_id: str | None = None
    nickname: str | None = None
    avatar_url: str | None = None
    similarity_score: float | None = None
    matching_peer_label: str | None = None
    matching_peer_concept: str | None = None
    has_exact_concept_match: bool = False


class JointMomentPayload(BaseModel):
    joint_moment_id: str | None = None
    status: str | None = None
    candidate: JointMomentCandidate | None = None
    lana_copy: str | None = None
    match_reason: str | None = None
    is_demo: bool = False


class MappedSpan(BaseModel):
    text: str
    bucket: str = "general"
    claim_concept: str | None = None


class EventDraft(BaseModel):
    title: str | None = None
    description: str | None = None
    venue_name: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    duration_minutes: int | None = None
    max_attendees: int | None = None
    cohort_tags: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class CreateSessionRequest(BaseModel):
    purpose: Literal["profile_intake", "event_draft"] = "profile_intake"


class CreateSessionResponse(BaseModel):
    session_id: str
    purpose: str
    status: str
    assistant_message: str
    ready_to_complete: bool = False
    ui: LanaTurnUi = Field(default_factory=LanaTurnUi)
    event_draft: EventDraft | None = None
    orchestrator: bool = False
    timing_ms: dict[str, int] | None = None
    is_anonymous: bool = False
    phone_verified: bool = False
    home_block_assigned: bool = False
    onboarding_step: str | None = None
    requires_phone_verification: bool = False
    joint_moment: JointMomentPayload | None = None
    peer_matches: list[PeerMatchRow] = Field(default_factory=list)


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class TurnRouting(BaseModel):
    outcome: str | None = None
    intent_class: str | None = None
    confidence: float | None = None
    tool_called: str | None = None
    capture_fired: bool = False


class SendMessageResponse(BaseModel):
    session_id: str
    status: str
    assistant_message: str
    ready_to_complete: bool = False
    message_count: int = 0
    ui: LanaTurnUi = Field(default_factory=LanaTurnUi)
    event_draft: EventDraft | None = None
    routing: TurnRouting | None = None
    orchestrator: bool = False
    timing_ms: dict[str, int] | None = None
    onboarding_step: str | None = None
    requires_phone_verification: bool = False
    joint_moment: JointMomentPayload | None = None
    phone_verified: bool = False
    home_block_assigned: bool = False
    peer_matches: list[PeerMatchRow] = Field(default_factory=list)


class CompleteSessionRequest(BaseModel):
    force: bool = False
    publish: bool = True


class ExtractedClaim(BaseModel):
    concept: str
    label: str
    tone: str | None = None
    confidence: float = Field(..., ge=0, le=1)
    disclosure: str = "public"
    synonyms: list[str] = Field(default_factory=list)
    source_quote: str | None = None
    bucket: str | None = None


class CompleteSessionResponse(BaseModel):
    session_id: str
    status: str
    assistant_message: str
    claims: list[ExtractedClaim] = Field(default_factory=list)
    threads_found: int = 0
    mapped_summary: str | None = None
    spans: list[MappedSpan] = Field(default_factory=list)
    event_id: str | None = None
    event_draft: EventDraft | None = None
    published: bool = False


class SessionDetailResponse(BaseModel):
    session_id: str
    purpose: str
    status: str
    context: dict = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list)
    mapped_summary: str | None = None
    spans: list[MappedSpan] = Field(default_factory=list)
    event_draft: EventDraft | None = None
