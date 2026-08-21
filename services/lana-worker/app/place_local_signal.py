"""Stamp Google Places rows with the community we already have for that spot.

A tip_seek nobody has vouched for falls back to Google, and every row read as a
stranger's listing even when neighbors belong to that exact place. `places.google_place_id`
is unique, so matching a whole result page is one lookup — no search, no embeddings.

STRUCTURED ONLY, no prose: this returns the count, whether the caller is one of them, and
what members do there. The sentence is the surface's to write (and to translate) — a badge
composed here would be English text baked into an API payload.

Weaker proof than a neighbor's posted tip and must never outrank one: nobody recommended
the place, they just go there. Best-effort throughout — a recommendation turn must not
fail because the enrichment did.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# One page of Google suggestions is 3 rows; the cap is a guard against a caller
# handing us an unbounded list, not a real limit.
_MAX_LOOKUP = 20


def local_signal_for(
    google_place_ids: list[str], *, user_id: str | None
) -> dict[str, dict[str, Any]]:
    """google_place_id -> {member_count, is_member, activity_labels}. Missing = no community."""
    ids = [str(p).strip() for p in (google_place_ids or []) if str(p or "").strip()]
    if not ids or not user_id:
        return {}
    try:
        res = service_client().rpc(
            "local_signal_for_places",
            {"p_user_id": user_id, "p_google_place_ids": ids[:_MAX_LOOKUP]},
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("local_signal_lookup_failed n=%d", len(ids))
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        gid = str(r.get("google_place_id") or "").strip()
        if not gid:
            continue
        labels = r.get("activity_labels")
        out[gid] = {
            "place_id": str(r.get("place_id") or "") or None,
            "member_count": int(r.get("member_count") or 0),
            "is_member": bool(r.get("is_member")),
            "activity_labels": [
                str(x).strip() for x in labels if str(x or "").strip()
            ][:4] if isinstance(labels, list) else [],
        }
    return out


def _min_similarity() -> float:
    """0.50, not the 0.55 the peer matcher uses — measured, not guessed.

    text-embedding-005 on prod's own rows (2026-08-21), ask vs activity claim:

        pool hall            -> Snooker                 0.644   want
        place to play pool   -> Snooker                 0.609   want
        sushi place          -> Sushi Making Classes    0.699   want
        place to read w/kids -> Weekly reading session  0.530   want, 0.55 rejected it
        pool hall            -> Laser tag               0.479   reject
        pool hall            -> Sushi Making Classes    0.409   reject

    A short activity label against a conversational ask scores lower than the peer
    matcher's claim-to-claim comparison, so 0.55 dropped a real match. The nearest true
    negative is 0.479, which leaves 0.50 with room on both sides.
    """
    try:
        return float(os.environ.get("LANA_PLACE_ACTIVITY_MIN_SIM", "0.50"))
    except ValueError:
        return 0.50


def _radius_meters() -> float:
    """25 km, NOT the 8 km peer radius — a place you drive to is not a neighbor.

    The peer radius answers "who lives near me", and 8 km is right for that. A community
    is a PLACE, and the same 8 km silently excluded both real answers on prod (2026-08-21):

        Mizu Sushi & Steakhouse    4.5 km   inside — the only case that ever worked
        Florida Game Rooms        16.1 km   excluded
        Orlando Public Library    23.0 km   excluded

    Google was meanwhile being shown for the SAME asks at 16-20 km (search_places biases
    to 16 km and returns past it), so we were listing a stranger's pool hall 20 km away
    while refusing to mention our own community at 16. The member whose activity matched
    still lives on the caller's block; only the venue is a drive.
    """
    raw = os.environ.get("LANA_PLACE_RADIUS_METERS", "").strip()
    if not raw:
        return 25000.0
    try:
        return max(1000.0, min(float(raw), 200000.0))
    except ValueError:
        logger.warning("place_radius_bad_env value=%r — using default", raw)
        return 25000.0


def communities_for_request(
    request: str, *, user_id: str | None, limit: int = 2
) -> list[dict[str, Any]]:
    """Nearby communities whose members DO something close to the ask.

    The labeller above can only mark a place Google returned; asked for a reading spot
    Google answered with coffee shops, so the library neighbors actually read at was never
    in the list (prod 2026-08-19). This finds it — matched on the member's activity, since
    nothing in "Orlando Public Library" contains the word "read".

    Shaped like a Places suggestion so the caller can put these rows straight into the same
    list. Best-effort: [] on a missing embedding, a missing RPC, or any error.
    """
    ask = str(request or "").strip()
    if not ask or not user_id:
        return []
    try:
        from app.layer1_handlers import _embed_attr_filter
        from app.vec_util import to_pgvector

        literal = to_pgvector(_embed_attr_filter(ask))
        if not literal:
            logger.info("communities_for_request.skip reason=no_embedding ask=%r", ask[:60])
            return []
        res = service_client().rpc(
            "find_places_by_activity_semantic",
            {
                "p_user_id": user_id,
                "p_query_embedding": literal,
                "p_radius_meters": _radius_meters(),
                "p_min_similarity": _min_similarity(),
                "p_limit": max(1, min(int(limit or 2), 5)),
            },
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("communities_for_request_failed ask=%r", ask[:60])
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not str(r.get("name") or "").strip():
            continue
        labels = r.get("activity_labels")
        out.append(
            {
                "name": str(r["name"]).strip(),
                "address": str(r.get("address") or "").strip() or None,
                # The Google id, so the card's maps link and the dedupe below both work.
                "place_id": str(r.get("google_place_id") or "") or None,
                "community": {
                    "place_id": str(r.get("place_id") or "") or None,
                    "member_count": int(r.get("member_count") or 0),
                    "is_member": bool(r.get("is_member")),
                    "activity_labels": [
                        str(x).strip() for x in labels if str(x or "").strip()
                    ][:4] if isinstance(labels, list) else [],
                    # The activity that actually scored — the proof line, so the card can
                    # say WHY this place is here instead of asserting a bare match.
                    "matched_label": str(r.get("matched_label") or "").strip() or None,
                },
            }
        )
    logger.info("communities_for_request ask=%r found=%d", ask[:60], len(out))
    return out


def merge_communities_first(
    places: list[dict[str, Any]], communities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Community rows lead, Google fills the rest, nothing listed twice.

    Deduped on google_place_id: when Google returned the same spot, the community row wins
    because it carries the proof line the plain listing does not.
    """
    seen = {
        str(c.get("place_id") or "").strip()
        for c in communities
        if str(c.get("place_id") or "").strip()
    }
    rest = [
        p for p in places
        if isinstance(p, dict) and str(p.get("place_id") or "").strip() not in seen
    ]
    return [*communities, *rest]


def stamp_local_signal(places: list[dict[str, Any]], *, user_id: str | None) -> None:
    """Mark, in place, the Google rows that are also one of our communities."""
    if not isinstance(places, list) or not places:
        return
    by_gid = local_signal_for(
        [str(p.get("place_id") or "") for p in places if isinstance(p, dict)],
        user_id=user_id,
    )
    if not by_gid:
        return
    hits = 0
    for p in places:
        if not isinstance(p, dict):
            continue
        signal = by_gid.get(str(p.get("place_id") or "").strip())
        if not signal:
            continue
        p["community"] = signal
        hits += 1
    if hits:
        logger.info("local_signal_stamped %d of %d", hits, len(places))
