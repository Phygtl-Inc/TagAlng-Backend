"""Break a recommendation statement into its sections, then quantify each one in words.

THE MODEL (2026-09-24 standup)

    A statement carries no weight. "The atmosphere was compelling, I noticed the
    porcelain, the server spoke three languages and the owner came over" is four
    sections, and a single five-star rating on the whole thing is what lets someone say
    "this restaurant sucks" and give it five stars because a friend owns it.

    So: split it, then ask about each part, then read the ANSWER — not a slider —
    for its sentiment.

THE RULES (locked 2026-09-24, do not relitigate in review)

    1. ONE QUESTION PER SECTION THEY RAISED. "If the user mentioned six sections within
       the statement, we need definitely six sections in the pipeline to be asked."
       We never ask about the porcelain because the category template has a porcelain
       field. We ask because they said porcelain.

    2. THEIR SECTIONS ONLY. reco_fields is NOT appended to this round. It keeps running
       for a different question — what the subject IS (hours, delivery, profession) —
       but six mentioned means six asked, not six plus four.

    3. FREE WORDS ONLY. No chip tray, no suggested-word list, no scale. The method rests
       on their vocabulary and a fixed chip set is a star rating wearing words.

    4. THE WORDS ARE THE PRODUCT. answer_verbatim is what another person reads. The
       sentiment band is internal and exists so Find can work. Render the band and we
       have rebuilt stars.

    5. SKIP AT BOTH LEVELS — one question, or the whole round. A skip is STORED.

    6. PARTIAL ROUNDS SURVIVE. Two of six answered and they leave: the two are live, the
       four stay 'open' and Lana re-offers them later. Open != skipped — skipped is a
       decision, open is an unfinished round.

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
#
# CALIBRATE BEFORE MERGE. This number is a guess and it is load-bearing for Find.
ASPECT_MERGE_SIMILARITY = 0.82

# How many open aspects Lana re-offers in one sitting. Small: this is a warm nudge on
# something they chose to talk about, not a queue to clear.
REOFFER_BATCH = 3

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

# Find side. "Somewhere the owner speaks Italian and the porcelain is unique" is TWO
# requirements, and the right answer satisfies both. One averaged embedding returns a
# place that is vaguely Italian-ish and vaguely nice — which is what every existing
# search already does, and why none of them can answer this.
SPLIT_QUERY_PROMPT = """You split ONE search request into the separate requirements it \
contains, so each can be matched independently.

Output ONLY valid JSON:
{
  "clauses": [
    {"text": "the owner speaks Italian", "aspect_hint": "owner"},
    {"text": "the porcelain is unique",  "aspect_hint": "porcelain"}
  ],
  "subject_kind": "restaurant"
}

Rules:
- ONE clause per requirement. "A quiet cafe with good wifi where the barista knows you"
  is three.
- Keep the clause in the asker's words. Do not normalise "the owner speaks Italian" into
  "multilingual staff" — the words have to match how someone else would have SAID it.
- "aspect_hint" is a slug guess, optional, best-effort.
- "subject_kind" is what they are looking for, if stated. Null if not.
- Drop location and time constraints — those are handled before this runs.
- One clause is a valid answer. Zero means there is nothing aspect-shaped here.

