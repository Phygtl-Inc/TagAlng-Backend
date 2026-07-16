"""Rapport ranker — pick the single best follow-up gap for the home "By the way…" tile.

Deterministic on purpose (no AI in the home-render hot path):
  * frequency cap — env-tunable min-gap + rolling-7-day ceiling (keyed on asked_at).
  * tier gate      — HIGH-sensitivity gaps require the mom to have warmed into the
                     community (reached >= acquaintance with anyone).
  * score          — unlock_score decayed by prior skips; oldest-open breaks ties.
Muted/answered/expired gaps are simply not 'open', so they never appear as candidates.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.analytics import track
from app.auth import service_client
from app.rapport_gap_tree import get_gap

logger = logging.getLogger(__name__)

# Cadence caps (STRATEGY doc §4: "Frequency capped (1/session, 3/rolling-7-days)").
# Tunable without code changes; set both to 0 to effectively "always show" when a gap is open.
_DEFAULT_MIN_HOURS = 6.0   # min gap between NEW asks (approximates once-per-session)
_DEFAULT_MAX_PER_7D = 3    # rolling 7-day ceiling (the doc's "3/rolling-7-days")

# relationship_tier enum order (see 20260613120000_social_graph_lana_tools.sql).
_TIER_RANK = {"stranger": 0, "nudge": 1, "acquaintance": 2, "direct": 3, "irl_peer": 4}
# HIGH-sensitivity gaps require at least this much warmth.
_HIGH_MIN_RANK = _TIER_RANK["acquaintance"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _recently_asked(user_id: str) -> bool:
    """True if the cadence caps block a NEW ask.

    Two knobs (env-tunable): LANA_RAPPORT_MIN_HOURS (min gap between new asks) and
    LANA_RAPPORT_MAX_PER_7D (rolling 7-day ceiling). Either at 0 disables that check;
    both at 0 → a new ask surfaces whenever one is open ("always show"). A still-pending ask
    is re-shown regardless (see next_ask) — these caps only gate brand-new asks.
    """
    min_hours = _env_float("LANA_RAPPORT_MIN_HOURS", _DEFAULT_MIN_HOURS)
    max_7d = int(_env_float("LANA_RAPPORT_MAX_PER_7D", _DEFAULT_MAX_PER_7D))
    try:
        sb = service_client()
        if min_hours > 0:
            cutoff = (_now() - timedelta(hours=min_hours)).isoformat()
            r = (
                sb.table("rapport_gaps")
                .select("gap_row_id")
                .eq("user_id", user_id)
                .gte("asked_at", cutoff)
                .limit(1)
                .execute()
            )
            if r.data:
                return True
        if max_7d > 0:
            week = (_now() - timedelta(days=7)).isoformat()
            r = (
                sb.table("rapport_gaps")
                .select("gap_row_id", count="exact")
                .eq("user_id", user_id)
                .gte("asked_at", week)
                .execute()
            )
            count = r.count if getattr(r, "count", None) is not None else len(r.data or [])
            if count >= max_7d:
                return True
        return False
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


def _build(
    row: dict[str, Any], gap: dict[str, Any] | None, lang: str | None = None
) -> dict[str, Any]:
    # `gap` is the static gap-tree entry, or None for dynamic (semantic) gaps opened from the
    # extractor's follow-up. Prefer the question stored on the row; fall back to the tree.
    gap = gap or {}
    question = row.get("question") or gap.get("question", "")
    why_frame = row.get("why_frame") or ""
    if lang and lang != "en":
        # Questions are stored English-canonical and rendered into the user's language at
        # WRITE time (question_i18n) — serving is a lookup, never an LLM wait. A miss (race
        # right after a language switch, or a pre-i18n row) serves English once and kicks a
        # background render so the next fetch has it.
        i18n = row.get("question_i18n")
        entry = i18n.get(lang) if isinstance(i18n, dict) else None
        if isinstance(entry, dict) and entry.get("question"):
            question = str(entry["question"])
            why_frame = str(entry.get("why_frame") or why_frame)
        elif question:
            try:
                from app.rapport_i18n import localize_gap_row_async

                localize_gap_row_async(
                    row["gap_row_id"], question, why_frame, lang, i18n
                )
            except Exception:  # noqa: BLE001 — self-heal is best-effort
                logger.exception("rapport: i18n self-heal kickoff failed")
    return {
        "gap_row_id": row["gap_row_id"],
        "gap_id": row["gap_id"],
        "parent_bucket": row["parent_bucket"],
        "why_frame": why_frame,
        "question": question,
        "sensitivity_tier": gap.get("sensitivity_tier", "LOW"),
        "chip_color_token": f"--d-{row['parent_bucket']}",
    }


def _load_open_rows(user_id: str) -> list[dict[str, Any]]:
    try:
        return (
            service_client()
            .table("rapport_gaps")
            .select("*")
            .eq("user_id", user_id)
            .eq("status", "open")
            .execute()
        ).data or []
    except Exception:
        logger.exception("rapport: candidate load failed for %s", user_id)
        return []


def _build_candidates(
    rows: list[dict[str, Any]], tier_rank: int
) -> list[tuple[float, str, dict[str, Any], dict[str, Any]]]:
    out: list[tuple[float, str, dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        gap = get_gap(row["gap_id"])  # None for dynamic (semantic) gaps — that's fine
        tier = (gap or {}).get("sensitivity_tier", "LOW")
        if tier == "HIGH" and tier_rank < _HIGH_MIN_RANK:
            continue
        # opened_at ascending as tie-breaker → oldest topic asked first.
        out.append((_score(row), row.get("opened_at") or "", row, gap))
    return out


def _backfill_from_claims(user_id: str) -> bool:
    """Synthesize fresh gaps from the user's claims so the tile is never empty. Best-effort;
    the LLM call is deferred-imported so it only loads when the plate is actually empty."""
    try:
        from app.rapport_synth import synthesize_gaps_from_claims

        return synthesize_gaps_from_claims(user_id) > 0
    except Exception:
        logger.exception("rapport: claim backfill failed for %s", user_id)
        return False


def _preferred_lang(user_id: str) -> str | None:
    """users.locale, or None — one indexed single-row read on the home render."""
    try:
        from app.lang_pref import get_user_preferred_language

        return get_user_preferred_language(user_id)
    except Exception:  # noqa: BLE001 — language must never break the tile
        logger.exception("rapport: preferred-lang lookup failed for %s", user_id)
        return None


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


def next_ask(
    user_id: str, surface: str = "homescreen", cycle: bool = False
) -> dict[str, Any] | None:
    """Return the ask to show, or None.

    Idempotent by default: while an ask is still pending (shown but not yet answered or
    skipped) it is re-returned as-is, so reloads and React's double-invoked effects keep
    showing the SAME tile instead of consuming it and tripping the frequency cap. A brand-new
    ask is only chosen (and marked 'asked') when nothing is pending and the 24h cap is clear.

    `cycle=True` is the user tapping the tile's refresh (⟳): retire the current pending ask
    and hand over a *different* one immediately, **bypassing the 24h cap** — the cap exists to
    stop automatic nagging, not an explicit request for another question.
    """
    if not user_id:
        return None

    # The tile lives on the home screen, outside any chat session — the persisted
    # preference (users.locale) is the language authority, not the session-sticky lang.
    lang = _preferred_lang(user_id)

    exclude_id: str | None = None
    if cycle:
        pending = _pending_ask(user_id)
        if pending:
            exclude_id = pending.get("gap_row_id")
            try:
                service_client().rpc(
                    "increment_skip_and_reopen", {"p_gap_row_id": exclude_id}
                ).execute()
            except Exception:
                logger.exception("rapport: cycle-skip failed for %s", user_id)
    else:
        # 1) Re-show a still-pending ask — no re-mark, no duplicate impression event.
        #    Works for dynamic semantic gaps too (get_gap returns None → _build tolerates it).
        pending = _pending_ask(user_id)
        if pending:
            return _build(pending, get_gap(pending["gap_id"]), lang)

        # 2) Daily cap — something was asked (and since answered/skipped) within the window.
        if _recently_asked(user_id):
            return None

    tier_rank = _max_tier_rank(user_id)
    rows = _load_open_rows(user_id)
    candidates = _build_candidates(rows, tier_rank)
    if not candidates:
        # Plate is empty (no open gaps, or all gated) and the cadence cap is clear — synthesize
        # fresh follow-ups from what we already know, so there's always something to build on.
        if _backfill_from_claims(user_id):
            rows = _load_open_rows(user_id)
            candidates = _build_candidates(rows, tier_rank)
    if not candidates:
        return None

    # When cycling, prefer any gap other than the one we just retired. If that leaves nothing
    # (the retired ask was the only one), synthesize a fresh alternative rather than re-showing it.
    fresh = [c for c in candidates if c[2].get("gap_row_id") != exclude_id]
    if exclude_id and not fresh and _backfill_from_claims(user_id):
        candidates = _build_candidates(_load_open_rows(user_id), tier_rank)
        fresh = [c for c in candidates if c[2].get("gap_row_id") != exclude_id]
    pool = fresh or candidates
    pool.sort(key=lambda t: (-t[0], t[1]))
    score, _opened, row, gap = pool[0]

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

    return _build(row, gap, lang)
