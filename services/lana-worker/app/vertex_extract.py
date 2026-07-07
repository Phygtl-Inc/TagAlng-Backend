import os
import re
from typing import Any

from app.lana_ui import normalize_bucket, parse_mapped_spans
from app.models import ExtractedClaim, MappedSpan
from app.orchestrator.json_util import parse_json_object

EXTRACT_PROMPT = """You are an identity extraction model for TagAlng, a block-based social app.

Read the full conversation transcript between Lana and the user. Extract identity "threads" — by meaning, not keyword matching.

Output ONLY valid JSON (no markdown):
{
  "mapped_summary": "One warm sentence summarizing the user",
  "spans": [
    {
      "text": "exact phrase from user words",
      "bucket": "heritage",
      "claim_concept": "latino_heritage"
    }
  ],
  "claims": [
    {
      "concept": "snake_case_slug",
      "label": "Short UI card title",
      "tone": "optional",
      "confidence": 0.85,
      "disclosure": "public",
      "synonyms": ["tag1"],
      "source_quote": "exact short quote from user",
      "bucket": "heritage",
      "transient": false
    }
  ],
  "assistant_message": "Short warm closing line"
}

Allowed bucket values: heritage, stage, vicinity, faith, activity, interest, general.
Allowed disclosure: public, mutual, private.

Rules:
- Max 8 claims; only what the user expressed or clearly implied
- ONE claim per distinct thread — NEVER emit the same thread twice with different wording, bucket, or synonyms (e.g. do NOT list "Interested in Hosting Neighbor Meetings" five times). Merge them into a single claim.
- NEVER emit a claim that is only a bare topic label ("Health", "Wellness", "Lifestyle", "General") or that expresses uncertainty ("Unsure what to call", "Not sure about time"). Skip these entirely — they are not threads.
- "transient": true for TEMPORARY states that are NOT durable identity — an injury or illness ("sprained ankle"), an upcoming trip/vacation, a passing mood. Durable identity (heritage, life stage, ongoing interests, occupation, faith) is transient=false. When in doubt, false.
- Capture languages spoken as one claim (bucket "interest", e.g. "Speaks 7 languages")
- Every claim MUST have source_quote (verbatim or tight paraphrase from user) and bucket
- synonyms: 3-6 lowercase tags per claim — include broader/related terms, not just the literal word (e.g. "sicilian" → ["sicilian","italian","mediterranean"])
- spans: 3-8 phrases covering the mapped_summary for frontend color highlights
- concept must match ^[a-z][a-z0-9_]{1,63}$
- NEVER extract race, exact age, sex/gender demographics, street address
- NEVER make parenting/kids into a claim, and never capture a child's name, age, or school
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"

Transcript:
"""

