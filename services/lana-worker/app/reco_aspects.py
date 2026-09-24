"""Break a recommendation statement into its sections, then quantify each one in words.

THE MODEL (2026-09-24 standup)

    A statement carries no weight. "The atmosphere was compelling, I noticed the
    porcelain, the server spoke three languages and the owner came over" is four
    sections, and a single five-star rating on the whole thing is what lets someone say
    "this restaurant sucks" and give it five stars because a friend owns it.

    So: split it, then ask about each part, then read the ANSWER — not a slider —
    for its sentiment.

TWO RULES THAT ARE NOT NEGOTIABLE

    1. ONE QUESTION PER SECTION THEY RAISED. "If the user mentioned six sections within
       the statement, we need definitely six sections in the pipeline to be asked."
       We never ask about the porcelain because the category template has a porcelain
       field. We ask because they said porcelain.

    2. THE WORDS ARE THE PRODUCT. answer_verbatim is what another person reads. The
       sentiment band is internal and exists so Find can work — "somewhere the owner
       speaks Italian and the porcelain is unique" is an aspect query. Render the band
       and we have rebuilt stars.

WHAT THIS DOES NOT REPLACE
    reco_fields (the category question set) keeps running. It answers "what IS this
    place" — profession, hours, delivery. Aspects answer "what did YOU notice". Both are
    useful; only one of them can grow a vocabulary nobody designed in advance.

Defensive by contract: never raises into the request path.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# Bounded so a long voice note cannot produce a twenty-question interrogation. If someone
# genuinely raised more than this, we ask about the ones they dwelt on.
MAX_ASPECTS = 8
MIN_ASPECT_CONFIDENCE = 0.5

# Canonicalisation floor. Below this an aspect is new; above it, it is one we have seen.
# "the front desk" / "reception" / "the desk staff" must not be three aspects with n=1,
# or nothing ever accumulates and the count is always 1.
ASPECT_MERGE_SIMILARITY = 0.82

SPLIT_PROMPT = """You split ONE spoken recommendation into the distinct things the person \
actually commented on, so each can be asked about separately.

Output ONLY valid JSON (no markdown):
{
  "aspects": [
    {"label": "the owner", "key": "owner", "span": "the owner came over to the table",
     "confidence": 0.9}
  ]
}

Rules:
- ONE entry per distinct thing THEY raised. Not a checklist of what a place of this type
  usually has — if they did not mention parking, there is no parking aspect.
- "label" is how THEY framed it, so Lana can echo it back: "you mentioned the owner…".
  Keep their noun. Do not tidy "the lady at the front" into "reception staff".
- "key" is a short lowercase slug for matching across people: owner, porcelain,
  front_desk, wait_time, parking.
- "span" is the fragment of their words this came from. Every aspect must be traceable to
  something they said.
- Skip the subject itself. "Dr. Sarah is great with toddlers" about Dr. Sarah is ONE
  aspect (good with toddlers), not two.
- Skip pure sentiment with no object. "It was amazing" alone is not an aspect.
- At most {max_aspects}. If they raised more, keep the ones they said most about.
- Empty list is a valid answer.

Statement:
"""

BAND_PROMPT = """You read ONE answer about ONE aspect of a place and place it on a band.

You are NOT scoring quality. You are reading what this person's words mean about this
aspect, in their register. "Not bad" from someone understated is not the same as
"not bad" meaning mediocre — use the whole answer.

Output ONLY valid JSON:
{"sentiment": 2, "confidence": 0.8}

Bands:
   2  clearly positive, would recommend on this      "amazing and unique", "she was lovely"
   1  positive, mild                                 "fine", "no complaints"
   0  genuinely mixed or explicitly neutral          "good food, slow service"
  -1  negative, mild                                 "a bit much", "could be better"
  -2  clearly negative                               "really sucked", "never again"

