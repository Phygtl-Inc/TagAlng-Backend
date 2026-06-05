import json
import os
import re
from typing import Any

from app.lana_ui import normalize_bucket, parse_mapped_spans
from app.models import ExtractedClaim, MappedSpan

EXTRACT_PROMPT = """You are an identity extraction model for TagAlng, a block-based social app.

Read the full conversation transcript between Lana and the user. Extract identity "threads" — by meaning, not keyword matching.

Output ONLY valid JSON (no markdown):
{
  "mapped_summary": "One warm sentence summarizing the user (comma-separated phrases, natural prose)",
  "spans": [
    {
      "text": "exact phrase from user words",
      "bucket": "heritage" | "stage" | "vicinity" | "faith" | "activity" | "interest" | "general",
      "claim_concept": "matching claim slug or empty"
    }
  ],
  "claims": [
    {
      "concept": "snake_case_slug",
      "label": "Short UI card title",
      "tone": "optional",
      "confidence": 0.0-1.0,
      "disclosure": "public" | "mutual" | "private",
      "synonyms": ["≈ style tags, max 4"],
      "source_quote": "exact short quote from user story for From '...' UI",
      "bucket": "heritage" | "stage" | "vicinity" | "faith" | "activity" | "interest" | "general"
    }
  ],
  "assistant_message": "Short warm closing line"
}

Rules:
- Max 8 claims; only what the user expressed or clearly implied
- Every claim MUST have source_quote (verbatim or tight paraphrase from user) and bucket
- spans: 3-8 phrases covering the mapped_summary for frontend color highlights
- concept must match ^[a-z][a-z0-9_]{1,63}$
- NEVER extract race, exact age, sex/gender demographics, street address
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"

Transcript:
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
    data = json.loads((response.text or "{}").strip())
    return parse_profile_extract_data(data)


def vertex_embed(text: str, dim: int = 768) -> list[float]:
    client = _vertex_client()
    model = os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005")
    result = client.models.embed_content(model=model, contents=text)
    values = result.embeddings[0].values
    if len(values) != dim:
        raise ValueError(f"expected_{dim}_dims_got_{len(values)}")
    return list(values)
