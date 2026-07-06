"""Rapport ranker — pick the single best follow-up gap for the home "By the way…" tile.

Deterministic on purpose (no AI in the home-render hot path):
  * frequency cap — at most one ask per 24h (keyed on rapport_gaps.asked_at).
  * tier gate      — HIGH-sensitivity gaps require the mom to have warmed into the
                     community (reached >= acquaintance with anyone).
  * score          — unlock_score decayed by prior skips; oldest-open breaks ties.
Muted/answered/expired gaps are simply not 'open', so they never appear as candidates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.analytics import track
from app.auth import service_client
from app.rapport_gap_tree import get_gap

logger = logging.getLogger(__name__)

FREQ_CAP_HOURS = 24

# relationship_tier enum order (see 20260613120000_social_graph_lana_tools.sql).
_TIER_RANK = {"stranger": 0, "nudge": 1, "acquaintance": 2, "direct": 3, "irl_peer": 4}
# HIGH-sensitivity gaps require at least this much warmth.
_HIGH_MIN_RANK = _TIER_RANK["acquaintance"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recently_asked(user_id: str) -> bool:
    """True if any gap was shown to this user within the frequency-cap window."""
    cutoff = (_now() - timedelta(hours=FREQ_CAP_HOURS)).isoformat()
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("gap_row_id")
            .eq("user_id", user_id)
            .gte("asked_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception:
        logger.exception("rapport: freq-cap check failed for %s", user_id)
        return True  # fail closed — don't risk over-asking on a transient error


def _max_tier_rank(user_id: str) -> int:
    """Highest relationship tier this user has reached with anyone (proxy for warmth)."""
    try:
        res = (
            service_client()
            .table("relationship_tier_events")
            .select("to_tier")
            .or_(f"user_low.eq.{user_id},user_high.eq.{user_id}")
            .execute()
        )
        ranks = [_TIER_RANK.get(r.get("to_tier"), 0) for r in (res.data or [])]
        return max(ranks) if ranks else 0
    except Exception:
        logger.exception("rapport: tier lookup failed for %s", user_id)
        return 0


def _score(row: dict[str, Any]) -> float:
    unlock = float(row.get("unlock_score") or 0.0)
    skips = int(row.get("skipped_count") or 0)
    return max(0.0, unlock * (1.0 - 0.2 * skips))


def _build(row: dict[str, Any], gap: dict[str, Any]) -> dict[str, Any]:
    return {
        "gap_row_id": row["gap_row_id"],
        "gap_id": row["gap_id"],
        "parent_bucket": row["parent_bucket"],
        "why_frame": row["why_frame"],
        "question": gap.get("question", ""),
        "sensitivity_tier": gap["sensitivity_tier"],
        "chip_color_token": f"--d-{row['parent_bucket']}",
    }


def _pending_ask(user_id: str) -> dict[str, Any] | None:
    """A gap already shown and awaiting the user's action — re-show it verbatim."""
    try:
        res = (
            service_client()
            .table("rapport_gaps")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "asked")
            .order("asked_at", desc=True)
            .limit(1)
            .execute()
        )
        return (res.data or [None])[0]
    except Exception:
        logger.exception("rapport: pending-ask lookup failed for %s", user_id)
        return None


def next_ask(user_id: str, surface: str = "homescreen") -> dict[str, Any] | None:
    """Return the ask to show, or None.

    Idempotent: while an ask is still pending (shown but not yet answered or skipped) it is
    re-returned as-is, so reloads and React's double-invoked effects keep showing the SAME
    tile instead of consuming it and then tripping the frequency cap. A brand-new ask is only
    chosen (and marked 'asked') when nothing is pending and the 24h cap isn't in effect.
    """
    if not user_id:
        return None

    # 1) Re-show a still-pending ask — no re-mark, no duplicate impression event.
    pending = _pending_ask(user_id)
    if pending:
        gap = get_gap(pending["gap_id"])
        if gap:
            return _build(pending, gap)

    # 2) Daily cap — something was asked (and since answered/skipped) within the window.
    if _recently_asked(user_id):
        return None

    try:
        rows = (
            service_client()
            .table("rapport_gaps")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "open")
            .execute()
        ).data or []
    except Exception:
        logger.exception("rapport: candidate load failed for %s", user_id)
        return None
    if not rows:
        return None

    tier_rank = _max_tier_rank(user_id)

    candidates: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        gap = get_gap(row["gap_id"])
        if not gap:
            continue  # stale gap id no longer in the tree
        if gap["sensitivity_tier"] == "HIGH" and tier_rank < _HIGH_MIN_RANK:
            continue
        # opened_at ascending as tie-breaker → oldest topic asked first.
        candidates.append((_score(row), row.get("opened_at") or "", row, gap))
    if not candidates:
        return None

    candidates.sort(key=lambda t: (-t[0], t[1]))
    score, _opened, row, gap = candidates[0]

    try:
        service_client().table("rapport_gaps").update(
            {"status": "asked", "asked_at": _now().isoformat(), "updated_at": _now().isoformat()}
        ).eq("gap_row_id", row["gap_row_id"]).execute()
    except Exception:
        logger.exception("rapport: failed to mark gap asked for %s", user_id)
        return None

    track(
        "rapport_gap_shown",
        user_id=user_id,
        event_properties={"gap_id": row["gap_id"], "surface": surface, "score": round(score, 3)},
    )

    return _build(row, gap)