Return null for sentiment if the answer does not actually address the aspect ("I don't
remember", "what do you mean"). Null is honest; 0 would claim we read neutrality where
we read nothing.

Aspect: {aspect}
Their answer: """


def split_statement(statement: str, *, subject_name: str | None = None) -> list[dict[str, Any]]:
    """The sections of one statement. [] on any failure — never raises."""
    text = (statement or "").strip()
    if len(text) < 12:
        return []

    payload = text if not subject_name else f"(about: {subject_name})\n{text}"
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=SPLIT_PROMPT.format(max_aspects=MAX_ASPECTS),
                user_payload=payload,
                max_tokens=640,
                temperature=0.2,
            )
            return _parse_aspects(data)
    except Exception:
        logger.exception("reco_aspects: split failed")

    try:
        import os

        from app.orchestrator.llm import vertex_generate_json

        return _parse_aspects(
            vertex_generate_json(
                model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
                system=None,
                user_payload=SPLIT_PROMPT.format(max_aspects=MAX_ASPECTS) + payload,
                max_tokens=640,
                temperature=0.2,
            )
        )
    except Exception:
        logger.exception("reco_aspects: split fallback failed")
        return []


def _parse_aspects(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("aspects")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        key = _slug(str(item.get("key") or label))
        if not label or not key or key in seen:
            continue
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        except (TypeError, ValueError):
            conf = 0.8
        if conf < MIN_ASPECT_CONFIDENCE:
            continue
        seen.add(key)
        out.append({
            "aspect_key": key,
            "aspect_label": label,
            "source_span": str(item.get("span") or "").strip()[:300] or None,
            "confidence": conf,
        })
        if len(out) >= MAX_ASPECTS:
            break
    return out


def _slug(value: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "_", (value or "").lower().strip()).strip("_")
    return s[:48]


def band_answer(aspect_label: str, answer: str) -> tuple[int | None, float]:
    """Read one answer onto -2..+2. (None, 0.0) when it does not address the aspect."""
    text = (answer or "").strip()
    if not text:
        return None, 0.0
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=BAND_PROMPT.format(aspect=aspect_label),
                user_payload=text,
                max_tokens=64,
                temperature=0.0,
            )
            return _parse_band(data)
    except Exception:
        logger.exception("reco_aspects: band failed")
    return None, 0.0


def _parse_band(data: Any) -> tuple[int | None, float]:
    if not isinstance(data, dict):
        return None, 0.0
    raw = data.get("sentiment")
    if raw is None:
        return None, 0.0
    try:
        band = int(raw)
    except (TypeError, ValueError):
        return None, 0.0
    if band < -2 or band > 2:
        return None, 0.0
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.7))))
    except (TypeError, ValueError):
        conf = 0.7
    return band, conf


# ── canonicalisation ────────────────────────────────────────────────────────

def canonical_key(subject_ref: str | None, aspect_key: str, aspect_label: str) -> str:
    """Reuse an aspect key this subject already has when the meaning matches.

    Without this, "the front desk", "reception" and "the desk staff" are three aspects at
    n=1 and the count that makes the whole thing worth reading never rises above one.
    """
    if not subject_ref:
        return aspect_key
    try:
        from app.vec_util import to_pgvector
        from app.vertex_extract import vertex_embed

        vec = vertex_embed(f"{aspect_label} ({aspect_key})")
        if not vec:
            return aspect_key
        res = service_client().rpc(
            "match_reco_aspect_key",
            {
                "p_subject_ref": subject_ref,
                "p_embedding": to_pgvector(vec),
                "p_min_similarity": ASPECT_MERGE_SIMILARITY,
            },
        ).execute()
        rows = res.data or []
        if rows and rows[0].get("aspect_key"):
            return str(rows[0]["aspect_key"])
    except Exception:
        # No RPC yet, or a transient failure. Falling back to the raw key fragments the
        # vocabulary rather than merging two different things — the safe direction.
        logger.debug("reco_aspects: canonicalisation unavailable, using raw key")
    return aspect_key


# ── persistence ─────────────────────────────────────────────────────────────

def record_aspect(
    *,
    signal_id: str,
    subject_ref: str | None,
    author_id: str,
    aspect_key: str,
    aspect_label: str,
    source_span: str | None,
    answer_verbatim: str | None,
    answer_source: str = "voice",
) -> dict[str, Any] | None:
    """Store one answered (or skipped) aspect. Never raises.

    A SKIPPED aspect is stored, deliberately. That someone raised the front desk and then
    declined to grade it still says the front desk is salient here — and it stops us
    asking again on the next pass.
    """
    sentiment: int | None = None
    confidence = 0.0
    if answer_source != "skipped" and answer_verbatim:
        sentiment, confidence = band_answer(aspect_label, answer_verbatim)

    row: dict[str, Any] = {
        "signal_id": signal_id,
        "subject_ref": subject_ref,
        "author_id": author_id,
        "aspect_key": canonical_key(subject_ref, aspect_key, aspect_label),
        "aspect_label": aspect_label,
        "source_span": source_span,
        "answer_verbatim": answer_verbatim,
        "answer_source": answer_source,
        "sentiment": sentiment,
        "sentiment_confidence": confidence or None,
    }
    try:
        from app.vec_util import to_pgvector
        from app.vertex_extract import vertex_embed

        vec = vertex_embed(f"{aspect_label}: {answer_verbatim or ''}".strip())
        if vec:
            row["embedding"] = to_pgvector(vec)
    except Exception:
        logger.debug("reco_aspects: embed failed, storing without")

    try:
        res = (
            service_client()
            .table("reco_aspect")
            .upsert(row, on_conflict="signal_id,aspect_key")
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        logger.exception("reco_aspects: write failed for signal=%s", signal_id)
        return None


def open_aspect_questions(
    *, signal_id: str, subject_ref: str | None, author_id: str, statement: str,
    subject_name: str | None = None,
) -> list[dict[str, Any]]:
    """Split the statement and return the questions to ask, in order.

    Returns [{aspect_key, aspect_label, source_span, question}]. The caller asks them one
    at a time and calls record_aspect per answer.

    SKIP IS AVAILABLE AT BOTH LEVELS — per question and for the whole round. Flagged as
    missing in the 2026-09-24 review: the UI offers it only at the start.
    """
    aspects = split_statement(statement, subject_name=subject_name)
    if not aspects:
        return []
    out: list[dict[str, Any]] = []
    for a in aspects:
        out.append({
            "aspect_key": canonical_key(subject_ref, a["aspect_key"], a["aspect_label"]),
            "aspect_label": a["aspect_label"],
            "source_span": a["source_span"],
            # Lana echoes their own words back. "You mentioned the owner — how was it?"
            # is answerable; "Rate the ownership experience" is a form.
            "question": f"You mentioned {a['aspect_label']} — how was it?",
            "skippable": True,
        })
    logger.info(
        "reco_aspects: %d question(s) for signal=%s: %s",
        len(out), signal_id, [q["aspect_key"] for q in out],
    )
    return out
