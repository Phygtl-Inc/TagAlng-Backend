"""Demand-triggered host nudge — supply engine №2 for marketplace cold-start.

When >= 3 moms on a block saved the SAME unmet need (listening `meet_seek` signals,
grouped by category — else by normalized detail text), Lana nudges the best candidate
host: "Three moms near you want a weekday park morning. Want to host it? I'll handle
invites, RSVPs, and reminders." Accepting drops her into the existing host flow with the
need pre-filled (the CTA's message is "I want to host {need}", which the layer-1
classifier routes to sharing.host / host_meet like any typed hosting ask).

Candidate-host heuristic (uses what data actually exists on this branch):
  1. VERIFIED — the user has email_verified_at or phone_verified_at (only verified
     users can publish events, so an unverified nudge would dead-end at the gate).
  2. MATCHING INTEREST — she saved a signal in this very demand group (her own
     meet_seek is one of the >= 3): she wants the thing herself, which is the strongest
     "would host it" predictor we have.
  3. MOST ACTIVE — most local_signals saved on the block (the engagement proxy that
     exists; there is no event-count/last-seen rollup on this branch), tie-broken by
     user_id for determinism.
Cap: one nudge per host per 7 days, persisted in `host_nudges` (see migration
20260709120100) and enforced both at candidate selection and again at emit time.

Grouping/selection are pure (testable without a DB); `find_host_nudge_candidates`
fetches from Supabase only for the inputs the caller didn't inject. The emit path is
scripts/emit_host_nudges.py (cron/ops), which sends through app.notifications.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

NEED_THRESHOLD = 3
NUDGE_COOLDOWN_DAYS = 7

_COUNT_WORDS = {3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine"}


# ── Pure: demand grouping ────────────────────────────────────────────────────
def normalize_need_key(category: str | None, detail_text: str | None) -> str:
    """Grouping key for 'the same need': the signal's category when it has one, else the
    detail text lower/space-folded. Cheap and deterministic — the matcher's semantic
    machinery stays out of this v1 counting loop."""
    cat = str(category or "").strip().lower()
    if cat:
        return f"cat:{cat}"
    detail = re.sub(r"\s+", " ", str(detail_text or "").strip().lower())
    return f"detail:{detail[:80]}"


def group_unmet_needs(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group listening meet_seek signals by need; keep groups with >= NEED_THRESHOLD
    DISTINCT users (one mom saving the same ask three times is not demand). Returns
    [{need_key, need_label, count, user_ids}] sorted by count desc, key asc."""
    groups: dict[str, dict[str, Any]] = {}
    for sig in signals or []:
        if not isinstance(sig, dict):
            continue
        if str(sig.get("intent") or "") != "meet_seek":
            continue
        if str(sig.get("status") or "listening") != "listening":
            continue
        user_id = str(sig.get("user_id") or "").strip()
        detail = str(sig.get("detail_text") or "").strip()
        if not user_id or not detail:
            continue
        key = normalize_need_key(sig.get("category"), detail)
        g = groups.setdefault(key, {"need_key": key, "need_label": detail, "user_ids": set()})
        g["user_ids"].add(user_id)
    out = []
    for g in groups.values():
        if len(g["user_ids"]) >= NEED_THRESHOLD:
            out.append(
                {
                    "need_key": g["need_key"],
                    "need_label": g["need_label"],
                    "count": len(g["user_ids"]),
                    "user_ids": sorted(g["user_ids"]),
                }
            )
    out.sort(key=lambda g: (-g["count"], g["need_key"]))
    return out


