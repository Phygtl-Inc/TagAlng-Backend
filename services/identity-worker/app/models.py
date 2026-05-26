from pydantic import BaseModel, Field


class ClarificationAnswer(BaseModel):
    question_id: str = Field(..., min_length=1, max_length=64)
    question: str = Field(..., min_length=1, max_length=500)
    answer: str = Field(..., min_length=1, max_length=1000)


class ExtractRequest(BaseModel):
    cover_text: str = Field(..., min_length=8, max_length=4000)


class IntakeRequest(BaseModel):
    """ChatGPT-style intake: first message may trigger follow-up questions."""

    cover_text: str = Field(..., min_length=8, max_length=4000)
    clarifications: list[ClarificationAnswer] = Field(default_factory=list)


class FollowUpQuestion(BaseModel):
    id: str
    prompt: str


class ExtractedClaim(BaseModel):
    concept: str
    label: str
    tone: str | None = None
    confidence: float = Field(..., ge=0, le=1)
    disclosure: str = "public"
    synonyms: list[str] = Field(default_factory=list)


class ExtractResponse(BaseModel):
    user_id: str
    claims: list[ExtractedClaim]
    threads_found: int
    mode: str


class IntakeResponse(BaseModel):
    user_id: str
    status: str  # clarify | complete
    assistant_message: str
    questions: list[FollowUpQuestion] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)
    threads_found: int = 0
    mode: str = "vertex"