INCREMENTAL_EXTRACT_PROMPT = """You extract identity threads from ONE user message in a TagAlng block chat, \
and you stay curious — after capturing what they said, you propose ONE warm follow-up that draws out more.

Output ONLY valid JSON (no markdown):
{
  "nickname": "first name neighbors should use, or null",
  "kids_count": null,
  "claims": [
    {
      "concept": "snake_case_slug",
      "label": "Short UI card title",
      "tone": "optional",
      "confidence": 0.85,
      "disclosure": "public",
      "synonyms": ["tag1", "tag2", "tag3"],
      "source_quote": "exact short quote from this message",
      "bucket": "heritage",
      "vague": false,
      "transient": false
    }
  ],
  "followup_question": "one warm question that adds a NEW MATCHABLE facet (never backstory), or null"
}

Allowed bucket values: heritage, stage, vicinity, faith, activity, interest, general.
Allowed disclosure: public, mutual, private.

Rules:
- Max 6 claims from this message only
- If no identity content (greetings, "ok", ZIP, phone), return {"nickname": null, "kids_count": null, "claims": [], "followup_question": null}
- Split distinct threads — capture EACH one, do not collapse (e.g. "pakistani dad, married 10 years, speak 5 languages, do triathlon" → pakistani_heritage + multilingual + married_ten_years + triathlon; "dad" and kid count go to kids_count, never a claim)
- Capture LANGUAGES spoken as one claim, bucket "interest" (e.g. "speak 7 languages" → concept "multilingual", label "Speaks 7 languages")
- Capture RELATIONSHIP status as a claim, bucket "stage" (e.g. "married 10 years" → concept "long_married", label "Married 10 years")
- Capture occupation/work as a claim, bucket "activity" or "interest" (e.g. "work in tech" → tech_worker). Mark it "vague": true when it is coarse and a specific would help (e.g. "tech worker", "athlete", "in finance")
- Every claim MUST have source_quote from this message and bucket
- concept must match ^[a-z][a-z0-9_]{1,63}$
- synonyms: 3-6 lowercase tags per claim — include BROADER and RELATED terms, not just the literal word (e.g. "sicilian" → ["sicilian","italian","mediterranean","sicily"]; "triathlon" → ["triathlon","endurance","running","cycling","swimming"]). These power match discovery.
- "vague": true when the claim is coarse enough that a follow-up would sharpen it — e.g. "tech worker", "athlete", "in finance", OR a COUNT without specifics ("speaks 5 languages" → vague until they name them, "plays sports" → which). false when already specific.
- "transient": true for TEMPORARY states that are NOT durable identity — an injury or illness ("sprained ankle", "got the flu"), an upcoming trip/vacation, a one-off plan, a passing mood ("feeling low-key this week"). Durable identity (heritage, life stage, ongoing interests, occupation, faith) is transient=false. When in doubt, false.
- NEVER emit a claim that is only a bare topic label ("Health", "Wellness", "Lifestyle", "General") or that expresses uncertainty ("Unsure what to call", "Not sure about time", "don't know"). Skip these entirely — they are not threads.
- Do NOT emit the SAME thread twice with different wording — one claim per distinct thread
- kids_count: an integer ONLY when the user states HOW MANY children they have ("2 sons" → 2, "three kids" → 3). null otherwise. This is private and never a claim. NEVER capture a child's name, age, gender, school, or photo — only the count.
- NEVER extract race, exact age, sex/gender demographics, street address
- NEVER extract negative or exclusion claims ("not Brazilian", "no Italian", "without X")
- NEVER make parenting/kids into a claim — only kids_count carries it
- ONLY extract first-person identity ("I am", "I'm", "my heritage") — NOT who they search for ("find Brazilian mom", "looking for Pakistani neighbors")
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"
- nickname only when user states their name ("I'm Brinda", "call me Sam", "my name is brigade")
- followup_question — this becomes a "By the way…" tile on her home screen; her answer is stored as an identity claim used to match her with nearby moms. So keep a warm neighborhood-concierge tone (not an interviewer) and only ask what genuinely helps her connect locally. Propose ONE only if it adds a MATCHABLE facet peer-matching would actually use; reason about what you ALREADY know (the claims above + existing threads) and target a GAP, never a repeat. Two allowed shapes: (1) SHARPEN a claim you marked "vague": true — vague tech_worker → "What kind of tech — engineering, product, design?"; "speaks 5 languages" → "Which five?". (2) FILL a matchable dimension you don't yet know — whether they do it WITH others / would want to nearby, their free-time rhythm, kids' ages or stage, a specific sub-interest, or where they're from / new-to-area — running → "When do you usually get out — mornings, weekends?". NEVER ask an origin-story or opinion question; rewrite it to a matchable one instead: ✗ "What got you into FIFA?" → ✓ "Do you catch matches solo, or want to watch with neighbors?"; ✗ "Why do you love Real Madrid?" → ✓ "Any local spot you like to watch at?"; ✗ "How did you learn Portuguese?" → ✓ "Which of your languages do you use most day-to-day?". Keep it short (<120 char), warm, OPEN, reference what they said. Return null when nothing is vague AND no matchable dimension is missing — filler is worse than silence. HARD RULE: also null for any sensitive or help-seeking topic — divorce / relationship trouble, health or medical, mental health or personal safety, money or debt, legal or immigration (handled elsewhere) — and when the message is a question aimed at you.

User message:
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
        source_quote = str(item.get("source_quote", "")).strip()[:160] or None
        bucket = normalize_bucket(item.get("bucket"))
        transient = bool(item.get("transient", False))
        vague = bool(item.get("vague", False))
        out.append(
            ExtractedClaim(
                concept=concept,
                label=label,
                tone=str(tone)[:40] if tone else None,
                confidence=conf,
                disclosure=disclosure,
                synonyms=syns,
                source_quote=source_quote,
                bucket=bucket,
                transient=transient,
                vague=vague,
            )
        )
    return out[:8]


def parse_profile_extract_data(
    data: Any,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    if not isinstance(data, dict):
        raise ValueError("invalid_extract_json")
    claims = _parse_claims(data)
    if not claims:
        raise ValueError("model_returned_no_valid_claims")
    closing = str(data.get("assistant_message", "")).strip()[:800]
    if not closing:
        closing = "Your profile threads are ready — neighbors on your block can get to know the real you."
    mapped_summary = str(data.get("mapped_summary", "")).strip()[:800] or None
    span_dicts = parse_mapped_spans(data.get("spans"))
    spans = [MappedSpan(**s) for s in span_dicts if s.get("text")]
    if not mapped_summary and spans:
        mapped_summary = ", ".join(s.text for s in spans[:6])
    return claims, closing, mapped_summary, spans


def _parse_kids_count(raw: Any) -> int | None:
    """An integer 1-20 only; None for anything else (never trust ages/years here)."""
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 20:
        return n
    return None


def _parse_followup_question(raw: Any) -> str | None:
    q = str(raw or "").strip()
    if not q or q.lower() in ("none", "null", "n/a"):
        return None
    return q[:160]


def parse_incremental_claims_data(
    data: Any,
) -> tuple[str | None, list[ExtractedClaim], int | None, str | None]:
    if not isinstance(data, dict):
        return None, [], None, None
    nickname_raw = data.get("nickname")
    nickname = str(nickname_raw).strip()[:30] if nickname_raw else None
    if nickname and len(nickname) < 2:
        nickname = None
    claims = _parse_claims(data)
    kids_count = _parse_kids_count(data.get("kids_count"))
    followup = _parse_followup_question(data.get("followup_question"))
    return nickname, claims[:6], kids_count, followup


def _existing_claims_block(existing_labels: list[str] | None) -> str:
    """Tell the extractor what's already on the profile so it merges, not duplicates."""
    labels = [str(s).strip() for s in (existing_labels or []) if str(s).strip()]
    if not labels:
        return ""
    return (
        "ALREADY ON PROFILE (do NOT create a new claim that duplicates or is a narrower "
        "version of any of these — e.g. if 'Speaks 10 languages' is listed, do NOT add "
        "'English Speaker'; only add genuinely NEW threads): "
        + "; ".join(labels[:40])
        + "\n\n"
    )


