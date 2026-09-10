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
from difflib import SequenceMatcher
from typing import Any

from app.auth import service_client
from app.rapport_gap_tree import get_gap, render_why_frame
from app.rapport_priority import P_NEW_BUCKET
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
    place_ref: str | None = None,
    affiliation_ref: str | None = None,
    gap_id: str | None = None,
    unlock_score: float | None = None,
    from_local_supply: bool = False,
    answer_options: list[str] | None = None,
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
    # An explicit gap_id (e.g. "ground:<affiliation_id>") keys idempotency to the THING
    # the question is about, not its wording — so a grounding question can never reopen
    # for an affiliation that was already asked, answered, or skipped out.
    gap_id = gap_id or f"deepen:{_slug(topic)}"
    bucket = bucket or "general"
    # Priority. Every semantic gap used to open at a flat 0.8, so the tile's
    # "highest-scoring open gap" was really oldest-open-first — a new user's most
    # valuable question queued behind whatever landed first. app/rapport_priority.py
    # derives the score from what the MATCHER pays for (a place is +3, an interest
    # +1, and only if a neighbor nearby shares it), so what unlocks introductions
    # soonest ranks first. An explicit unlock_score from the caller still wins.
    if unlock_score is None:
        from app.rapport_priority import describe, score_for

        unlock_score = score_for(
            user_id,
            bucket=bucket,
            deepens_concept=deepens_concept,
            affiliation_ref=affiliation_ref,
            from_local_supply=from_local_supply,
        )
        logger.info(
            "rapport_gap_priority user=%s tier=%s score=%.2f q=%r",
            user_id,
            describe(unlock_score),
            unlock_score,
            str(question)[:60],
        )
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
        "unlock_score": unlock_score,
        "opened_from_message_id": message_id,
        "status": "open",
    }
    if deepens_concept:
        row["deepens_concept"] = str(deepens_concept).strip()[:64] or None
    if place_ref:
        # Circles §4.3: the claim made from this gap's answer inherits this place tag.
        row["place_ref"] = str(place_ref)
    if affiliation_ref:
        # A place-grounding question: the answer path grounds this affiliation instead
        # of persisting an identity claim (see circles_flow.handle_grounding_answer).
        row["affiliation_ref"] = str(affiliation_ref)
    chips = [
        " ".join(str(o).split())[:48] for o in (answer_options or []) if str(o or "").strip()
    ][:3]
    if chips:
        # Tappable answers for the card. A tap posts the chip text through the normal
        # answer path, so nothing downstream needs to know they existed.
        row["answer_options"] = chips
    literal = to_pgvector(embedding)
    if literal:
        row["question_embedding"] = literal
    try:
        res = service_client().table("rapport_gaps").insert(row).execute()
    except Exception:
        # unique(user_id, gap_id) violation = already open for this topic — fine.
        # An environment without answer_options (pre-20261029) also lands here, so
        # retry once without the chips rather than lose the whole question.
        if "answer_options" not in row:
            logger.debug("rapport: semantic gap %s exists/race", gap_id)
            return False
        row.pop("answer_options")
        try:
            res = service_client().table("rapport_gaps").insert(row).execute()
        except Exception:
            logger.debug("rapport: semantic gap %s exists/race", gap_id)
            return False
    gap_row_id = str(((res.data or [{}])[0] or {}).get("gap_row_id") or "")
    # The ⓘ line on the ask card explains WHY this question helps ("so I can introduce
    # you to neighbors who…") rather than just naming the topic. AI-authored per gap,
    # in a background thread — a subtitle never makes a user wait on an LLM.
    if gap_row_id:
        try:
            from app.rapport_reasons import attach_ask_reason_async

            attach_ask_reason_async(
                gap_row_id,
                q_text,
                user_id=user_id,
                label=label,
                why_frame=why_frame,
                grounding=bool(affiliation_ref),
            )
        except Exception:  # noqa: BLE001 — the gap itself is already saved
            logger.exception("rapport: why-reason kickoff failed")
    # Write-time i18n: question/why_frame are English-canonical; render them into the
    # user's preferred language NOW (off the read path) so the home tile serves a saved
    # string. Background + best-effort — next_ask self-heals any miss. (The reason lands
    # a moment later and re-renders the entry itself.)
    try:
        from app.lang_pref import get_user_preferred_language
        from app.rapport_i18n import localize_gap_row_async

        pref = get_user_preferred_language(user_id)
        if pref and pref != "en" and gap_row_id:
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
    """Fetch a gap row's identifying fields, INCLUDING its question.

    `question` was missing from this select for as long as
    _wire_ask_gap_action has relied on it, and that function treats a row with no
    question as "nothing vetted this" — so EVERY ask_gap silently downgraded to
    `reply`, the stored question was never substituted, and mark_chat_asked never
    ran (it sits after that early return). The model writes a lead-in expecting the
    system to append the question, so the person received a dangling fragment:
    "Italian pizza's a good one —" and nothing else (2026-08-06). Two of the
    rapport bugs chased today were downstream of this one line.
    """
    if not gap_row_id:
        return None
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select(
                "gap_row_id, gap_id, question, covers_concept, parent_bucket, why_frame, "
                "place_ref, affiliation_ref, grounding_options"
            )
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


