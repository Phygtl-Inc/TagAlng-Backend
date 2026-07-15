"""Backfill rapport gaps from what we already know.

Rapport gaps are normally opened per-turn from a chat message (the extractor's warm
follow-up). Once a user stops chatting, that queue drains and the home "By the way…"
tile goes silent — nothing left on the plate to keep building their profile.

This module refills the plate: given their existing identity claims, it asks the model
for a couple of fresh follow-ups that DEEPEN the profile with matchable facets, then
opens them as semantic gaps. Called by the ranker only when no gap is queued (and the
cadence cap is already clear), so it stays off the home-render hot path in the common
case. One Flash-class call; silent no-op on any failure.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from app.auth import service_client
from app.rapport_gaps import open_semantic_gap, recent_gap_questions
from app.vec_util import to_pgvector

logger = logging.getLogger(__name__)

# Coverage is now exact (a question records which claim it's about — deepens_concept). This knob
# only controls how tightly answer-spawned sub-claims ("runs mornings") fold into their parent
# theme ("running") via claim↔claim similarity, so they don't resurface as new topics. Env-tunable.
_CLUSTER_SIMILARITY = float(os.environ.get("LANA_RAPPORT_CLUSTER_SIMILARITY", "0.8"))

# Keep the plate small — one to show plus one in reserve is plenty. A larger buffer just
# risks stale questions the user never reaches. _MAX_NEW caps a single synth call; _BUFFER_TARGET
# is how many OPEN gaps we try to keep queued ahead so the tile is never caught empty.
_MAX_NEW = 2
_BUFFER_TARGET = 2

# Coalesce burst calls: the ranker attempts a backfill whenever the plate is empty, and a
# richly-profiled user can yield no new questions — without this, rapid home re-renders would
# each fire an LLM call. In-process only (best-effort across worker instances), keyed by user.
_SYNTH_COOLDOWN_S = 120.0
_last_attempt: dict[str, float] = {}


def _cooling_down(user_id: str) -> bool:
    now = time.monotonic()
    last = _last_attempt.get(user_id)
    if last is not None and (now - last) < _SYNTH_COOLDOWN_S:
        return True
    # Prune stale entries before recording — keeps the map bounded on a long-lived worker.
    if len(_last_attempt) > 5000:
        for uid in [u for u, t in _last_attempt.items() if now - t >= _SYNTH_COOLDOWN_S]:
            _last_attempt.pop(uid, None)
    _last_attempt[user_id] = now
    return False

SYNTH_PROMPT = """You are Lana, a warm neighborhood concierge in TagAlng, a block-based app where neighbors \
connect with nearby neighbors. You keep their profile growing by asking ONE well-timed follow-up at a time on \
their home screen ("By the way…").

Below are THREADS TO ASK ABOUT — identity threads of theirs we have NOT covered yet — plus the questions \
you've ALREADY ASKED. Propose up to {max_new} NEW follow-up questions, each SHARPENING a DIFFERENT one of \
the uncovered threads. Spread across threads — do NOT ask two questions about the same thread, and do NOT \
ask about anything already covered by the ALREADY ASKED list.

A good question adds a CONNECTION-MATCHABLE facet — something that helps them MEET or RELATE to nearby \
neighbors: shared activities/hobbies, family/life stage, local spots they go to, cultural or community ties, \
their weekly rhythm, skill/level, doing-it-with-ottheirs, kids' involvement, teach-vs-learn.

Every question must clear THREE bars — drop any that fail even one:
1. RELEVANT — it sharpens one of the UNCOVERED THREADS listed below, not a generic survey question out of nowhere.
2. IMPORTANT — the answer meaningfully helps match THEM to nearby neighbors. Apply the test: "would this change who they connect with on the block?" If not, drop it.
3. FRESH — you have NOT asked it before. Scan ALREADY ASKED and skip anything you've asked or would just be rewording.

