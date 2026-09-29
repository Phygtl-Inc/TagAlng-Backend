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
import re
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
     "question": "...", "confidence": 0.9}
  ]
}

Rules:
- ONE entry per distinct thing THEY raised. Not a checklist of what a place of this type
  usually has — if they did not mention parking, there is no parking aspect.
- "label" is THEIR noun for the thing — short, 1-4 words, no verb, no verdict:
  "the mess", "the price", "the owner", "the wait", "the noise", "Carlos". Not the whole
  clause ("he left a mess in the kitchen"). Keep their word — do not tidy "the lady at the
  front" into "reception staff".
- "key" is a short lowercase English slug for matching across people: owner, porcelain,
  front_desk, wait_time, parking.
- "span" is the fragment of their words this came from. Every aspect must be traceable to
  something they said.
- "question" is the ONE follow-up Lana asks. Its whole job is to get THEIR VERDICT on this
  thing IN THEIR OWN WORDS — how it actually went — so a neighbour can read it later.
  · If they gave NO verdict on it, ask how it went, echoing their noun.
  · If they ALREADY gave a verdict (fair, rude, slow, cheap, fast, big, great), ask for the
    one concrete detail behind it that they have NOT said yet — never re-ask the verdict.
  · Exactly ONE question mark. One short conversational sentence. No "and ...", no second
    clause, no "can you tell me more".
  · Forbidden: how important it was, how it made them feel / affected them, yes/no about
    their own verdict, scales or choices ("rate", "out of 5", "good or bad?").
    NOT "the portions — were they enough?"   (they said big)   BUT "the portions — big
      enough to share?"
    NOT "the price — how important was it?"                    BUT "the price — roughly
      what did he charge, and for what?"
    NOT "la espera — ¿qué tan larga fue y cómo te afectó?"     BUT "la espera — ¿cuánto
      esperaste?"
- LANGUAGE: "label" and "question" are in the SAME language as the statement. A Spanish
  statement gets Spanish labels and Spanish questions. Only "key" is always English.
- A quality they attach TO the subject is something they said about it, and IS an aspect:
  "a Spanish barber", "a female dentist", "a 24-hour plumber", "a vegan bakery". Only the
  category word itself (barber, dentist, plumber) is the subject and is never an aspect.
  Check every word they put BEFORE or AROUND the category noun — each such quality gets
  its own entry, even when the rest of the statement has more to say. Its question asks the PRACTICAL fact a neighbour would act on — never how it affected
  them, never why it matters, never to explain or justify who someone is:
    "Spanish"  → "Does he cut in Spanish if you'd rather?"
    "female"   → "Was it easy to book with her specifically?"
    "24-hour"  → "You said they came at 2am — how fast did they get there?"
    "vegan"    → "Is everything vegan, or just some of it?"
- When the thing is how the subject is WITH someone ("amazing with my son", "good with
  kids"), the label is that trait ("with kids", "con niños"), not the person.

Worked example —
Statement: "Rosa's bakery has amazing croissants, the line is always out the door, and the
guy at the register never smiles."
  {"label": "the croissants", "key": "croissants", "span": "amazing croissants",
   "question": "You said the croissants are amazing — which ones should a first-timer get?"}
  {"label": "the line", "key": "line", "span": "the line is always out the door",
   "question": "You said the line is always out the door — how long did you wait?"}
  {"label": "the register", "key": "register_staff", "span": "the guy at the register never smiles",
   "question": "You said he never smiles — what happened when you paid?"}
Nothing else: they said nothing about parking, prices or the owner, so none of those exist —
and Rosa's bakery itself is the subject, never an aspect. These are illustrations of the
SHAPE only; never reuse their wording.
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
  "subject_kind": "restaurant",
  "recommender_trait": null
}

Rules:
- ONE clause per requirement. "A quiet cafe with good wifi where the barista knows you"
  is three.
- "recommender_trait" is a requirement about the PERSON RECOMMENDING, never the thing:
  "a Turkish restaurant recommended by someone from Turkey" → "from Turkey";
  "a barber a Spanish speaker would go to" → "speaks Spanish";
  "a running shop, ideally from someone who has run a marathon" → "has run a marathon".
  "a Turkish restaurant a Turkish person would vouch for" → "Turkish".
  ONLY who the person IS or has done, in a few of the asker's words — never the verb
  around it ("would vouch for", "recommended by"). It is NOT also a clause. Null when the
  request says nothing about who recommends — "a Spanish barber" is about the BARBER:
  "Spanish" is a clause and recommender_trait is null.
