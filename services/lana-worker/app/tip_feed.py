"""Recent recommendations: the browse companion to asking (C-FIND-RECENT).

Reading a neighbour's tip used to require asking for one — find_neighbor_tips scores tips
against a specific request inside a chat turn. This is the same rows, browsed: newest
first, or only from people the reader shares a circle with, or nearest.

Two feedback verbs, kept apart on purpose:

    ✓ I vouch   adds YOUR voice to the recommendation ("I know this place too")
    👍 Helpful  rates the ANSWER, and says nothing about the place

The vouch count is the number a stranger reads as social proof, so a reader who has never
been there must not be able to raise it. Enforced in SQL (set_tip_vouch refuses the
author's own tip); mirrored here only in the error the caller sees.
"""

from __future__ import annotations

import logging
from typing import Any

from app.supabase_rpc import call_rpc

logger = logging.getLogger(__name__)

# The three tabs on the feed. Anything else is read as "recent" rather than erroring —
# an unknown tab is a client that shipped ahead of us, not a reason to show nothing.
FILTERS = ("recent", "circles", "nearest")

PAGE_SIZE = 20


def _clean_circles(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        pid = str(c.get("place_id") or "").strip()
        name = str(c.get("name") or "").strip()
        if not pid or not name:
            continue
        out.append(
            {
                "place_id": pid,
                "name": name,
                "circle_type": str(c.get("circle_type") or "").strip() or None,
            }
        )
    return out[:3]


def _row(raw: dict[str, Any]) -> dict[str, Any] | None:
    sid = str(raw.get("signal_id") or "").strip()
    text = str(raw.get("detail_text") or "").strip()
    if not sid or not text:
        return None
    circles = _clean_circles(raw.get("shared_circles"))
    tags = raw.get("affinity_tags")
    return {
        "signal_id": sid,
        "detail_text": text,
        "category": str(raw.get("category") or "").strip() or None,
        "trait_tags": [str(t).strip() for t in tags if str(t or "").strip()][:6]
        if isinstance(tags, list)
        else [],
        "created_at": str(raw.get("created_at") or "") or None,
        "peer_user_id": str(raw.get("peer_user_id") or "").strip() or None,
        "nickname": str(raw.get("neighbor_label") or "").strip() or None,
        "avatar_url": str(raw.get("avatar_url") or "").strip() or None,
        "distance_text": str(raw.get("distance_text") or "").strip() or None,
        # The shared circle labels the card in the "My circles" tab — the reason this tip
        # is worth more than a stranger's. Empty on the Recent tab's unconnected rows.
        "shared_circles": circles,
        "same_block": bool(raw.get("same_block")),
        "vouch_count": int(raw.get("vouch_count") or 0),
        "helpful_count": int(raw.get("helpful_count") or 0),
        "i_vouched": bool(raw.get("i_vouched")),
        "i_marked_helpful": bool(raw.get("i_marked_helpful")),
    }


def recent_tips(
    user_jwt: str,
    *,
    tab: str = "recent",
    limit: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """One page of the feed. [] on any failure — a browse surface must not error out."""
    wanted = str(tab or "recent").strip().lower()
    if wanted not in FILTERS:
        wanted = "recent"
    try:
        raw = call_rpc(
            user_jwt,
            "recent_neighbor_tips",
            {"p_filter": wanted, "p_limit": max(1, min(int(limit or PAGE_SIZE), 50))},
        )
    except Exception:
        logger.exception("recent_tips_failed tab=%s", wanted)
        return []
    rows = [_row(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    out = [r for r in rows if r]
    logger.info("recent_tips tab=%s rows=%d", wanted, len(out))
    return out


def set_vouch(user_jwt: str, *, signal_id: str, on: bool = True) -> int:
    """Add or remove the caller's vouch. Returns the new count."""
    raw = call_rpc(user_jwt, "set_tip_vouch", {"p_signal_id": signal_id, "p_on": bool(on)})
    return int(raw or 0) if isinstance(raw, (int, float)) else 0


def set_helpful(user_jwt: str, *, signal_id: str, on: bool = True) -> int:
    """Add or remove the caller's "helpful" mark. Returns the new count."""
    raw = call_rpc(user_jwt, "set_tip_helpful", {"p_signal_id": signal_id, "p_on": bool(on)})
    return int(raw or 0) if isinstance(raw, (int, float)) else 0