def mark_chat_asked(gap_row_id: str) -> None:
    """Stamp a gap Lana just asked in CONVERSATION, so candidate_goals stops
    offering it and the policy cannot re-ask it next turn.

    Deliberately NOT status='asked': that status means "showing on the home
    tile", and rapport_ranker._pending_ask re-shows any such row verbatim — so
    reusing it would surface every chat question as a tile as well. The status
    stays 'open' because an asked-and-ignored question is not answered; when the
    user does engage, mark_answered closes it properly.

    QA 2026-08-03: without this the same gap was asked three turns running,
    reworded each time so the verbatim loop guard never saw it.
    """
    if not gap_row_id:
        return
    try:
        service_client().table("rapport_gaps").update(
            {"chat_asked_at": _now(), "updated_at": _now()}
        ).eq("gap_row_id", gap_row_id).execute()
    except Exception:
        # Pre-20260928 environments have no chat_asked_at. Never fail the turn
        # over bookkeeping — worst case the gap stays askable, as it does today.
        logger.warning("rapport: mark_chat_asked failed for %s", gap_row_id, exc_info=True)


def _normalize_question(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").casefold()).strip()


def _trailing_question(utterance: str) -> str:
    """The question Lana actually ended on. Her replies are warm-up + question
    ("You really do love their pizza! Out of curiosity, is there a favorite
    dish…?"), so the lead-in must not dilute the comparison."""
    parts = [p for p in re.split(r"(?<=[.!?])\s+", str(utterance or "").strip()) if p]
    for part in reversed(parts):
        if part.rstrip().endswith("?"):
            return part
    return parts[-1] if parts else ""


def mark_chat_asked_if_reused(user_id: str, utterance: str) -> str | None:
    """Stamp a queued gap that this reply asked WITHOUT declaring itself an ask_gap.

    mark_chat_asked only ever ran for kind='ask_gap'. The policy can ask a queued
    question as a plain `reply` with goal_id null — it did exactly that twice in a
    row (prod 2026-08-05 21:30:42 and 21:31:05, its own `why` naming "the queued
    question about their favorite dish at The Piazza Italia"). Nothing stamped, so
    the gap stayed offerable and the home tile served the SAME question six
    minutes later, after the user had already answered it in chat.

    So the stamp is keyed on what the user actually READ, not on the label the
    model chose to attach. Verbatim reuse is caught by containment; a reworded ask
    by ratio on the trailing question only.
    """
    if not user_id or not str(utterance or "").strip():
        return None
    asked_norm = _normalize_question(utterance)
    tail_norm = _normalize_question(_trailing_question(utterance))
    if not asked_norm:
        return None
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id, question, chat_asked_at, answered_at")
            .eq("user_id", user_id)
            .eq("status", "open")
            .limit(40)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.warning("rapport: mark_chat_asked_if_reused lookup failed", exc_info=True)
        return None
    for row in rows:
        if row.get("chat_asked_at") or row.get("answered_at"):
            continue
        q = _normalize_question(row.get("question"))
        if len(q) < 12:
            continue
        hit = q in asked_norm
        if not hit and tail_norm:
            hit = SequenceMatcher(None, q, tail_norm).ratio() >= 0.7
        if hit:
            gid = str(row.get("gap_row_id") or "")
            mark_chat_asked(gid)
            logger.info("rapport: chat_asked stamped from a non-ask_gap turn %s", gid)
            return gid
    return None


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


# The catalogue gaps that need NO prior knowledge of the user to make sense, and
# which land in four DIFFERENT buckets — so answering them walks a zero-claim user
# straight to a matchable profile rather than deep into one topic.
#
# GAP_TREE was retired as an OPENER (open_semantic_gap owns that; see the module
# docstring) and this does not revive it: these three fire only for a user with no
# claims and no gaps at all, when even rapport_local_supply came back empty — the
# genuinely-first user in an area. Everyone else is served AI-written questions.
#
# language_home is deliberately NOT here: rapport_synth already injects a standing
# `languages_spoken` thread first for every user, it is AI-phrased, and it is wired
# to the language-switch offer. Two questions about the same thing is one too many.
COLD_SEED_GAP_IDS = ("relocation_recency", "daily_rhythm", "free_windows")


def open_cold_seed_gaps(user_id: str) -> int:
    """Open the no-prior-knowledge catalogue seeds. Returns how many were created.

    Uses the tree's REAL covers_concept (unlike semantic gaps, whose synthetic one
    never matches), so reconcile_gaps() retires a seed the moment the user states
    the fact in chat instead — the seed never becomes a stale question about
    something Lana already knows.
    """
    if not user_id:
        return 0
    existing = _existing_gap_rows(user_id)
    sb = service_client()
    opened = 0
    for gap_id in COLD_SEED_GAP_IDS:
        if gap_id in existing:
            continue
        gap = get_gap(gap_id)
        if not gap:
            continue
        try:
            sb.table("rapport_gaps").insert(
                {
                    "user_id": user_id,
                    "gap_id": gap_id,
                    "parent_bucket": gap["parent_bucket"],
                    "covers_concept": gap["covers_concept"],
                    "why_frame": render_why_frame(gap, None),
                    "question": gap["question"],
                    # A seed opens a bucket the user has nothing in, which is the
                    # highest-value question shape there is for a new user.
                    "unlock_score": P_NEW_BUCKET,
                    "status": "open",
                }
            ).execute()
            opened += 1
        except Exception:
            # unique(user_id, gap_id) race, or a pre-question-column env — either
            # way the seed already exists or cannot exist. Never fail the render.
            logger.debug("rapport: cold seed %s exists/race for %s", gap_id, user_id)
    if opened:
        logger.info("rapport: opened %d cold seed gap(s) for %s", opened, user_id)
    return opened