HARD RULES on shape:
- NEVER a yes/no question. If it starts with "Do you", "Are you", "Have you", "Would you" — rewrite it to ask for a SPECIFIC thing (a time, a place, a genre, an activity, a cadence) or drop it. Bad: "Do you and your spouse share any hobbies?" Good: "What's a spot nearby you love for a run?"
- Keep THEM the subject. You MAY sharpen their OWN life-stage facet (e.g. "married" → how long they've been married; "new to the block" → how long they've lived here). But NEVER ask about their partner or the relationship itself ("do you and your spouse…", "what does your partner…") — that's not a facet neighbors match on. Prefer their own activities, local spots, rhythm, and community ties; if a thread has no matchable angle left, skip it.
- The answer should be a concrete facet another mom could share — a place, a time, a genre, an activity, a level — not a sentiment.

FORBIDDEN — never ask an opinion, feeling, or origin-story question (anything asking why, how you \
started, what you enjoy/love most, or what "caught your interest"); those add no matchable facet. Never \
ask about generic consumer/brand/device/product preferences (no neighbor connects over that). NEVER \
touch a sensitive topic — health/medical, grief, divorce/relationship trouble, money/debt, \
legal/immigration, mental health.

Write ONLY the question itself — NO "By the way" prefix, no greeting. Short (<120 chars), warm, OPEN, \
and reference what you know. Quality over quantity: if only one question clears every bar, return just \
one; if none do, return an empty list — silence beats a filler, yes/no, or repeat question.

Each uncovered thread below is shown as `concept | Label [bucket] — "quote"`. For every question
you write, echo back the `concept` of the thread it deepens (copy it EXACTLY from the list) so we
can record what we asked about.

