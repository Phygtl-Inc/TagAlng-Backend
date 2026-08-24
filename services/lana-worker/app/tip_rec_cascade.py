"""Neighbor tips → ranked peer rows for the recommendation cascade (frontend §12 / #68).

The frame (C-FIND-MOM-RESULTS) puts the neighbor's ACTUAL recommendation on their row —
"Dr. Reyes at Lake Nona Smiles — so gentle with the toddlers". That quote is what makes the
row a pre-qualified answer instead of one more person to message, and until now the wire
carried it only inside Lana's prose. find_neighbor_tips v2 returns the rec, its author and
an honest distance; this module shapes those into peer_matches rows.

TRUTHFULNESS ([[truthful-peer-match-model]]). These rows are NOT claim-affinity matches and
must never be dressed as them: no stars, no band, no "You both:" label — nothing here
computed a similarity between two people. What the row honestly claims is exactly what
happened: this neighbor posted this recommendation, it matched this ask, and they are this
far away. trait_tags are the TIP's own affinity tags (what the rec is about), not shared
identity. `tip_rec: True` marks the row so peer-surface enrichment leaves it alone.

RE-RANK (§12c). "What matters most" weights are applied here, server-side, over a wider
fetch than the page the user was shown — the client-side version can only re-order rows it
already has. Weighting only ever re-orders; it never fabricates a match and never drops a
row, so a weight nobody satisfies leaves the list intact rather than emptying it.
"""

from __future__ import annotations

from typing import Any

from app.ui_actions import peer_card_nudge_action

# One page of rows. Fetch is deliberately wider (see WIDE_FETCH) so a re-rank has somewhere
# to reach — re-ordering only the visible five is the client-side behaviour we're replacing.
PAGE_SIZE = 5
WIDE_FETCH = 12


def _clean_tags(raw: Any, *, limit: int = 5) -> list[str]:
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        tag = str(item or "").strip()
        if len(tag) < 2 or tag in out:
            continue
        out.append(tag)
        if len(out) >= limit:
            break
    return out