def vertex_extract_claims_from_utterance(
    message: str, existing_labels: list[str] | None = None
) -> Any:
    client = _vertex_client()
    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    from google.genai import types

    prompt = INCREMENTAL_EXTRACT_PROMPT + _existing_claims_block(existing_labels) + message.strip()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return parse_json_object(response.text or "")


def incremental_claims_from_utterance(
    message: str, existing_labels: list[str] | None = None
) -> Any:
    """Extract claims via orchestrator LLM when configured; else Vertex."""
    import logging

    log = logging.getLogger(__name__)
    text = str(message or "").strip()
    system = INCREMENTAL_EXTRACT_PROMPT + _existing_claims_block(existing_labels)
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            return llm_json(
                model=router_model(),
                system=system,
                user_payload=text,
                max_tokens=512,
                temperature=0.2,
            )
    except Exception:
        log.exception("llm_incremental_claim_extract_failed")
    return vertex_extract_claims_from_utterance(text, existing_labels)


def vertex_extract_from_transcript(
    transcript: str,
) -> tuple[list[ExtractedClaim], str, str | None, list[MappedSpan]]:
    client = _vertex_client()
    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
    from google.genai import types

    response = client.models.generate_content(
        model=model,
        contents=EXTRACT_PROMPT + transcript.strip(),
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    data = parse_json_object(response.text or "")
    return parse_profile_extract_data(data)


def vertex_embed(text: str, dim: int = 768) -> list[float]:
    client = _vertex_client()
    model = os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005")
    result = client.models.embed_content(model=model, contents=text)
    values = result.embeddings[0].values
    if len(values) != dim:
        raise ValueError(f"expected_{dim}_dims_got_{len(values)}")
    return list(values)
