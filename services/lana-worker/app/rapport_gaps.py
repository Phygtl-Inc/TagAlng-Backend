"""Rapport gap lifecycle — open (semantic) / close / skip / mute.

Follow-up questions are opened by open_semantic_gap() using the extractor's own warm,
per-turn question (see app/claims_persist.py) — contextual to what the user actually said,
never a static template. reconcile_gaps() only *closes* gaps whose covered concept the user
has since stated. Every write is idempotent, so this is safe to run fire-and-forget.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from app.auth import service_client
from app.rapport_gap_tree import get_gap, render_why_frame
from app.vec_util import to_pgvector

logger = logging.getLogger(__name__)

# A candidate question this close (cosine) to one we've already asked is treated as the same
# question and dropped — this is what stops "which trails do you run?" from reappearing as
# "any local spots you like for running?". Env-tunable.
_DUP_SIMILARITY = float(os.environ.get("LANA_RAPPORT_DUP_SIMILARITY", "0.9"))


def _question_embedding(question: str) -> list[float] | None:
    """Embed a gap question for dedup/coverage. None on any failure (caller fails open)."""
    try:
        from app.vertex_extract import vertex_embed

        return vertex_embed(question)
    except Exception:
        logger.debug("rapport: question embed failed (dedup will fail open)")
        return None


def _is_semantic_duplicate(user_id: str, embedding: list[float] | None) -> bool:
    """True when an existing (non-skipped) gap question means the same thing as this one."""
    literal = to_pgvector(embedding)
    if not literal:
        return False  # no embedding → can't judge → fail open (let it through)
    try:
        res = service_client().rpc(
            "rapport_question_max_similarity",
            {"p_user_id": user_id, "p_embedding": literal},
        ).execute()
        max_sim = float(res.data or 0.0)
        return max_sim >= _DUP_SIMILARITY
    except Exception:
        logger.exception("rapport: dup-similarity check failed for %s", user_id)
        return False  # fail open — never block a gap on a transient RPC error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _active_claims(user_id: str) -> list[dict[str, Any]]:
    """Active (non-dismissed) identity claims with the fields reconciliation needs."""
    try:
        res = (
            service_client()
            .table("user_identity_claims")
            .select("id, concept, label, bucket, confidence")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(100)
            .execute()
        )
        return res.data or []
    except Exception:  # best-effort; a read failure just means no reconciliation this turn
        logger.exception("rapport: failed to load active claims for %s", user_id)
        return []


def _existing_gap_rows(user_id: str) -> dict[str, dict[str, Any]]:
    """All rapport_gaps rows for the user, keyed by gap_id (one row per gap by unique idx)."""
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id, gap_id, status, covers_concept")
            .eq("user_id", user_id)
            .execute()
        )
        return {r["gap_id"]: r for r in (res.data or [])}
    except Exception:
        logger.exception("rapport: failed to load gap rows for %s", user_id)
        return {}


def recent_gap_questions(user_id: str, limit: int = 10) -> list[str]:
    """Recent rapport questions already opened for this user — so the extractor can avoid
    generating a near-duplicate (e.g. asking 'watch with neighbors?' for soccer AND Real Madrid)."""
    if not user_id:
        return []
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("question")
            .eq("user_id", user_id)
            .order("opened_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [r["question"] for r in (res.data or []) if r.get("question")]
    except Exception:
        logger.exception("rapport: recent_gap_questions failed for %s", user_id)
        return []


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s[:48] or "topic"


def open_semantic_gap(
    user_id: str,
    message_id: str | None,
    question: str,
    *,
    label: str | None = None,
    bucket: str | None = None,
    teaser: str | None = None,
    deepens_concept: str | None = None,
) -> bool:
    """Open ONE contextual follow-up gap carrying the AI's own per-turn question.

    `deepens_concept` records WHICH identity claim this question is about, so coverage is an
    exact fact (not a fuzzy similarity guess) — see rapport_uncovered_claims.

    The question is generated from what the user actually said (e.g. "I play FIFA" →
    "online with a squad, or solo career mode?"), so it always makes sense — unlike the
    retired static templates. Keyed by topic slug so the same thread doesn't reopen twice.
    Semantic gaps close when the user answers/skips (not via concept-match).

    Returns True when a NEW row was inserted, False when it already existed (unique-key
    collision) or the input was empty — so callers can count real openings.
    """
    if not user_id or not question or not str(question).strip():
        return False
    topic = label or question
    gap_id = f"deepen:{_slug(topic)}"
    bucket = bucket or "general"
    # Teaser is AI-generated (grammatical, contextual) — e.g. "about your reading…". We no
    # longer glue the raw claim label, which broke on predicate labels ("about your interested
    # in books…"). Fall back to a neutral eyebrow only if the model gave none.
    why_frame = teaser.strip() if teaser and teaser.strip() else "one quick thing…"
    q_text = str(question).strip()
    # Semantic dedup: don't reopen a question that means the same as one we already asked, even
    # when the wording (and thus the slug) differs. Embedding also stored to power coverage steering.
    embedding = _question_embedding(q_text)
    if _is_semantic_duplicate(user_id, embedding):
        logger.info("rapport: skipped near-duplicate question for %s: %s", user_id, q_text)
        return False
    row: dict[str, Any] = {
        "user_id": user_id,
        "gap_id": gap_id,
        "parent_bucket": bucket,
        # synthetic — semantic gaps close on answer/skip, not on concept-match
        "covers_concept": f"deepen_{_slug(topic)}",
        "why_frame": why_frame,
        "question": q_text,
        "unlock_score": 0.8,
        "opened_from_message_id": message_id,
        "status": "open",
    }
    if deepens_concept:
        row["deepens_concept"] = str(deepens_concept).strip()[:64] or None
    literal = to_pgvector(embedding)
    if literal:
        row["question_embedding"] = literal
    try:
        res = service_client().table("rapport_gaps").insert(row).execute()
    except Exception:
        # unique(user_id, gap_id) violation = already open for this topic — fine
        logger.debug("rapport: semantic gap %s exists/race", gap_id)
        return False
    # Write-time i18n: question/why_frame are English-canonical; render them into the
    # user's preferred language NOW (off the read path) so the home tile serves a saved
    # string. Background + best-effort — next_ask self-heals any miss.
    try:
        from app.lang_pref import get_user_preferred_language
        from app.rapport_i18n import localize_gap_row_async

        pref = get_user_preferred_language(user_id)
        if pref and pref != "en":
            gap_row_id = str(((res.data or [{}])[0] or {}).get("gap_row_id") or "")
            if gap_row_id:
                localize_gap_row_async(gap_row_id, q_text, why_frame, pref)
    except Exception:  # noqa: BLE001 — localization must never block a gap opening
        logger.exception("rapport: gap i18n kickoff failed")
    return True


def reconcile_gaps(user_id: str, message_id: str | None = None) -> None:
    """Close gaps whose covered concept the user has now stated. Idempotent.

    Opening is handled by open_semantic_gap(); this pass only retires gaps that got answered
    elsewhere (e.g. the user volunteered the fact in a later turn). Semantic gaps use a
    synthetic covers_concept that never matches, so they're untouched here — they close via
    record-answer / skip.
    """
    if not user_id:
        return
    claims = _active_claims(user_id)
    known_concepts = {c["concept"] for c in claims if c.get("concept")}
    if not known_concepts:
        return
    existing = _existing_gap_rows(user_id)
    sb = service_client()
    for gap_id, row in existing.items():
        if row["status"] not in ("open", "asked"):
            continue
        if row["covers_concept"] in known_concepts:
            claim_id = next(
                (c["id"] for c in claims if c.get("concept") == row["covers_concept"]),
                None,
            )
            try:
                sb.table("rapport_gaps").update(
                    {
                        "status": "answered",
                        "answered_at": _now(),
                        "answer_claim_id": claim_id,
                        "updated_at": _now(),
                    }
                ).eq("gap_row_id", row["gap_row_id"]).execute()
            except Exception:
                logger.exception("rapport: failed to close gap %s", gap_id)


def get_gap_row(gap_row_id: str) -> dict[str, Any] | None:
    """Fetch a gap row's identifying fields (for persisting its answer as a claim)."""
    if not gap_row_id:
        return None
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_id, covers_concept, parent_bucket, why_frame")
            .eq("gap_row_id", gap_row_id)
            .limit(1)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception:
        logger.exception("rapport: get_gap_row failed for %s", gap_row_id)
        return None