def _strength(row: dict[str, Any]) -> float:
    try:
        return float(row.get("match_strength") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _shared_circles(raw: Any, *, limit: int = 3) -> list[dict[str, Any]]:
    """The places both the caller and this recommender belong to (find_neighbor_tips v3)."""
    out: list[dict[str, Any]] = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        pid = str(c.get("place_id") or "").strip()
        if not name or not pid:
            continue
        out.append(
            {
                "place_id": pid,
                "name": name,
                "circle_type": str(c.get("circle_type") or "").strip() or None,
            }
        )
        if len(out) >= limit:
            break
    return out


def _group(circles: list[dict[str, Any]], same_block: bool) -> dict[str, Any]:
    """Which heading this rec sits under (C-FIND-V2: "the grouping IS the explanation").

    A named shared circle wins over the block: "St Mary's Church" says more about why the
    rec is trustworthy than "lives near you" does. `block` and `nearby` are KEYS, not copy
    — the surface writes and translates the heading, the same rule the place cards follow.
    """
    if circles:
        first = circles[0]
        return {
            "group_key": first["place_id"],
            "group_label": first["name"],
            "group_kind": "circle",
        }
    if same_block:
        return {"group_key": "block", "group_label": None, "group_kind": "block"}
    return {"group_key": "nearby", "group_label": None, "group_kind": "nearby"}


def peer_rows_from_neighbor_tips(
    tips: list[dict[str, Any]],
    *,
    phone_verified: bool = True,
) -> list[dict[str, Any]]:
    """peer_matches rows carrying the rec itself. Rows without an author are dropped —
    a card with no one behind it cannot be attributed or replied to, and the prose reply
    already speaks every tip we found."""
    rows: list[dict[str, Any]] = []
    for tip in tips:
        if not isinstance(tip, dict):
            continue
        text = str(tip.get("detail_text") or "").strip()
        peer_id = str(tip.get("peer_user_id") or "").strip()
        if not text or not peer_id:
            continue
        nickname = str(tip.get("neighbor_label") or "").strip() or "A neighbor"
        circles = _shared_circles(tip.get("shared_circles"))
        row: dict[str, Any] = {
            "peer_user_id": peer_id,
            "nickname": nickname,
            "avatar_url": str(tip.get("avatar_url") or "").strip() or None,
            "tip_text": text,
            "tip_signal_id": str(tip.get("signal_id") or "").strip() or None,
            "distance_text": str(tip.get("distance_text") or "").strip() or None,
            "trait_tags": _clean_tags(tip.get("affinity_tags")),
            "match_strength": _strength(tip),
            # C-FIND-V2 groups results by the circle shared with the recommender, and
            # C-FIND-V2-DETAIL lists those circles on the voucher card. Shared only —
            # never this person's other memberships.
            "shared_circles": circles,
            "same_block": bool(tip.get("same_block")),
            # Marks this as a rec row for the peer-surface enricher and the FE.
            "tip_rec": True,
            "preview": False,
        }
        row.update(_group(circles, bool(tip.get("same_block"))))
        if phone_verified:
            row["actions"] = [
                peer_card_nudge_action(nickname=nickname, peer_user_id=peer_id)
            ]
        rows.append(row)
    # Shared-circle rows first, then same-block, then the rest — within each, best match.
    # The list arrives in the order the C-FIND-V2 headings render, so the surface groups by
    # walking it rather than sorting rows it was handed unordered.
    _RANK = {"circle": 0, "block": 1, "nearby": 2}
    rows.sort(key=lambda r: (_RANK.get(str(r.get("group_kind")), 3), -_strength(r)))
    return rows


def rerank_by_weights(
    rows: list[dict[str, Any]],
    weights: list[str],
) -> list[dict[str, Any]]:
    """Re-order rows by the threads the user said matter most.

    A weight counts when it appears in the row's own words — its trait tags or the text of
    the rec. Match strength stays the tiebreak, so within an equal number of satisfied
    weights the better-matching rec still leads.
    """
    wanted = [str(w or "").strip().lower() for w in (weights or []) if str(w or "").strip()]
    if not wanted or not rows:
        return list(rows)

    def hits(row: dict[str, Any]) -> int:
        haystack = " ".join(
            [str(t).lower() for t in row.get("trait_tags") or []]
            + [str(row.get("tip_text") or "").lower()]
        )
        return sum(1 for w in wanted if w in haystack)

    ranked = sorted(rows, key=lambda r: (-hits(r), -_strength(r)))
    for row in ranked:
        row["weight_hits"] = hits(row)
    return ranked


def tip_discovery_surface(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The counts strip for a cascade turn.

    Deliberately not build_discovery_surface(): that one counts claim-affinity bands, and
    none of these rows has one. Here "strong" means the row carries a real neighbor rec,
    which for this frame is the only tier that exists — everything on screen is either a
    pre-qualified answer or not shown at all.
    """
    with_rec = [r for r in rows if str(r.get("tip_text") or "").strip()]
    if not with_rec:
        return None
    n = len(with_rec)
    return {
        "strong_count": n,
        "partial_count": 0,
        "weak_count": 0,
        "status_label": f"{n} neighbor rec{'s' if n != 1 else ''}",
        "weak_peer": None,
        "ranked_summary": " · ".join(
            str(r.get("nickname") or "Neighbor")[:16] for r in with_rec[:PAGE_SIZE]
        )
        or None,
    }


def stamp_tip_peer_surface(
    ctx: dict[str, Any],
    tips: list[dict[str, Any]],
    *,
    phone_verified: bool = True,
    weights: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Put the ranked rec rows (and their counts strip) on ctx. Returns the rows shown."""
    rows = peer_rows_from_neighbor_tips(tips, phone_verified=phone_verified)
    if weights:
        rows = rerank_by_weights(rows, weights)
    shown = rows[:PAGE_SIZE]
    if not shown:
        return []
    ctx["peer_matches"] = shown
    surface = tip_discovery_surface(shown)
    if surface:
        ctx["discovery_surface"] = surface
    return shown
