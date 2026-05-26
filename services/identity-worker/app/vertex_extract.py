import json
import os
import re
from typing import Any

from app.models import ExtractedClaim

# TagAlng spec: Gemini Flash for extract, text-embedding-005 for vectors
EXTRACT_PROMPT = """You are an identity extraction model for TagAlng, a block-based social app.

Read the user's self-description and extract identity "threads" they are expressing — by meaning, not by keyword matching.

Output ONLY valid JSON (no markdown):
{
  "claims": [
    {
      "concept": "snake_case_slug",
      "label": "Short UI label",
      "tone": "optional warm/neutral/etc",
      "confidence": 0.0-1.0,
      "disclosure": "public" | "mutual" | "private",
      "synonyms": ["phrases that mean the same thing", "max 4"]
    }
  ]
}

Rules:
- Infer semantic meaning: e.g. "Sicilian-American" → synonyms like "Italian heritage", "Italo-American"
- Max 8 claims; only what the user actually expressed or clearly implied
- concept must match ^[a-z][a-z0-9_]{1,63}$
- NEVER extract or infer: race, ethnicity used as race, exact age, sex/gender demographics, street address
- Faith, religion, sobriety, recovery, LGBTQ+: disclosure MUST be "mutual"
- Life stage, neighborhood-newcomer, hobbies, parenting stage, profession vibe are OK as public unless sensitive above

User text:
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
    location = os.environ.get("GCP_VERTEX_LOCATION", "us-east1")
    if not project:
        raise RuntimeError("GCP_VERTEX_PROJECT not set")
    from google import genai

    return genai.Client(vertexai=True, project=project, location=location), location


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
        conf = float(item.get("confidence", 0.8))
        conf = max(0.0, min(1.0, conf))
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


def vertex_extract_claims(cover_text: str) -> list[ExtractedClaim]:
    client, location = _vertex_client()
    model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.0-flash-001")

    from google.genai import types

    response = client.models.generate_content(
        model=model,
        contents=EXTRACT_PROMPT + cover_text,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    text = (response.text or "{}").strip()
    data = json.loads(text)
    claims = _parse_claims(data)
    if not claims:
        raise ValueError("model_returned_no_valid_claims")
    return claims


def vertex_embed(text: str, dim: int = 768) -> list[float]:
    client, _ = _vertex_client()
    model = os.environ.get("VERTEX_EMBED_MODEL", "text-embedding-005")

    result = client.models.embed_content(model=model, contents=text)
    values = result.embeddings[0].values
    if len(values) != dim:
        raise ValueError(f"expected_{dim}_dims_got_{len(values)}")
    return list(values)
