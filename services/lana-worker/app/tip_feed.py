"""Recent recommendations: the browse companion to asking (C-FIND-RECENT).

Reading a neighbour's tip used to require asking for one — find_neighbor_tips scores tips
against a specific request inside a chat turn. This is the same rows, browsed: newest
first, or only from people the reader shares a circle with, or nearest.

Every row is FIELDS, not a sentence: name / category / reco_type / place / description /
the answered steps. detail_text is still carried for tips captured before those columns
existed (20261120120000 backfilled only the name out of it) — new readers should render
the fields and treat detail_text as the legacy fallback it is.

One feedback verb with a direction: 👍 / 👎 both rate the ANSWER. A reader has ONE vote
per tip and it flips, so a card never shows the same person on both sides.
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


def _clean_fields(raw: Any) -> list[dict[str, Any]]:
    """The answered steps, self-describing (field/label/question/answer). Kept as an array
    because the questions are generated per recommendation: an answer without the label it
    was asked under is unreadable on a card."""
    out: list[dict[str, Any]] = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        answer = str(f.get("answer") or "").strip()
        label = str(f.get("label") or "").strip()
        if not answer or not label:
            continue
        out.append(
            {
                "field": str(f.get("field") or "").strip() or None,
                "label": label,
                "question": str(f.get("question") or "").strip() or None,
                "kind": str(f.get("kind") or "").strip() or "text",
                "answer": answer,
            }
        )
    return out


# The steps that answer "where is it" for the types that ask one. Read only as a fallback
# for rows with no reco_place: the author naming a neighbourhood always wins.
_PLACE_FIELDS = ("where", "where_to_buy", "location")


def _place(raw: dict[str, Any], fields: list[dict[str, Any]]) -> str | None:
    named = str(raw.get("reco_place") or "").strip()
    if named:
        return named
    for f in fields:
        if f["field"] in _PLACE_FIELDS:
            return f["answer"]
    return None


def _row(raw: dict[str, Any]) -> dict[str, Any] | None:
    sid = str(raw.get("signal_id") or "").strip()
    legacy = str(raw.get("detail_text") or "").strip()
    name = str(raw.get("reco_name") or "").strip()
    # A card needs something to title itself with. Pre-20261120 rows have the name
    # backfilled out of detail_text, so "neither" means a row nothing can render.
    if not sid or not (name or legacy):
        return None
    fields = _clean_fields(raw.get("reco_fields"))
    return {
        "signal_id": sid,
        "name": name or None,
        # The specific kind the card labels the tip with ("pediatric dentist"); reco_type
        # is the coarse taxonomy bucket the browse indexes on ("professional").
        "category": str(raw.get("category") or "").strip() or None,
        "reco_type": str(raw.get("reco_type") or "").strip() or None,
        "place": _place(raw, fields),
        "description": str(raw.get("reco_description") or "").strip() or None,
        "fields": fields,
        # Legacy only: the " · "-joined sentence tips were captured as before the fields
        # existed. Render the fields above when they are there.
        "detail_text": legacy or None,
        "created_at": str(raw.get("created_at") or "") or None,
        "peer_user_id": str(raw.get("peer_user_id") or "").strip() or None,
        "nickname": str(raw.get("neighbor_label") or "").strip() or None,
        "avatar_url": str(raw.get("avatar_url") or "").strip() or None,
        "distance_text": str(raw.get("distance_text") or "").strip() or None,
        # The shared circle labels the card in the "My circles" tab — the reason this tip
        # is worth more than a stranger's. Empty on the Recent tab's unconnected rows.
        "shared_circles": _clean_circles(raw.get("shared_circles")),
        "same_block": bool(raw.get("same_block")),
        "helpful_count": int(raw.get("helpful_count") or 0),
        "unhelpful_count": int(raw.get("unhelpful_count") or 0),
        "i_marked_helpful": bool(raw.get("i_marked_helpful")),
        "i_marked_unhelpful": bool(raw.get("i_marked_unhelpful")),
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


def set_helpful(
    user_jwt: str, *, signal_id: str, on: bool = True, helpful: bool = True
) -> dict[str, Any]:
    """Set the caller's 👍/👎 on a tip. `on=False` clears it whichever way it pointed.
    Returns both counts and the caller's own state, so the tapped row re-renders without
    re-reading the feed."""
    raw = call_rpc(
        user_jwt,
        "set_tip_helpful",
        {"p_signal_id": signal_id, "p_on": bool(on), "p_helpful": bool(helpful)},
    )
    out = raw if isinstance(raw, dict) else {}
    return {
        "helpful_count": int(out.get("helpful_count") or 0),
        "unhelpful_count": int(out.get("unhelpful_count") or 0),
        "i_marked_helpful": bool(out.get("i_marked_helpful")),
        "i_marked_unhelpful": bool(out.get("i_marked_unhelpful")),
    }