# ── Pure: candidate pick ─────────────────────────────────────────────────────
def pick_candidate_host(
    need: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    exclude_user_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Best host for one need per the module heuristic. `candidates` rows carry
    {user_id, verified, activity_count}; matching interest = membership in the need's
    user_ids. `exclude_user_ids` = hosts inside the 7-day nudge cooldown."""
    in_need = set(need.get("user_ids") or [])
    pool = [
        c
        for c in candidates or []
        if isinstance(c, dict)
        and bool(c.get("verified"))
        and str(c.get("user_id") or "")
        and str(c["user_id"]) not in exclude_user_ids
    ]
    if not pool:
        return None
    pool.sort(
        key=lambda c: (
            0 if str(c["user_id"]) in in_need else 1,  # wants it herself first
            -int(c.get("activity_count") or 0),  #        then most active
            str(c["user_id"]),  #                          then deterministic
        )
    )
    return pool[0]


# ── Copy + CTA ───────────────────────────────────────────────────────────────
def host_nudge_copy(count: int, need_label: str) -> str:
    """The nudge sentence. count=3 reads exactly as the strategy line: 'Three moms near
    you want a weekday park morning. Want to host it? I'll handle invites, RSVPs, and
    reminders.'"""
    n = _COUNT_WORDS.get(int(count), str(int(count)))
    need = str(need_label or "").strip() or "the same thing"
    return (
        f"{n} moms near you want {need}. Want to host it? "
        "I'll handle invites, RSVPs, and reminders."
    )


def host_nudge_actions(need_label: str) -> list[dict[str, Any]]:
    """Bubble CTAs for the nudge. The primary's message is a plain hosting utterance the
    layer-1 classifier already routes to sharing.host/host_meet — so accepting drops the
    user into the existing host flow with the need pre-filled, no new lane needed."""
    from app.ui_actions import _action

    need = str(need_label or "").strip() or "it"
    return [
        _action(
            action_id="host_nudge_accept",
            label="Yes, I'll host it",
            message=f"I want to host {need}",
            style="primary",
        ),
        _action(
            action_id="host_nudge_pass",
            label="Not now",
            message="not now",
            style="secondary",
        ),
    ]


# ── Selection (pure when inputs injected; fetches otherwise) ────────────────
def find_host_nudge_candidates(
    block_id: str,
    *,
    signals: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    recently_nudged_user_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Demand pockets on `block_id` worth a host nudge, each with its picked host.

    Returns [{need_key, need_label, count, host_user_id, host_nickname, copy}].
    One nudge per host per pass: a host picked for one need is excluded from the next
    (alongside anyone nudged in the last NUDGE_COOLDOWN_DAYS)."""
    if signals is None:
        signals = _fetch_block_meet_signals(block_id)
    if candidates is None:
        candidates = _fetch_block_candidates(block_id, signals)
    if recently_nudged_user_ids is None:
        recently_nudged_user_ids = _fetch_recently_nudged(block_id)

    taken: set[str] = set(recently_nudged_user_ids)
    out: list[dict[str, Any]] = []
    for need in group_unmet_needs(signals):
        host = pick_candidate_host(need, candidates, exclude_user_ids=taken)
        if not host:
            continue
        taken.add(str(host["user_id"]))
        out.append(
            {
                "need_key": need["need_key"],
                "need_label": need["need_label"],
                "count": need["count"],
                "host_user_id": str(host["user_id"]),
                "host_nickname": str(host.get("nickname") or "").strip() or None,
                "copy": host_nudge_copy(need["count"], need["need_label"]),
            }
        )
    return out


def _fetch_block_meet_signals(block_id: str) -> list[dict[str, Any]]:
    from app.auth import service_client

    try:
        res = (
            service_client()
            .table("local_signals")
            .select("user_id, intent, category, detail_text, status")
            .eq("block_id", block_id)
            .eq("intent", "meet_seek")
            .eq("status", "listening")
            .execute()
        )
        return [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        return []


def _fetch_block_candidates(
    block_id: str, signals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Verified users homed on the block, with activity_count = their signals saved on
    the block (any intent) — see the heuristic note in the module docstring."""
    from app.auth import service_client

    sb = service_client()
    try:
        users = (
            sb.table("users")
            .select("id, nickname, phone_verified_at, email_verified_at")
            .eq("home_block_id", block_id)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        return []
    try:
        activity_rows = (
            sb.table("local_signals")
            .select("user_id")
            .eq("block_id", block_id)
            .execute()
            .data
            or []
        )
    except Exception:  # noqa: BLE001
        activity_rows = [{"user_id": s.get("user_id")} for s in signals]
    activity: dict[str, int] = {}
    for row in activity_rows:
        uid = str((row or {}).get("user_id") or "")
        if uid:
            activity[uid] = activity.get(uid, 0) + 1
    return [
        {
            "user_id": str(u.get("id")),
            "nickname": u.get("nickname"),
            "verified": bool(u.get("email_verified_at") or u.get("phone_verified_at")),
            "activity_count": activity.get(str(u.get("id")), 0),
        }
        for u in users
        if isinstance(u, dict) and u.get("id")
    ]


def _fetch_recently_nudged(block_id: str) -> set[str]:
    from app.auth import service_client

    since = (datetime.now(timezone.utc) - timedelta(days=NUDGE_COOLDOWN_DAYS)).isoformat()
    try:
        rows = (
            service_client()
            .table("host_nudges")
            .select("user_id")
            .eq("block_id", block_id)
            .gte("created_at", since)
            .execute()
            .data
            or []
        )
        return {str(r.get("user_id")) for r in rows if isinstance(r, dict) and r.get("user_id")}
    except Exception:  # noqa: BLE001
        return set()


def _host_inside_cooldown(user_id: str) -> bool:
    """Emit-time re-check of the cap (selection already filters, but the emit script may
    run against stale selections — never double-nudge a host inside the window)."""
    from app.auth import service_client

    since = (datetime.now(timezone.utc) - timedelta(days=NUDGE_COOLDOWN_DAYS)).isoformat()
    try:
        rows = (
            service_client()
            .table("host_nudges")
            .select("id")
            .eq("user_id", user_id)
            .gte("created_at", since)
            .limit(1)
            .execute()
            .data
            or []
        )
        return bool(rows)
    except Exception:  # noqa: BLE001
        return True  # can't verify the cap — fail closed, don't nudge


# ── Emit (used by scripts/emit_host_nudges.py) ──────────────────────────────
def emit_host_nudges(block_id: str, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Send the block's due host nudges through the existing notification machinery
    (push + email via notify_user) and persist each to host_nudges for the 7-day cap.
    Returns the nudges acted on (with 'sent': bool)."""
    from app.auth import service_client
    from app.notifications import email_html, notify_user

    results: list[dict[str, Any]] = []
    for nudge in find_host_nudge_candidates(block_id):
        entry = dict(nudge)
        if dry_run:
            entry["sent"] = False
            results.append(entry)
            continue
        if _host_inside_cooldown(nudge["host_user_id"]):
            continue
        service_client().table("host_nudges").insert(
            {
                "user_id": nudge["host_user_id"],
                "block_id": block_id,
                "need_key": nudge["need_key"],
                "need_label": nudge["need_label"],
                "signal_count": nudge["count"],
            }
        ).execute()
        notify_user(
            nudge["host_user_id"],
            title="Your block needs a host",
            body=nudge["copy"],
            url="/",
            email_subject="Neighbors near you want this — want to host it?",
            email_html=email_html(
                "Your block needs a host",
                nudge["copy"],
                cta_label="Host it with Lana",
                cta_path="/",
            ),
        )
        entry["sent"] = True
        results.append(entry)
    return results