def mark_answered(gap_row_id: str, answer_claim_id: str | None = None) -> None:
    """Close a gap because the user engaged with the ask this turn, linking the claim it made.

    reconcile_gaps closes gaps whose covered concept now exists as a claim, but a free-text
    answer may map to a differently-named concept (or none). We still don't want to re-ask a
    topic she just responded to, so close it directly by row id and record the answer claim.
    """
    if not gap_row_id:
        return
    patch: dict[str, Any] = {
        "status": "answered",
        "answered_at": _now(),
        "updated_at": _now(),
    }
    if answer_claim_id:
        patch["answer_claim_id"] = answer_claim_id
    try:
        service_client().table("rapport_gaps").update(patch).eq(
            "gap_row_id", gap_row_id
        ).execute()
    except Exception:
        logger.exception("rapport: mark_answered failed for %s", gap_row_id)


def record_skip(gap_row_id: str) -> None:
    """Bump skip count; the RPC reopens the gap or expires it after 3 skips."""
    try:
        service_client().rpc(
            "increment_skip_and_reopen", {"p_gap_row_id": gap_row_id}
        ).execute()
    except Exception:
        logger.exception("rapport: skip failed for %s", gap_row_id)


def mute_gap(user_id: str, gap_id: str) -> None:
    """Never ask this gap again. Mutes an existing row, or writes a stub for a tree gap."""
    if not user_id or not gap_id:
        return
    sb = service_client()
    try:
        existing = (
            sb.table("rapport_gaps")
            .select("gap_row_id")
            .eq("user_id", user_id)
            .eq("gap_id", gap_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            # Works for any gap (semantic or tree) — it already has a row.
            sb.table("rapport_gaps").update(
                {"status": "muted_by_user", "updated_at": _now()}
            ).eq("gap_row_id", existing.data[0]["gap_row_id"]).execute()
            return
        # No row yet — only a known tree gap can be pre-muted as a stub.
        gap = get_gap(gap_id)
        if not gap:
            return
        sb.table("rapport_gaps").insert(
            {
                "user_id": user_id,
                "gap_id": gap_id,
                "parent_bucket": gap["parent_bucket"],
                "covers_concept": gap["covers_concept"],
                "why_frame": render_why_frame(gap, None),
                "unlock_score": gap["unlock_score"],
                "status": "muted_by_user",
            }
        ).execute()
    except Exception:
        logger.exception("rapport: mute failed for %s / %s", user_id, gap_id)
