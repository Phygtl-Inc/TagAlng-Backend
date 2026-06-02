import json
import os
import re
from typing import Any

from app.models import ClarificationAnswer, ExtractedClaim, FollowUpQuestion

INTAKE_PROMPT = """You are TagAlng's identity intake assistant (warm, concise, like a thoughtful friend — not a form).

The user describes themselves in their own words. Your job:
1. Decide if you have ENOUGH to extract meaningful identity threads for matching neighbors on their block.
2. If NOT enough — ask 1–3 short follow-up questions (never ask about race, exact age, sex, or street address).
3. If ENOUGH — extract claims OR, if clarifications were provided, use those too.

Examples of when to ask:
- "I'm a mom" but no kids ages → ask how many kids and rough ages/life stage
- "I'm new here" but no sense of when → ask how recently they moved
- "I like sports" but no sport → ask which activities
- Vague faith mention → optional: parish/tradition level (mutual disclosure applies)

Output ONLY valid JSON:
{
  "status": "clarify" | "complete",
  "assistant_message": "Short friendly reply to the user",
  "questions": [{"id": "snake_case_id", "prompt": "question text"}],
  "claims": []
}

If status is "complete", include 3–8 claims in "claims" (same schema as below) and questions may be [].
If status is "clarify", claims must be [] and include 1–3 questions.

Each claim when complete:
{
  "concept": "snake_case",
  "label": "UI label",
  "tone": "optional",
  "confidence": 0.0-1.0,
  "disclosure": "public" | "mutual" | "private",
  "synonyms": ["semantic equivalents", "max 4"]
}

Rules:
- NEVER extract race, exact age, sex demographics, or street address
- Faith, sobriety, LGBTQ+: disclosure MUST be "mutual"
- concept must match ^[a-z][a-z0-9_]{1,63}$
- Be inclusive; infer meaning semantically (Sicilian ≈ Italian heritage)

"""

MUTUAL_CONCEPT_MARKERS = (
    "faith",
    "catholic",
    "muslim",
    "jewish",
    "christian",
    "church",
    "mosque",
    "synagogue",
    "sober",
    "recovery",
    "lgbtq",
    "queer",
)


def _vertex_client():
    project = os.environ.get("GCP_VERTEX_PROJECT", "")
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-central1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    return genai.Client(vertexai=True, project=project, location=location)


def _build_user_payload(cover_text: str, clarifications: list[ClarificationAnswer]) -> str:
    parts = [f"COVER TEXT:\n{cover_text.strip()}"]
    if clarifications:
        parts.append("\nCLARIFICATIONS (user already answered):")
        for c in clarifications:
            parts.append(f"- [{c.question_id}] Q: {c.question}\n  A: {c.answer}")
    return "\n".join(parts)


def _parse_questions(raw: Any) -> list[FollowUpQuestion]:
    if not isinstance(raw, list):
        return []
    out: list[FollowUpQuestion] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id", "")).strip().lower()
        prompt = str(item.get("prompt", "")).strip()
        if re.match(r"^[a-z][a-z0-9_]{1,63}$", qid) and prompt:
            out.append(FollowUpQuestion(id=qid, prompt=prompt[:500]))
    return out[:3]


def _parse_claims(data: Any) -> list[ExtractedClaim]:
    if not isinstance(data, dict):
        return []
    raw = data.get("claims", [])
    if not isinstance(raw, list):
        return []
    out: list[ExtractedClaim] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept", "")).strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]{1,63}$", concept):
            continue
        label = str(item.get("label", concept)).strip()[:120]
        disclosure = str(item.get("disclosure", "public"))
        if disclosure not in ("public", "mutual", "private"):
            disclosure = "public"
        if any(m in concept for m in MUTUAL_CONCEPT_MARKERS):
            disclosure = "mutual"
        conf = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        syns = item.get("synonyms", [])
        if not isinstance(syns, list):
            syns = []
        syns = [str(s)[:80] for s in syns[:4]]
        tone = item.get("tone")
        out.append(
            ExtractedClaim(
                concept=concept,
                label=label,
                tone=str(tone)[:40] if tone else None,
                confidence=conf,
                disclosure=disclosure,
                synonyms=syns,
            )
        )
    return out[:8]


def vertex_intake(
    cover_text: str,
    clarifications: list[ClarificationAnswer] | None = None,
) -> tuple[str, str, list[FollowUpQuestion], list[ExtractedClaim]]:
    """
    Returns: status, assistant_message, questions, claims
    """
    client = _vertex_client()
    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    from google.genai import types

    user_payload = _build_user_payload(cover_text, clarifications or [])
    response = client.models.generate_content(
        model=model,
        contents=INTAKE_PROMPT + "\n\n" + user_payload,
        config=types.GenerateContentConfig(
            temperature=0.35,
            response_mime_type="application/json",
        ),
    )
    data = json.loads((response.text or "{}").strip())
    status = str(data.get("status", "clarify")).lower()
    if status not in ("clarify", "complete"):
        status = "clarify"
    assistant_message = str(data.get("assistant_message", "")).strip()[:800]
    if not assistant_message:
        assistant_message = (
            "Tell me a bit more so I can find your people on the block."
            if status == "clarify"
            else "Here's what I heard — building your profile."
        )
    questions = _parse_questions(data.get("questions"))
    claims = _parse_claims(data)

    # If user sent clarifications, prefer completing unless model still needs more
    if clarifications and status == "clarify" and not claims:
        # Second pass: nudge to complete
        retry_payload = user_payload + "\n\nYou have clarifications now. Prefer status complete with claims."
        response2 = client.models.generate_content(
            model=model,
            contents=INTAKE_PROMPT + "\n\n" + retry_payload,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        data2 = json.loads((response2.text or "{}").strip())
        status = str(data2.get("status", "complete")).lower()
        claims = _parse_claims(data2)
        if data2.get("assistant_message"):
            assistant_message = str(data2["assistant_message"]).strip()[:800]
        questions = _parse_questions(data2.get("questions"))

    if status == "complete" and not claims:
        raise ValueError("model_complete_without_claims")

    return status, assistant_message, questions, claims