Request:
"""


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


# ── the round ───────────────────────────────────────────────────────────────

def open_aspect_questions(
    *, signal_id: str, subject_ref: str | None, author_id: str, statement: str,
    subject_name: str | None = None, persist: bool = True,
) -> list[dict[str, Any]]:
    """Split the statement, PERSIST one open row per section, return the questions.

    Persisting up front is what makes a partial round survive (rule 6). If they answer
    two and close the app, the other four already exist as 'open' and
    reoffer_open_aspects() can bring them back — the sections they raised are not lost
    because they got tired.

    Returns [{aspect_key, aspect_label, source_span, question, skippable}]. The caller
    asks one at a time and calls record_aspect per answer or per skip.
    """
    aspects = split_statement(statement, subject_name=subject_name)
    if not aspects:
        return []

    out: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for a in aspects:
        key = canonical_key(subject_ref, a["aspect_key"], a["aspect_label"])
        out.append({
            "aspect_key": key,
            "aspect_label": a["aspect_label"],
            "source_span": a["source_span"],
            # Lana echoes their own words back. "You mentioned the owner — how was it?"
            # is answerable; "Rate the ownership experience" is a form.
            "question": f"You mentioned {a['aspect_label']} — how was it?",
            # Rule 5. Per-question skip, alongside the whole-round skip.
            "skippable": True,
        })
        rows.append({
            "signal_id": signal_id,
            "subject_ref": subject_ref,
            "author_id": author_id,
            "aspect_key": key,
            "aspect_label": a["aspect_label"],
            "source_span": a["source_span"],
            "answer_source": "open",
        })

    if persist and rows:
        try:
            service_client().table("reco_aspect").upsert(
                rows, on_conflict="signal_id,aspect_key", ignore_duplicates=True
            ).execute()
        except Exception:
            # The round still works; only the re-offer safety net is lost.
            logger.exception("reco_aspects: could not persist open rows for %s", signal_id)

    logger.info(
        "reco_aspects: %d question(s) for signal=%s: %s",
        len(out), signal_id, [q["aspect_key"] for q in out],
    )
    return out


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
    """Close one aspect — answered or skipped. Never raises.

    A SKIPPED aspect is stored, deliberately. That someone raised the front desk and then
    declined to grade it still says the front desk is salient here — and it stops us
    asking again on the next pass.
    """
    from datetime import datetime, timezone

    if answer_source == "open":
        raise ValueError("record_aspect closes a round; use open_aspect_questions to open one")

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
        "answered_at": datetime.now(timezone.utc).isoformat(),
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


def reoffer_open_aspects(author_id: str, limit: int = REOFFER_BATCH) -> list[dict[str, Any]]:
    """Sections this person raised and never graded, ready to ask again.

    Warmer than any cold question: they chose the subject. Marks asked_at so the same
    question does not reappear twice in one sitting.
    """
    from datetime import datetime, timezone

    try:
        res = service_client().rpc(
            "open_aspects_for_author", {"p_author_id": author_id, "p_limit": limit}
        ).execute()
        rows = res.data or []
    except Exception:
        logger.exception("reco_aspects: re-offer lookup failed for %s", author_id)
        return []

    if not rows:
        return []

    now = datetime.now(timezone.utc).isoformat()
    try:
        service_client().table("reco_aspect").update({"asked_at": now}).in_(
            "id", [r["id"] for r in rows]
        ).execute()
    except Exception:
        logger.debug("reco_aspects: could not stamp asked_at")

    return [{
        "aspect_key": r["aspect_key"],
        "aspect_label": r["aspect_label"],
        "source_span": r.get("source_span"),
        "signal_id": r["signal_id"],
        "subject_ref": r.get("subject_ref"),
        # Different framing from the first ask: this is a return, and pretending
        # otherwise reads as a bot that forgot.
        "question": f"Earlier you mentioned {r['aspect_label']} — how was that?",
        "skippable": True,
    } for r in rows]


# ── Find ────────────────────────────────────────────────────────────────────

def split_query(request: str) -> list[dict[str, Any]]:
    """Split a search request into its separate requirements.

    "A restaurant where the owner speaks Italian and the porcelain is unique" is two
    clauses, and the right answer satisfies both. Averaging them into one vector is how
    every other search works and why none of them can answer this.
    """
    text = (request or "").strip()
    if len(text) < 8:
        return []
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            data = llm_json(
                model=router_model(),
                system=SPLIT_QUERY_PROMPT,
                user_payload=text,
                max_tokens=400,
                temperature=0.1,
            )
            if not isinstance(data, dict):
                return []
            out = []
            for c in (data.get("clauses") or [])[:MAX_ASPECTS]:
                if isinstance(c, dict) and str(c.get("text") or "").strip():
                    out.append({
                        "text": str(c["text"]).strip()[:200],
                        "aspect_hint": _slug(str(c.get("aspect_hint") or "")) or None,
                    })
            return out
    except Exception:
        logger.exception("reco_aspects: query split failed")
    return []


def find_by_aspects(
    *, request: str, viewer_id: str, subject_scope: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Aspect-level retrieval. Ranks by how many clauses a subject actually satisfies.

    subject_scope comes from the existing geo/category recall step — this RANKS, it does
    not replace recall. Returns matched quotes so the caller can show WHY each result
    came back; a result that cannot explain itself is indistinguishable from a guess.
    """
    clauses = split_query(request)
    if not clauses:
        return []
    try:
        from app.vec_util import to_pgvector
        from app.vertex_extract import vertex_embed

        vecs = []
        for c in clauses:
            v = vertex_embed(c["text"])
            if v:
                vecs.append(to_pgvector(v))
        if not vecs:
            return []

        res = service_client().rpc("search_subjects_by_aspect", {
            "p_clauses": vecs,
            "p_viewer_id": viewer_id,
            "p_subject_scope": subject_scope,
            "p_limit": limit,
        }).execute()
        return res.data or []
    except Exception:
        logger.exception("reco_aspects: aspect find failed")
        return []
