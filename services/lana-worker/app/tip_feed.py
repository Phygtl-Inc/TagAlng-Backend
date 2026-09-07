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
    circle_place_id: str | None = None,
) -> list[dict[str, Any]]:
    """One page of the feed. [] on any failure — a browse surface must not error out.

    With `circle_place_id` this is ONE community's recommendations: no distance bound and
    no tabs (the tabs belong to the area screen). Without it, tips shared into a community
    are excluded — they were meant for that community, not for the neighbourhood.
    """
    wanted = str(tab or "recent").strip().lower()
    if wanted not in FILTERS:
        wanted = "recent"
    payload: dict[str, Any] = {
        "p_filter": wanted,
        "p_limit": max(1, min(int(limit or PAGE_SIZE), 50)),
    }
    if circle_place_id:
        payload["p_circle_place_id"] = str(circle_place_id)
    try:
        raw = call_rpc(user_jwt, "recent_neighbor_tips", payload)
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


def tip_by_id(signal_id: str, *, viewer_user_id: str | None = None) -> dict[str, Any] | None:
    """ONE recommendation, by id — what a shared link opens (§39).

    The feed is scoped to the reader's own block/radius, so a signal_id a client already
    holds could not be turned into anything a second person could read. This is the same
    row, read by id alone: a signal id is an unguessable uuid, so the link discloses
    exactly that one recommendation and nothing about the block behind it — which is why
    nothing is minted and nothing is stored.

    Caller-relative fields come back empty on purpose: distance and shared circles are
    meaningless when the viewer may share nothing at all with the author. Withdrawn
    (status <> listening) is a miss; EXPIRED is not — expires_at is feed freshness, and a
    recommendation a neighbour passed along should not die on day 15.
    """
    from app.auth import service_client
    from app.community_surface import _blocked_ids

    sid = str(signal_id or "").strip()
    if not sid:
        return None
    sb = service_client()
    try:
        res = (
            sb.table("local_signals")
            .select(
                "id, category, reco_name, reco_type, reco_place, reco_description, "
                "reco_fields, detail_text, created_at, user_id, intent, status"
            )
            .eq("id", sid)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("tip_by_id_failed signal=%s", sid)
        return None
    raw = (res.data or [None])[0]
    if not raw or raw.get("intent") != "tip_share" or raw.get("status") != "listening":
        return None
    author = str(raw.get("user_id") or "")
    if viewer_user_id and author and author in _blocked_ids(viewer_user_id, [author]):
        return None

    label, avatar = "A neighbor", None
    votes: list[dict[str, Any]] = []
    try:
        prof = (
            sb.table("users")
            .select("nickname, profile_photo_url")
            .eq("id", author)
            .limit(1)
            .execute()
        )
        row = (prof.data or [{}])[0] or {}
        label = str(row.get("nickname") or "").strip() or "A neighbor"
        avatar = row.get("profile_photo_url")
        # One read, counted in Python — a shared link is a single row, not a page.
        got = sb.table("tip_helpful").select("user_id, is_helpful").eq("signal_id", sid).execute()
        votes = got.data if isinstance(got.data, list) else []
    except Exception:
        logger.exception("tip_by_id_side_read_failed signal=%s", sid)

    mine = [v for v in votes if str(v.get("user_id")) == str(viewer_user_id or "")]
    return _row(
        {
            **raw,
            "signal_id": sid,
            "peer_user_id": author,
            "neighbor_label": label,
            "avatar_url": avatar,
            "helpful_count": sum(1 for v in votes if v.get("is_helpful")),
            "unhelpful_count": sum(1 for v in votes if not v.get("is_helpful")),
            "i_marked_helpful": any(v.get("is_helpful") for v in mine),
            "i_marked_unhelpful": any(not v.get("is_helpful") for v in mine),
        }
    )