- The thing they want ("a barber", "a Turkish restaurant") is subject_kind, NEVER a clause.
  When the only requirement is on the recommender, "clauses" is empty.
- Keep the clause in the asker's words. Do not normalise "the owner speaks Italian" into
  "multilingual staff" — the words have to match how someone else would have SAID it.
- "aspect_hint" is a slug guess, optional, best-effort.
- "subject_kind" is what they are looking for, if stated. Null if not.
- Drop location and time constraints — those are handled before this runs.
- One clause is a valid answer. Zero means there is nothing aspect-shaped here.

Request:
"""


def _split_prompt() -> str:
    # NOT str.format: the prompt is full of literal JSON braces, and .format() on it raised
    # KeyError inside the try below — the splitter silently returned [] on every call.
    return SPLIT_PROMPT.replace("{max_aspects}", str(MAX_ASPECTS))


def split_statement(
    statement: str, *, subject_name: str | None = None, subject_terms: list[str] | None = None
) -> list[dict[str, Any]]:
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
                system=_split_prompt(),
                user_payload=payload,
                max_tokens=640,
                temperature=0.2,
            )
            return _parse_aspects(data, statement=text, subject_terms=subject_terms)
    except Exception:
        logger.exception("reco_aspects: split failed")

    try:
        import os

        from app.orchestrator.llm import vertex_generate_json

        return _parse_aspects(
            statement=text,
            subject_terms=subject_terms,
            data=vertex_generate_json(
                model=os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash"),
                system=None,
                user_payload=_split_prompt() + payload,
                max_tokens=640,
                temperature=0.2,
            )
        )
    except Exception:
        logger.exception("reco_aspects: split fallback failed")
        return []


_WORD = re.compile(r"\w+", re.UNICODE)


def _traceable(span: str | None, statement: str | None) -> bool:
    """Does this aspect come from something they actually said?

    Most of the span's words must be in the statement. An aspect whose span is not there
    is one the model invented — observed live: the prompt's own "You mentioned the wait"
    example turning up as a "wait" aspect on a plumber nobody said kept them waiting. A
    provenance check on the model's output, not a reading of the user's intent."""
    if statement is None:
        return True
    words = {w.lower() for w in _WORD.findall(span or "") if len(w) > 2}
    if not words:
        return False
    said = {w.lower() for w in _WORD.findall(statement)}
    return len(words & said) / len(words) >= 0.6


_FILLER = {"the", "a", "an", "my", "our", "his", "her", "their", "el", "la", "los", "las", "mi", "o", "os", "as"}


def _is_subject(label: str, subject_terms: list[str] | None) -> bool:
    """Is this "aspect" just the thing being recommended? ("the barber" on Carlos the
    barber.) Its label's words all sit inside the subject's name or category."""
    words = {w.lower() for w in _WORD.findall(label or "")} - _FILLER
    if not words or not subject_terms:
        return False
    for term in subject_terms:
        if words <= {w.lower() for w in _WORD.findall(term)}:
            return True
    return False


def _parse_aspects(
    data: Any, statement: str | None = None, subject_terms: list[str] | None = None
) -> list[dict[str, Any]]:
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
        if _is_subject(label, subject_terms):
            logger.info("reco_aspects: dropped the subject itself as an aspect %r", label)
            continue
        if not _traceable(str(item.get("span") or ""), statement):
            logger.info("reco_aspects: dropped untraceable aspect %r span=%r", key, item.get("span"))
            continue
        seen.add(key)
        out.append({
            "aspect_key": key,
            "aspect_label": label,
            "source_span": str(item.get("span") or "").strip()[:300] or None,
            "question": str(item.get("question") or "").strip()[:200] or None,
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
                system=BAND_PROMPT.replace("{aspect}", aspect_label),
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

def _label_text(aspect_label: str, aspect_key: str) -> str:
    # ONE form for both sides of the key match. The stored label_embedding and the probe
    # are built from this same string; comparing a bare label against "label: answer"
    # vectors measured two different kinds of text.
    return f"{aspect_label} ({aspect_key})"


def _resolve_key(
    subject_ref: str | None, aspect_key: str, aspect_label: str,
) -> tuple[str, str | None]:
    """(canonical key, label embedding as pgvector text or None). Never raises."""
    vec_text: str | None = None
    try:
        from app.vec_util import to_pgvector
        from app.vertex_extract import vertex_embed

        vec = vertex_embed(_label_text(aspect_label, aspect_key))
        vec_text = to_pgvector(vec) if vec else None
    except Exception:
        logger.debug("reco_aspects: label embed failed")
    if not subject_ref or not vec_text:
        return aspect_key, vec_text
    try:
        res = service_client().rpc(
            "match_reco_aspect_key",
            {
                "p_subject_ref": subject_ref,
                "p_embedding": vec_text,
                "p_min_similarity": ASPECT_MERGE_SIMILARITY,
            },
        ).execute()
        rows = res.data or []
        if rows and rows[0].get("aspect_key"):
            return str(rows[0]["aspect_key"]), vec_text
    except Exception:
        # No RPC yet, or a transient failure. Falling back to the raw key fragments the
        # vocabulary rather than merging two different things — the safe direction.
        logger.debug("reco_aspects: canonicalisation unavailable, using raw key")
    return aspect_key, vec_text


def canonical_key(subject_ref: str | None, aspect_key: str, aspect_label: str) -> str:
    """Reuse an aspect key this subject already has when the meaning matches.

    Without this, "the front desk", "reception" and "the desk staff" are three aspects at
    n=1 and the count that makes the whole thing worth reading never rises above one.
    """
    return _resolve_key(subject_ref, aspect_key, aspect_label)[0]


# ── the round ───────────────────────────────────────────────────────────────

def open_aspect_questions(
    *, signal_id: str, subject_ref: str | None, author_id: str, statement: str,
    subject_name: str | None = None, persist: bool = True,
    subject_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Split the statement, PERSIST one open row per section, return the questions.

    Persisting up front is what makes a partial round survive (rule 6). If they answer
    two and close the app, the other four already exist as 'open' and
    reoffer_open_aspects() can bring them back — the sections they raised are not lost
    because they got tired.

    Returns [{aspect_key, aspect_label, source_span, question, skippable}]. The caller
    asks one at a time and calls record_aspect per answer or per skip.
    """
    aspects = split_statement(
        statement, subject_name=subject_name, subject_terms=subject_terms
    )
    if not aspects:
        return []

    out: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for a in aspects:
        key, label_vec = _resolve_key(subject_ref, a["aspect_key"], a["aspect_label"])
        out.append({
            "aspect_key": key,
            "aspect_label": a["aspect_label"],
            "source_span": a["source_span"],
            # Lana echoes their own words back. "You mentioned the owner — how was it?"
            # is answerable; "Rate the ownership experience" is a form. The splitter
            # writes it in the statement's language; the template is only the fallback.
            "question": a.get("question") or f"You mentioned {a['aspect_label']} — how was it?",
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
            "label_embedding": label_vec,
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

    aspect_key is the key the question was OPENED with, used as-is. Re-canonicalising
    here could land on a different key once other people's answers move the centroid,
    and the upsert would then insert a second row and leave the open one owed forever.
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
        "aspect_key": aspect_key,
        "aspect_label": aspect_label,
        "source_span": source_span,
        "answer_verbatim": answer_verbatim,
        "answer_source": answer_source,
        "sentiment": sentiment,
        "sentiment_confidence": confidence or None,
        "answered_at": datetime.now(timezone.utc).isoformat(),
    }
    # The content embedding is what Find matches on; a skip has no content to find.
    if answer_source != "skipped" and answer_verbatim:
        try:
            from app.vec_util import to_pgvector
            from app.vertex_extract import vertex_embed

            vec = vertex_embed(f"{aspect_label}: {answer_verbatim}")
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

def _empty_split() -> dict[str, Any]:
    return {"clauses": [], "subject_kind": None, "recommender_trait": None}


def split_query_full(request: str) -> dict[str, Any]:
    """{"clauses": [...], "subject_kind": "barber" | None, "recommender_trait": str | None}.
    Never raises.

    subject_kind is what lets aspect search RECALL, not just rank: the second pass asks
    the ordinary tip search for every visible "barber", so a barber whose card never says
    "Spanish" — but whose recommenders did, in the round — can still be found.

    recommender_trait is a requirement on WHO recommends ("from Turkey"), read by
    app/reco_authority.py against the recommenders' own claims. It is never a clause: the
    subject is not Turkish because a Turkish neighbour recommended it."""
    text = (request or "").strip()
    if len(text) < 8:
        return _empty_split()
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
                return _empty_split()
            out = []
            for c in (data.get("clauses") or [])[:MAX_ASPECTS]:
                if isinstance(c, dict) and str(c.get("text") or "").strip():
                    out.append({
                        "text": str(c["text"]).strip()[:200],
                        "aspect_hint": _slug(str(c.get("aspect_hint") or "")) or None,
                    })
            kind = str(data.get("subject_kind") or "").strip()[:60] or None
            trait = str(data.get("recommender_trait") or "").strip()[:80] or None
            return {"clauses": out, "subject_kind": kind, "recommender_trait": trait}
    except Exception:
        logger.exception("reco_aspects: query split failed")
    return _empty_split()


def split_query(request: str) -> list[dict[str, Any]]:
    """Split a search request into its separate requirements.

    "A restaurant where the owner speaks Italian and the porcelain is unique" is two
    clauses, and the right answer satisfies both. Averaging them into one vector is how
    every other search works and why none of them can answer this.
    """
    return split_query_full(request)["clauses"]


def find_by_aspects(
    *, request: str, user_jwt: str, subject_scope: list[str] | None = None,
    limit: int = 20, clauses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Aspect-level retrieval. Ranks by how many clauses a subject actually satisfies.

    subject_scope comes from the existing geo/category recall step — this RANKS, it does
    not replace recall. Returns matched quotes so the caller can show WHY each result
    came back; a result that cannot explain itself is indistinguishable from a guess.

    Runs as the VIEWER (user_jwt): the RPC reads auth.uid() for block and circle
    visibility, so a service-role call would see nothing — and must not see everything.
    """
    if clauses is None:
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

        from app.supabase_rpc import call_rpc

        # text[] of pgvector literals — PostgREST passes a JSON array of strings cleanly,
        # a vector[] argument it does not.
        rows = call_rpc(user_jwt, "search_subjects_by_aspect", {
            "p_clauses": vecs,
            "p_subject_scope": subject_scope,
            "p_limit": limit,
        })
        return rows if isinstance(rows, list) else []
    except Exception:
        logger.exception("reco_aspects: aspect find failed")
        return []


# ── embedding backfill ──────────────────────────────────────────────────────

BACKFILL_BATCH = 25


def backfill_embeddings(limit: int = BACKFILL_BATCH) -> dict[str, int]:
    """Fill the vectors a row was saved without. Never raises.

    record_aspect / open_aspect_questions embed best-effort: when Vertex is down the row
    is still written (the answer is the product), but a row without `label_embedding`
    never merges with anyone's "front desk", and one without `embedding` can never be
    found by aspect search — silently, forever. This is what brings them back.

    Stops at the first failed embed: if Vertex is down, one probe is enough to know, and a
    batch of guaranteed failures is just load."""
    from app.vec_util import to_pgvector
    from app.vertex_extract import vertex_embed

    done = {"label": 0, "content": 0, "failed": 0}
    try:
        table = service_client().table("reco_aspect")
        need_label = (
            table.select("id,aspect_key,aspect_label")
            .is_("label_embedding", "null").limit(limit).execute().data or []
        )
        need_content = (
            table.select("id,aspect_label,answer_verbatim")
            .is_("embedding", "null")
            .in_("answer_source", ["voice", "text", "tap"])
            .not_.is_("answer_verbatim", "null")
            .limit(limit).execute().data or []
        )
    except Exception:  # noqa: BLE001
        logger.exception("reco_aspects: backfill lookup failed")
        return done

    def _embed(text: str) -> str | None:
        try:
            vec = vertex_embed(text)
            return to_pgvector(vec) if vec else None
        except Exception:  # noqa: BLE001
            return None

    for row in need_label:
        lit = _embed(_label_text(row["aspect_label"], row["aspect_key"]))
        if not lit:
            done["failed"] += 1
            return done
        service_client().table("reco_aspect").update({"label_embedding": lit}).eq(
            "id", row["id"]).execute()
        done["label"] += 1
    for row in need_content:
        lit = _embed(f"{row['aspect_label']}: {row['answer_verbatim']}")
        if not lit:
            done["failed"] += 1
            return done
        service_client().table("reco_aspect").update({"embedding": lit}).eq(
            "id", row["id"]).execute()
        done["content"] += 1
    if done["label"] or done["content"]:
        logger.info("reco_aspects: backfilled label=%d content=%d", done["label"], done["content"])
    return done


if __name__ == "__main__":  # python -m app.reco_aspects  → one backfill pass
    import json as _json

    logging.basicConfig(level=logging.INFO)
    print(_json.dumps(backfill_embeddings(limit=500)))
