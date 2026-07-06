"""Rapport gap lifecycle — open / suppress / close / skip / mute.

This is a deterministic reconciliation pass over (the user's active identity claims × the
static gap tree). No LLM call here: the facet parsing already happened upstream
(app/claims_persist.py writes the claims). We only decide which follow-up gaps make sense
to have open right now.

  open    — a captured claim unlocks a follow-up we don't already have a row for.
  suppress— never open a gap for something already known (exact concept, or same
            normalized label as an existing claim).
  close   — the concept a gap "covers" now exists as a claim → mark it answered.

Every write is idempotent, so this is safe to run fire-and-forget on every turn.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.auth import service_client
from app.rapport_gap_tree import GAP_TREE, gaps_for_bucket, get_gap, render_why_frame

logger = logging.getLogger(__name__)


def _normalized_label_key(label: str) -> str:
    """Collapse casing/punctuation/trailing-plural so near-identical labels merge.

    Kept in sync with app.claims_persist._normalized_label_key; inlined (not imported) so
    rapport stays free of that module's heavy import chain (a pre-existing circular import
    via app.orchestrator makes claims_persist unsafe to import in isolation).
    """
    text = re.sub(r"[^a-z0-9\s]", " ", str(label or "").lower())
    tokens = [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in text.split()]
    return " ".join(tokens).strip()

# Statuses that mean "this gap already has a life" — never re-open over them.
_TERMINAL_OR_LIVE = frozenset(
    {"open", "asked", "answered", "skipped", "muted_by_user", "expired"}
)


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


def reconcile_gaps(user_id: str, message_id: str | None = None) -> None:
    """Open/suppress/close gaps for a user based on their current claims. Idempotent."""
    if not user_id:
        return
    claims = _active_claims(user_id)
    known_concepts = {c["concept"] for c in claims if c.get("concept")}
    known_label_keys = {_normalized_label_key(c.get("label") or "") for c in claims}
    # Lowercased blob of every claim's label + concept, for keyword-gated gaps.
    claims_blob = " ".join(
        f"{c.get('label') or ''} {c.get('concept') or ''}" for c in claims
    ).lower()
    existing = _existing_gap_rows(user_id)
    sb = service_client()

    # 1) CLOSE — any open/asked gap whose covered concept now exists as a claim.
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

    # 2) OPEN — for each claim, unlock the gaps for its bucket that we don't have yet
    #    and don't already know the answer to.
    # Pick the highest-confidence claim per bucket to source the tile copy ("Morning Run").
    best_by_bucket: dict[str, dict[str, Any]] = {}
    for c in claims:
        b = c.get("bucket")
        if not b:
            continue
        cur = best_by_bucket.get(b)
        if cur is None or float(c.get("confidence") or 0) > float(cur.get("confidence") or 0):
            best_by_bucket[b] = c

    for bucket, trigger_claim in best_by_bucket.items():
        for gap_id, gap in gaps_for_bucket(bucket):
            if gap_id in existing:
                continue  # already open/asked/answered/skipped/muted/expired — leave it
            covers = gap["covers_concept"]
            # Gate: some gaps only make sense once she's mentioned the topic (e.g. don't ask
            # about kids' ages unless she's actually referenced kids).
            kws = gap.get("requires_any_keyword")
            if kws and not any(kw in claims_blob for kw in kws):
                continue
            # Suppress: we already know this (exact concept, or same normalized label).
            if covers in known_concepts:
                continue
            if _normalized_label_key(covers.replace("_", " ")) in known_label_keys:
                continue
            why_frame = render_why_frame(gap, trigger_claim.get("label"))
            try:
                sb.table("rapport_gaps").insert(
                    {
                        "user_id": user_id,
                        "gap_id": gap_id,
                        "parent_bucket": bucket,
                        "covers_concept": covers,
                        "why_frame": why_frame,
                        "unlock_score": gap["unlock_score"],
                        "opened_from_message_id": message_id,
                        "status": "open",
                    }
                ).execute()
            except Exception:
                # A unique-violation just means a concurrent turn already opened it — fine.
                logger.debug("rapport: open gap %s skipped (exists/race)", gap_id)


def mark_answered(gap_row_id: str) -> None:
    """Close a gap because the user engaged with the ask this turn.

    reconcile_gaps closes gaps whose covered concept now exists as a claim, but a free-text
    answer may map to a differently-named concept (or none, if vague). We still don't want to
    re-ask a topic she just responded to, so close it directly by row id.
    """
    if not gap_row_id:
        return
    try:
        service_client().table("rapport_gaps").update(
            {"status": "answered", "answered_at": _now(), "updated_at": _now()}
        ).eq("gap_row_id", gap_row_id).execute()
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
    """Never ask this gap again. Persists a muted row (creating a stub if none exists)."""
    gap = get_gap(gap_id)
    if not gap:
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
            sb.table("rapport_gaps").update(
                {"status": "muted_by_user", "updated_at": _now()}
            ).eq("gap_row_id", existing.data[0]["gap_row_id"]).execute()
        else:
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