Output ONLY valid JSON (no markdown):
{{
  "questions": [
    {{
      "question": "the question itself, <120 chars",
      "teaser": "2-5 word grammatical lead-in ending with …, e.g. 'about your running…'",
      "label": "short thread name this deepens, e.g. 'running'",
      "bucket": "heritage | stage | vicinity | faith | activity | interest | general",
      "deepens_concept": "the exact concept from the uncovered-threads list this question is about"
    }}
  ]
}}"""


def _backfill_question_embeddings(user_id: str) -> None:
    """Embed any of the user's existing gap questions that predate the embedding column, so
    coverage + dedup see the full history (incl. questions asked before this feature shipped)."""
    try:
        rows = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id, question")
            .eq("user_id", user_id)
            .is_("question_embedding", "null")
            .neq("status", "skipped")
            .limit(40)
            .execute()
        ).data or []
    except Exception:
        logger.exception("rapport-synth: backfill load failed for %s", user_id)
        return
    for row in rows:
        q = str(row.get("question") or "").strip()
        if not q:
            continue
        literal = to_pgvector(_embed_question(q))
        if not literal:
            continue
        try:
            service_client().table("rapport_gaps").update(
                {"question_embedding": literal}
            ).eq("gap_row_id", row["gap_row_id"]).execute()
        except Exception:
            logger.debug("rapport-synth: backfill update failed for a gap of %s", user_id)


def _embed_question(text: str) -> list[float] | None:
    try:
        from app.vertex_extract import vertex_embed

        return vertex_embed(text)
    except Exception:
        logger.debug("rapport-synth: embed failed")
        return None


def _uncovered_claims(user_id: str) -> list[dict[str, Any]]:
    """Identity threads we have NOT yet asked about (least-covered first), via the coverage RPC.
    An empty list means every known thread already has a question — time to go quiet."""
    try:
        res = service_client().rpc(
            "rapport_uncovered_claims",
            {"p_user_id": user_id, "p_cluster_threshold": _CLUSTER_SIMILARITY, "p_limit": 8},
        ).execute()
        return res.data or []
    except Exception:
        logger.exception("rapport-synth: uncovered-claims RPC failed for %s", user_id)
        return []


def _uncovered_block(claims: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for c in claims:
        label = str(c.get("label") or "").strip()
        concept = str(c.get("concept") or "").strip()
        if not label or not concept:
            continue
        bucket = str(c.get("bucket") or "general").strip()
        quote = str(c.get("source_quote") or "").strip()
        line = f"- {concept} | {label} [{bucket}]"
        if quote:
            line += f' — "{quote[:120]}"'
        lines.append(line)
    return "\n".join(lines)


def _asked_block(questions: list[str]) -> str:
    if not questions:
        return "(none yet)"
    return "\n".join(f"- {q}" for q in questions)


def _parse_questions(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("questions")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()[:200]
        if not question:
            continue
        out.append(
            {
                "question": question,
                "teaser": str(item.get("teaser") or "").strip()[:80],
                "label": str(item.get("label") or "").strip()[:80],
                "bucket": str(item.get("bucket") or "general").strip()[:32] or "general",
                "deepens_concept": str(item.get("deepens_concept") or "").strip()[:64],
            }
        )
        if len(out) >= _MAX_NEW:
            break
    return out


def _generate(uncovered: str, asked: str, max_new: int) -> Any:
    system = SYNTH_PROMPT.format(max_new=max_new)
    user_payload = f"THREADS TO ASK ABOUT (uncovered):\n{uncovered}\n\nALREADY ASKED:\n{asked}"
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if llm_configured():
            return llm_json(
                model=router_model(),
                system=system,
                user_payload=user_payload,
                max_tokens=512,
                temperature=0.4,
            )
    except Exception:
        logger.exception("rapport-synth: orchestrator llm failed")

    # Vertex Gemini fallback.
    try:
        from app.orchestrator.json_util import parse_json_object
        from app.vertex_extract import _vertex_client
        from google.genai import types

        client = _vertex_client()
        model = os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")
        response = client.models.generate_content(
            model=model,
            contents=system + "\n\n" + user_payload,
            config=types.GenerateContentConfig(
                temperature=0.4,
                response_mime_type="application/json",
            ),
        )
        return parse_json_object(response.text or "")
    except Exception:
        logger.exception("rapport-synth: vertex fallback failed")
        return None


def synthesize_gaps_from_claims(user_id: str, max_new: int = _MAX_NEW) -> int:
    """Open up to ``max_new`` fresh rapport gaps from the user's claims. Returns how many
    were opened. Never raises. Duplicate topics are deduped by ``open_semantic_gap``'s
    slug key, so re-running is safe."""
    if not user_id or _cooling_down(user_id):
        return 0
    # Make sure older questions (pre-embedding) count toward coverage before we decide.
    _backfill_question_embeddings(user_id)
    uncovered = _uncovered_claims(user_id)
    logger.info(
        "rapport-synth[%s]: uncovered threads = %s",
        user_id,
        [c.get("label") for c in uncovered] or "(none)",
    )
    if not uncovered:
        # Every known thread already has a question — go quiet rather than invent filler.
        logger.info("rapport-synth: all threads covered for %s — nothing to ask", user_id)
        return 0
    # Pass the FULL ask history (not just the recent few) so we never repeat or reword a question
    # from any point in the past, however old.
    asked = recent_gap_questions(user_id, limit=60)
    data = _generate(_uncovered_block(uncovered), _asked_block(asked), max_new)
    questions = _parse_questions(data)
    logger.info(
        "rapport-synth[%s]: generated = %s",
        user_id,
        [(q["deepens_concept"], q["question"]) for q in questions] or "(none)",
    )
    if not questions:
        return 0
    opened = 0
    for q in questions:
        try:
            if open_semantic_gap(
                user_id,
                None,
                q["question"],
                label=q["label"] or None,
                bucket=q["bucket"],
                teaser=q["teaser"] or None,
                deepens_concept=q["deepens_concept"] or None,
            ):
                opened += 1
        except Exception:
            logger.exception("rapport-synth: open_semantic_gap failed for %s", user_id)
    logger.info("rapport-synth: opened %d new gap(s) from claims for %s", opened, user_id)
    return opened


def _open_gap_count(user_id: str) -> int:
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "open")
            .execute()
        )
        return res.count if getattr(res, "count", None) is not None else len(res.data or [])
    except Exception:
        logger.exception("rapport-synth: open-count failed for %s", user_id)
        return _BUFFER_TARGET  # fail closed — assume full, don't over-synthesize on a read error


def ensure_gap_buffer(user_id: str, target: int = _BUFFER_TARGET) -> int:
    """Top the open-gap pool back up toward ``target`` so the tile always has a question queued
    ahead. Only synthesizes the shortfall; a no-op when the buffer is already full. Best-effort —
    intended to run as a background task after a tile answer closes a gap."""
    if not user_id:
        return 0
    need = target - _open_gap_count(user_id)
    if need <= 0:
        return 0
    return synthesize_gaps_from_claims(user_id, max_new=min(need, _MAX_NEW))
