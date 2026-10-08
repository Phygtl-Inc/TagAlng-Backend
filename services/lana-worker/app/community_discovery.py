"""Find a community that already exists nearby, and join it.

The three pre-existing ways into a community all started from the user: they
mentioned a place in conversation (`chat_extraction`, closed by Lana's "which
spot is it?" ask), they added one in the Communities panel (`profile_add`), or
they redeemed an invite and self-confirmed their own place (`invite_confirmed`).
None of them let a user SEE a community that already exists and join it. This
module is that path.

PROVENANCE (the product ask). Two facts, because one column cannot answer both
questions:

  * `source`        — how the community first ENTERED the system. Never
                      overwritten, so "they mentioned it in chat" stays true
                      forever.
  * `confirmed_via` — the action that turned a candidate into a REAL (confirmed +
                      grounded) community.

They diverge in exactly the interesting case: a place captured from something the
user said, parked as a candidate, and later confirmed by tapping Join on the
discovery panel — `source='chat_extraction'`, `confirmed_via='community_join'`.
A fresh join is `source='community_join'` on both counts.

DISCLOSURE (§F). Discovery returns a place, a member count and how well the caller fits
it (app/community_affinity.py) — never who is there. The people panel stays members-only
(`app/community_surface.py`), so joining is what earns you the names, and the SQL
counts only members the caller may be counted alongside (a place kept alive
solely by a blocked user is not returned at all).

JOINING IS A SELF-CLAIM, NOT A REQUEST. These are real-world places, so "I go
here too" is a statement about yourself: it takes effect immediately, with no
approval and nobody to notify. Leaving is the existing soft-delete
(`/lana/circles/remove`), which drops the row from matching at once.
"""

from __future__ import annotations

import logging
import os
import re
import json
import threading
from collections import Counter
from typing import Any

from app.auth import service_client
from app.circles_capture import CIRCLE_TYPES, _slugify
from app.circles_flow import place_relation_emoji, place_relation_noun

logger = logging.getLogger(__name__)

# Same coarse radius as radius peer-matching: wide enough that the adjacent block
# is in, narrow enough that the next town is not. Overridable per deploy.
_DEFAULT_RADIUS_M = 8000.0
_MAX_LIMIT = 40

# What made the row real. Mirrors the DB check constraint (migration 20261004).
CONFIRMED_VIA_GROUNDING = "grounding_ask"
CONFIRMED_VIA_PROFILE = "profile_add"
CONFIRMED_VIA_INVITE = "invite_self_confirm"
CONFIRMED_VIA_JOIN = "community_join"

# How each row is described back to the user, in one honest phrase. The product
# question "did they join it in Lana, or did we add it after they mentioned it?"
# is answered by this pair, not by guesswork over timestamps.
JOINED_VIA_LABELS = {
    CONFIRMED_VIA_JOIN: "Joined in Lana",
    CONFIRMED_VIA_GROUNDING: "From something you told Lana",
    CONFIRMED_VIA_PROFILE: "You added it",
    CONFIRMED_VIA_INVITE: "From an invite",
}


def radius_meters() -> float:
    raw = os.environ.get("LANA_COMMUNITY_DISCOVERY_RADIUS_METERS", "").strip()
    if not raw:
        return _DEFAULT_RADIUS_M
    try:
        return max(100.0, min(float(raw), 200000.0))
    except ValueError:
        logger.warning("community_discovery_bad_radius value=%r — using default", raw)
        return _DEFAULT_RADIUS_M


def joined_via_label(confirmed_via: str | None, source: str | None) -> str | None:
    """One phrase for how this community came to be theirs.

    `confirmed_via` is the authority (it records the closing action). A row from
    before that column existed falls back to its `source`, which for those rows
    implies the same thing — the grounding ask was the only path that could have
    confirmed a chat-captured community.
    """
    via = str(confirmed_via or "").strip()
    if via in JOINED_VIA_LABELS:
        return JOINED_VIA_LABELS[via]
    legacy = {
        "profile_add": JOINED_VIA_LABELS[CONFIRMED_VIA_PROFILE],
        "invite_confirmed": JOINED_VIA_LABELS[CONFIRMED_VIA_INVITE],
        "chat_extraction": JOINED_VIA_LABELS[CONFIRMED_VIA_GROUNDING],
        "community_join": JOINED_VIA_LABELS[CONFIRMED_VIA_JOIN],
    }
    return legacy.get(str(source or "").strip())


# ── discover ──────────────────────────────────────────────────────────────────


def _coord(value: Any, limit: float) -> float | None:
    """A usable coordinate, or None. `limit` is 90 for a latitude, 180 for a longitude.

    Anything that is not a finite number inside the globe is None rather than a raise:
    on the way out it means the client cannot draw this row (it drops it, exactly as it
    drops an address the geocoder could not match), and on the way in it means the
    search falls back to her home point. Neither is worth a 500."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")) or abs(out) > limit:
        return None
    return out


def discover_communities(
    user_id: str,
    *,
    limit: int = 20,
    query: str | None = None,
    radius_m: float | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> list[dict[str, Any]]:
    """Communities near a point with at least one visible member.

    The point is `lat`/`lng` when a complete, in-range pair is given — /map sends its
    own camera centre, so somebody looking at Orlando gets Orlando's communities rather
    than her own street's. Absent or half-given, it falls back to her home/ZIP centroid,
    which is what every caller before the map relied on.

    Ordered by how alive the place is (members), then how close to that point. Returns
    [] on any error — a discovery panel that fails must read as "nothing yet", never as a
    stack trace. `is_member` marks the caller's own places rather than hiding
    them, so the panel can say "you're in this one".
    """
    if not user_id:
        return []
    args: dict[str, Any] = {
        "p_user_id": user_id,
        "p_radius_meters": float(radius_m if radius_m else radius_meters()),
        "p_limit": max(1, min(int(limit or 20), _MAX_LIMIT)),
        # p_locale is left at its default: it only renders the RPC's
        # distance_text, which no longer reaches the wire (see the shaper below).
        "p_query": (str(query).strip() or None) if query else None,
    }
    origin_lat, origin_lng = _coord(lat, 90.0), _coord(lng, 180.0)
    if origin_lat is not None and origin_lng is not None:
        # Sent ONLY when there is a real origin, and deliberately so. PostgREST resolves
        # an RPC by its argument names: naming p_lat/p_lng against a database that has not
        # taken 20261224120000 yet is a 404 (PGRST202), not an ignored extra. Withholding
        # them keeps every origin-less caller — the chat card, the named-community
        # resolver — working across that window, and narrows the blast radius to the one
        # read that actually needs the new function.
        args["p_lat"], args["p_lng"] = origin_lat, origin_lng
    try:
        res = service_client().rpc("discover_communities_near", args).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("discover_communities_failed user=%s", user_id)
        return []
    out = _near_rows(rows)
    attach_chapter_parents(out)
    return out


def _near_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """discover_communities_near-shaped RPC rows -> CommunityDiscoveryRow dicts.

    Shared with the chapters read (`community_chapters`), whose RPC returns the same
    columns, so a chapter row and a discovery row cannot drift apart on the wire."""
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("place_id"):
            continue
        name = str(r.get("name") or "").strip()
        if not is_joinable_place_name(name):
            # An address or a bare ZIP that got grounded as somebody's "community".
            # Their own row keeps working; it is just never offered to anyone else.
            continue
        types = r.get("member_types")
        primary = str(r.get("place_type") or "").strip() or _first_type(types)
        out.append(
            {
                "place_id": str(r["place_id"]),
                "place_name": str(r.get("name") or "").strip() or None,
                "place_address": str(r.get("address") or "").strip() or None,
                "place_type": primary or None,
                # What members call it, in the app's own vocabulary — never "circle".
                # Derived from the PLACE's type, not from a circle: this lists
                # communities the viewer is NOT in, so there is no row of theirs to
                # take a noun/emoji from.
                "relation": place_relation_noun(primary),
                "emoji": place_relation_emoji(primary),
                "zip": str(r.get("zip") or "").strip() or None,
                # The place's own point, straight off the row the distance was measured
                # from — so a pin and the distance beside it can never disagree. Null for
                # a place we hold no coordinates for (an imported row, a creator
                # community); the client drops those, exactly as it drops an address the
                # geocoder could not match. This is strictly less than `place_address`
                # above already discloses.
                "lat": _coord(r.get("lat"), 90.0),
                "lng": _coord(r.get("lng"), 180.0),
                "member_count": int(r.get("member_count") or 0),
                # No distance on the wire. The SQL still measures it — it is what
                # decides which places are inside the radius and how ties break — but
                # the only origin the worker has is a coarse home/ZIP centroid, so a
                # rendered "1.4 mi away" is wrong for anyone who is not standing at
                # home. Distance is the client's to compute, from where it actually is.
                "is_member": bool(r.get("is_member")),
                "status_line": _discovery_status_line(
                    int(r.get("member_count") or 0),
                    bool(r.get("is_member")),
                ),
            }
        )
    return out


# ── chapters — found from their parent ────────────────────────────────────────

_CHAPTERS_MAX = 40


def community_chapters(user_id: str, place_id: str, *, limit: int = _CHAPTERS_MAX) -> dict[str, Any]:
    """"Chapters in Iron Man Training" (POST /lana/circles/chapters, backend-asks §59(a)).

    Discovery drops every chapter on purpose — a chapter is found from its parent — so
    this is the read that does the finding. Same row as /lana/circles/discover, the
    caller's own chapters included with `is_member: true`.

    Visibility is the SQL's (discover_community_chapters, 20270112120000), implementing
    the 20261214120000 contract for a directory row: a member of the parent sees every
    chapter, a member of only some chapter sees only her own (never a sibling), and a
    stranger to the family sees the directory any neighbour gets from discovery.

    Raises ValueError('place_not_found') for an unknown place. "No chapters" is never an
    error — it is `chapters: []`, and so is a failed read (a panel that cannot load reads
    as "none yet", never as a 500)."""
    pid = str(place_id or "").strip()
    if not user_id or not pid:
        raise ValueError("place_required")
    try:
        res = (
            service_client()
            .table("places")
            .select("id, name")
            .eq("id", pid)
            .limit(1)
            .execute()
        )
        parent = (res.data or [None])[0]
    except Exception:
        # A malformed uuid lands here too (PostgREST 400s it) — same answer as unknown.
        logger.exception("community_chapters_parent_read_failed place=%s", pid)
        parent = None
    if not isinstance(parent, dict) or not parent.get("id"):
        raise ValueError("place_not_found")
    rows: list[Any] = []
    try:
        got = service_client().rpc(
            "discover_community_chapters",
            {
                "p_user_id": user_id,
                "p_place_id": pid,
                "p_limit": max(1, min(int(limit or _CHAPTERS_MAX), _CHAPTERS_MAX)),
            },
        ).execute()
        rows = got.data if isinstance(got.data, list) else []
    except Exception:
        logger.exception("community_chapters_failed user=%s place=%s", user_id, pid)
        rows = []
    return {
        "place_id": str(parent["id"]),
        "place_name": str(parent.get("name") or "").strip() or None,
        "chapters": _near_rows(rows),
    }


# ── description + area on a discovery row (§58(b)) ────────────────────────────


def attach_description_and_area(rows: list[dict[str, Any]]) -> None:
    """Set `description` and `area_label` on each row, in place, from two reads total.

    `description` is the profile's one-liner as STORED on the place (places.blurb) — the
    creator's own words, or the line authored from the place's real facts. A list never
    authors one: the profile is where that model call is scheduled, and a row with no
    stored line says nothing rather than a template. Null when nothing is on file.

    `area_label` is the place's area as a person names it ("Lake Nona"): the ZIP's
    named area for a place that is somewhere, and for a creator community — which is
    not anywhere, by constraint — the city it is RUN FROM (hq_city). A label only; it is
    never used to decide what is near anyone (20261214120000).

    Best-effort: a failed read leaves both null and the panel still lists the rows."""
    for row in rows:
        row.setdefault("description", None)
        row.setdefault("area_label", None)
    ids = list(dict.fromkeys(str(r.get("place_id") or "") for r in rows if r.get("place_id")))
    if not ids:
        return
    places: dict[str, dict[str, Any]] = {}
    try:
        res = (
            service_client()
            .table("places")
            .select("id, blurb, zip, place_type, hq_city")
            .in_("id", ids)
            .execute()
        )
        places = {str(p["id"]): p for p in (res.data or []) if isinstance(p, dict) and p.get("id")}
    except Exception:
        logger.exception("discovery_description_read_failed places=%s", len(ids))
        return
    zips = sorted(
        {
            str(p.get("zip") or "").strip()[:5]
            for p in places.values()
            if str(p.get("zip") or "").strip()[:5].isdigit()
            and len(str(p.get("zip") or "").strip()[:5]) == 5
        }
    )
    areas: dict[str, str] = {}
    if zips:
        try:
            zres = (
                service_client()
                .table("zip_centroids")
                .select("zip5, city")
                .in_("zip5", zips)
                .execute()
            )
            areas = {
                str(z.get("zip5")): str(z.get("city") or "").strip()
                for z in (zres.data or [])
                if isinstance(z, dict) and str(z.get("city") or "").strip()
            }
        except Exception:
            logger.exception("discovery_area_read_failed zips=%s", len(zips))
    for row in rows:
        place = places.get(str(row.get("place_id") or ""))
        if not place:
            continue
        row["description"] = str(place.get("blurb") or "").strip() or None
        hq = str(place.get("hq_city") or "").strip() or None
        zip5 = str(place.get("zip") or "").strip()[:5]
        if place.get("place_type") == "creator":
            row["area_label"] = hq
        else:
            row["area_label"] = areas.get(zip5) or hq


# ── discover by topic — the only path a creator community has ─────────────────

# Mirrors find_places_by_activity_semantic's default, and the RPC's own. A cosine under
# this is noise, and a topic community matched on noise is worse than an empty list.
_TOPIC_MIN_SIMILARITY = 0.55
_TOPIC_MAX_LIMIT = 20


def _similarity(value: Any) -> float | None:
    """The RPC's cosine, clamped to 0-1, or None when it is not a number.

    Not `_coord`: that one guards a globe, this one guards a score, and a helper that
    answers both questions is a helper that will one day accept a latitude as a match
    strength."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return max(0.0, min(out, 1.0))


def discover_communities_by_topic(
    user_id: str,
    query: str,
    *,
    limit: int = 5,
    creator_only: bool = True,
) -> list[dict[str, Any]]:
    """Communities whose MEMBERS describe themselves like the ask.

    The twin of `discover_communities`, and deliberately NOT a section of it. "Near me" is
    a claim about geography and stays false for a creator community forever — it has no
    lat/lng by constraint, so it is excluded from discover_communities_near by
    construction. "Like me" is a claim about content, and it is the only way anybody inside
    the app can find "Iron Man Training" at all. A caller that wants both asks for both and
    labels each section honestly (20261215120000).

    The match runs on the members' own public self-claims, because nothing in the words
    "Iron Man Training" is about triathlon and everything about the people in it is.

    `hq_city`/`hq_lat`/`hq_lng` come back for RENDERING — a label and a pin at the city the
    community is run FROM. Never a distance: there is no distance in this read and no
    ordering by one. Returns [] on anything at all going wrong.
    """
    ask = str(query or "").strip()
    if not user_id or not ask:
        return []
    try:
        from app.layer1_handlers import _embed_attr_filter
        from app.vec_util import to_pgvector

        literal = to_pgvector(_embed_attr_filter(ask))
        if not literal:
            logger.info("discover_by_topic.skip reason=no_embedding ask=%r", ask[:60])
            return []
        res = service_client().rpc(
            "discover_communities_semantic",
            {
                "p_user_id": user_id,
                "p_query_embedding": literal,
                "p_min_similarity": _TOPIC_MIN_SIMILARITY,
                "p_limit": max(1, min(int(limit or 5), _TOPIC_MAX_LIMIT)),
                "p_creator_only": bool(creator_only),
            },
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("discover_by_topic_failed user=%s ask=%r", user_id, ask[:60])
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("place_id"):
            continue
        name = str(r.get("name") or "").strip()
        if not is_joinable_place_name(name):
            continue
        ptype = str(r.get("place_type") or "").strip() or None
        members = int(r.get("member_count") or 0)
        out.append(
            {
                "place_id": str(r["place_id"]),
                "place_name": name,
                "place_type": ptype,
                "relation": place_relation_noun(ptype),
                "emoji": place_relation_emoji(ptype),
                # Run FROM here. The client draws it as such — a creator community is not
                # anywhere, so this is a provenance label with a pin, not a location.
                "hq_city": str(r.get("hq_city") or "").strip() or None,
                "hq_lat": _coord(r.get("hq_lat"), 90.0),
                "hq_lng": _coord(r.get("hq_lng"), 180.0),
                "member_count": members,
                "is_member": bool(r.get("is_member")),
                "status_line": _discovery_status_line(members, bool(r.get("is_member"))),
                # The member self-claim that matched, which is the card's proof line: it
                # says WHY this community answered the ask, in a member's own words. The
                # RPC filters to public, self-subject claims, so this is never somebody's
                # child and never a mutual-only claim shown to a stranger.
                "matched_label": str(r.get("matched_label") or "").strip() or None,
                # How close the matched claim was, 0-1. Kept on the row because the
                # ordering is by it: a caller that wants to cut a weak tail can, and a
                # caller that wants to say "a strong match" has the number to stand on.
                "similarity": _similarity(r.get("similarity")),
            }
        )
    return out


# The candidate pool the query-less read ranks by fit. Matches community_affinity's own
# cap on one scoring read, so every candidate is scored rather than a tail left at None.
_CREATOR_POOL = 40


def discover_creator_communities_for(user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Creator communities ranked by how well THIS caller fits them — no query needed
    (POST /lana/circles/discover-topic without `query`, backend-asks §58(a)).

    The "Digital community" tab has nothing to type into, so the topic read (which
    embeds an ask) cannot fill it. This reads every creator community with members
    (discover_creator_communities, 20270112120000), scores the pool with the same
    `affinity` /discover uses, and keeps the best `limit`: highest affinity first, an
    unscored row (None — the scoring read failed) after every scored one, then the
    livelier community, then the name, so the order is stable.

    Not filtered to the caller's city: hq_city is a label and never a predicate
    (20261214120000), and a creator community is not anywhere. Rows carry
    `affinity`, `fit_line`/`fit_chips` are added by the route after the cut so only the
    rows shown are authored. [] on anything going wrong."""
    if not user_id:
        return []
    try:
        res = service_client().rpc(
            "discover_creator_communities",
            {"p_user_id": user_id, "p_limit": _CREATOR_POOL},
        ).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("discover_creator_communities_failed user=%s", user_id)
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("place_id"):
            continue
        name = str(r.get("name") or "").strip()
        if not is_joinable_place_name(name):
            continue
        ptype = str(r.get("place_type") or "").strip() or None
        members = int(r.get("member_count") or 0)
        out.append(
            {
                "place_id": str(r["place_id"]),
                "place_name": name,
                "place_type": ptype,
                "relation": place_relation_noun(ptype),
                "emoji": place_relation_emoji(ptype),
                "hq_city": str(r.get("hq_city") or "").strip() or None,
                "hq_lat": _coord(r.get("hq_lat"), 90.0),
                "hq_lng": _coord(r.get("hq_lng"), 180.0),
                "member_count": members,
                "is_member": bool(r.get("is_member")),
                "status_line": _discovery_status_line(members, bool(r.get("is_member"))),
                # Nothing was asked, so nothing matched: no proof line and no cosine.
                "matched_label": None,
                "similarity": None,
            }
        )
    from app.community_affinity import attach_affinity

    attach_affinity(user_id, out)
    out.sort(
        key=lambda r: (
            r.get("affinity") is None,
            -(r.get("affinity") or 0.0),
            -int(r.get("member_count") or 0),
            str(r.get("place_name") or "").lower(),
        )
    )
    return out[: max(1, min(int(limit or 5), _TOPIC_MAX_LIMIT))]


def discover_communities_anywhere(
    user_id: str,
    query: str,
    *,
    placeless_only: bool = True,
    limit: int = 5,
    by_meaning: bool = False,
    radius_m: float | None = None,
) -> list[dict[str, Any]]:
    """Communities found by what they ARE, not where (20270109120000).

    The path a community with no location has: a podcasters group made in chat has no
    lat/lng, so the radius read can never return it. This matches what the community says
    about ITSELF — name, description, first action (English stems: "podcasters" finds
    "Podcast Club"). Never its members' claims: one member's "podcaster" made a gym a
    podcast community on prod (20270110120000).

    `placeless_only=False` is for a NAMED lookup — "SJSU" asked from Orlando should find
    San Jose State. A topic browse keeps the default, so a gym three states away never
    answers "any communities for climbers?".

    `by_meaning` also matches the ask's embedding against each community's own
    (blurb_embedding, 20270119120000): "a club about AI ethics" finds the Responsible
    Computing Club, which shares no word with it. `radius_m` lets a LOCATED community answer
    a topic ask too — on its name or meaning, never on one shared stem — and tags each row
    `nearby` (inside the radius) or `far` (outside it, or no origin to measure from) so the
    caller can say which. Chapters come back labelled with their parent (`parent`).

    Rows share the near read's card shape, with no distance on the wire and the HQ
    folded into the status line as provenance ("Run from Orlando"). [] on any failure.
    """
    ask = str(query or "").strip()
    if not user_id or not ask:
        return []
    args: dict[str, Any] = {
        "p_user_id": user_id,
        "p_query": ask[:120],
        "p_placeless_only": bool(placeless_only),
        "p_limit": max(1, min(int(limit or 5), _TOPIC_MAX_LIMIT)),
    }
    extra: dict[str, Any] = {}
    if by_meaning:
        from app.community_embeddings import kick

        # Whatever was written since the last search (any path, any writer) gets its vector.
        kick()
        try:
            from app.vec_util import to_pgvector
            from app.vertex_extract import vertex_embed

            literal = to_pgvector(vertex_embed(ask[:500]))
        except Exception:  # noqa: BLE001 — the name and stem arms still answer
            logger.exception("discover_anywhere_embed_failed ask=%r", ask[:60])
            literal = None
        if literal:
            extra["p_query_embedding"] = literal
            extra["p_min_similarity"] = _MEANING_MIN_SIMILARITY
    if radius_m:
        extra["p_radius_meters"] = float(radius_m)
    try:
        try:
            res = service_client().rpc(
                "discover_communities_anywhere", {**args, **extra}
            ).execute()
        except Exception:
            if not extra:
                raise
            # A database that has not taken 20270119120000 does not know the new arguments
            # (PGRST202). The old read still answers by name and stem.
            logger.warning("discover_anywhere_new_args_rejected; retrying without them")
            res = service_client().rpc("discover_communities_anywhere", args).execute()
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("discover_anywhere_failed user=%s ask=%r", user_id, ask[:60])
        return []
    parents = _parents_of(rows)
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("place_id"):
            continue
        name = str(r.get("name") or "").strip()
        if not is_joinable_place_name(name):
            continue
        ptype = str(r.get("place_type") or "").strip() or None
        members = int(r.get("member_count") or 0)
        mine = bool(r.get("is_member"))
        hq = str(r.get("hq_city") or "").strip() or None
        status = _discovery_status_line(members, mine)
        parent = parents.get(str(r.get("parent_place_ref") or ""))
        out.append(
            {
                "place_id": str(r["place_id"]),
                "place_name": name,
                "place_address": None,
                "place_type": ptype,
                "relation": place_relation_noun(ptype),
                "emoji": place_relation_emoji(ptype),
                "zip": None,
                # No point: it is not anywhere. The HQ is provenance, not a location.
                "lat": None,
                "lng": None,
                "hq_city": hq,
                "member_count": members,
                "is_member": mine,
                "status_line": f"Run from {hq} · {status}" if hq else status,
                "matched_on": str(r.get("matched_on") or "") or None,
                # Only set when the caller passed a radius: where this one sits relative to
                # them. Distance itself stays off the wire (see _near_rows).
                "reach": _reach(r, radius_m),
                "area": str(r.get("area") or "").strip() or None,
                "parent": parent,
            }
        )
    return out


# A community's own description vs the ask. Measured on prod-shaped rows (2026-10-07):
# the right community scores 0.61-0.76, unrelated ones 0.24-0.43.
_MEANING_MIN_SIMILARITY = float(os.environ.get("LANA_COMMUNITY_MEANING_MIN_SIM", "0.50"))


def _reach(row: dict[str, Any], radius_m: float | None) -> str | None:
    """'placeless' | 'nearby' | 'far' for a radius read; None when no radius was asked.

    A located community with no distance (the caller has no origin) is 'far': it cannot be
    called near anybody, and the reply names its city instead."""
    if not radius_m:
        return None
    if row.get("is_placeless"):
        return "placeless"
    meters = row.get("distance_meters")
    try:
        return "nearby" if meters is not None and float(meters) <= float(radius_m) else "far"
    except (TypeError, ValueError):
        return "far"


def _parents_of(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Parent heads for any chapter among RPC rows — {} when none is a chapter."""
    refs = [
        str(r.get("parent_place_ref"))
        for r in rows
        if isinstance(r, dict) and r.get("parent_place_ref")
    ]
    if not refs:
        return {}
    from app.community_surface import parent_heads

    return parent_heads(refs)


def attach_chapter_parents(rows: list[dict[str, Any]]) -> None:
    """Label chapters in a discovery list with their parent, in place.

    discover_communities_near lists chapters (20270119120000) without saying they are one;
    an unlabelled chapter reads as a standalone local community. One read for the refs,
    and parent_heads' two only when some row is a chapter."""
    ids = [str(r.get("place_id")) for r in rows if r.get("place_id")]
    if not ids:
        return
    try:
        res = (
            service_client()
            .table("places")
            .select("id, parent_place_ref")
            .in_("id", ids)
            .not_.is_("parent_place_ref", "null")
            .execute()
        )
        refs = {
            str(r["id"]): str(r["parent_place_ref"])
            for r in (res.data or [])
            if isinstance(r, dict) and r.get("parent_place_ref")
        }
    except Exception:  # noqa: BLE001 — a missing label, not a missing list
        logger.exception("attach_chapter_parents_failed rows=%s", len(ids))
        return
    if not refs:
        return
    from app.community_surface import parent_heads

    heads = parent_heads(list(refs.values()))
    for r in rows:
        ref = refs.get(str(r.get("place_id")))
        if ref and ref in heads:
            r["parent"] = heads[ref]


def _first_type(types: Any) -> str:
    if isinstance(types, list):
        for t in types:
            s = str(t or "").strip()
            if s in CIRCLE_TYPES:
                return s
    return ""


def _discovery_status_line(members: int, is_member: bool) -> str:
    """The one fact on the row the worker can state. `member_count` includes the caller
    when they are already in, so an "N people" line must not read as N strangers.

    Distance used to be the second half of this line and is gone with `distance_text` —
    the worker measures from a coarse home centroid, which is not where the reader is."""
    if is_member:
        return "You're in" if members <= 1 else f"You + {members - 1} others"
    return "1 person" if members == 1 else f"{members} people"


# ── join ──────────────────────────────────────────────────────────────────────


# Places that are not communities however they got grounded: a bare street address,
# an apartment/unit line, a ZIP typed as a name. The chat extractor parks whatever the
# user said and grounding pins whatever Google returned, so dev already holds rows like
# "10057 Selten Way #328" and a place literally named "32827". They are legitimate
# `places` rows (an event can be there) but offering one as a community to JOIN is
# nonsense, so discovery filters them out. Deliberately name-shape only — nothing here
# guesses at quality, and a real place with a number in its name ("FIT 407") survives.
_ADDRESSISH_RE = re.compile(
    r"""^(?:
          \#?\d+\s+\w+.*            # 373 Tampa Ct, 692 Olde Camelot Cir #3282
        | \d{5}(?:-\d{4})?          # a ZIP as a name
        )$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_joinable_place_name(name: str) -> bool:
    """False for address-shaped / ZIP-shaped names — see _ADDRESSISH_RE."""
    text = str(name or "").strip()
    if len(text) < 2:
        return False
    return not _ADDRESSISH_RE.match(text)


def _place_row(place_id: str) -> dict[str, Any]:
    # blurb first (the noun model needs it for creator communities); step down without it
    # rather than fail the join on an environment that predates the column.
    for fields in ("id, name, address, place_type, blurb", "id, name, address, place_type"):
        try:
            res = (
                service_client()
                .table("places")
                .select(fields)
                .eq("id", place_id)
                .limit(1)
                .execute()
            )
        except Exception:
            continue
        rows = res.data if isinstance(res.data, list) else []
        return rows[0] if rows else {}
    logger.exception("community_join_place_read_failed place=%s", place_id)
    return {}


def _existing_rows(user_id: str) -> list[dict[str, Any]]:
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id, circle_key, circle_type, place_ref, status, source, confirmed_via, noun, emoji")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(60)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_join_existing_read_failed user=%s", user_id)
        return []
    return [r for r in rows if isinstance(r, dict)]


_PLACE_NOUN_PROMPT = """You name what a place IS to the people who go there. The word is \
rendered as "your <noun>" on a neighbour's card ("your gym", "your church", "your sushi spot").

Output ONLY JSON: {"noun": "...", "emoji": "..."}

Rules:
- noun: 1-3 lowercase words, a common noun for the KIND of place. Never the place's own \
name, never a brand, never an address — "mizu sushi" is wrong, "sushi spot" is right.
- Pick the word a regular would use: a CrossFit box is "gym", a parish hall is "church", \
a branch library is "library", a taproom is "brewery".
- emoji: exactly one, matching the kind of place.
- category "creator" is NOT a place: it is an online community run by one person (an \
influencer, a coach, a writer) around a topic. Name it from its description as the kind of \
GROUP it is — "founder circle", "running crew", "etiquette club", "book club" — never a \
venue word (studio, spot, shop, café, gym unless it truly is one).
- English only."""


def _llm_place_noun(place: dict[str, Any]) -> dict[str, str]:
    """Ask the model what kind of place this is, once, when nobody has said.

    The taxonomy cannot answer it: place_type collapses to 'other' for anything
    outside _GOOGLE_TYPE_MAP (no food, drink or library types are in it), and the
    type word for 'other' is "spot" — which is how seven neighbours who joined one
    restaurant all read "You both: your spot" (prod, 2026-08-31).

    Deliberately NOT given the disclosure escape hatch: the prompt forbids the
    place's own name, so the answer stays a RELATION word and §F/O7 holds — the
    viewer still learns only what kind of place they both go to.
    """
    name = str(place.get("name") or "").strip()
    if not name:
        return {}
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return {}
        data = llm_json(
            model=router_model(),
            system=_PLACE_NOUN_PROMPT,
            user_payload=json.dumps(
                {
                    "name": name,
                    "address": str(place.get("address") or ""),
                    "category": str(place.get("place_type") or ""),
                    # Without it a creator community is just a name and the word "creator",
                    # and the model guessed "creative studio 🎨" for a founders' group.
                    "description": str(place.get("blurb") or "")[:300],
                }
            ),
            max_tokens=60,
            temperature=0.1,
        )
    except Exception:
        logger.exception("place_noun_llm_failed place=%s", place.get("id"))
        return {}
    from app.circles_capture import _clean_emoji, _clean_noun

    noun = _clean_noun((data or {}).get("noun"))
    # A model that echoed the venue back ("mizu sushi") is refused, not shipped:
    # that is the one answer the disclosure rule does not allow.
    if noun and noun in _slugify(name).replace("_", " "):
        noun = ""
    out: dict[str, str] = {}
    if noun:
        out["noun"] = noun
    emoji = _clean_emoji((data or {}).get("emoji"))
    if emoji:
        out["emoji"] = emoji
    return out


def _place_noun_emoji(place_id: str, *, place: dict[str, Any] | None = None) -> dict[str, str]:
    """The noun/emoji this community answers to — stored, else asked, else nothing.

    Only capture ever minted these; a Join tap never did, so joiners inherit rather
    than guess. Most-used wins so one odd row cannot rename a place. When NOBODY has
    one the model is asked once and the answer is written back to every blank row at
    the place, so the next reader (and every other surface — the roster, the profile,
    the chat cards) finds it stored. Empty dict on any failure: the caller falls back
    to the type word, exactly as before.
    """
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id, noun, emoji")
            .eq("place_ref", place_id)
            .is_("dismissed_at", "null")
            .limit(200)
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:
        logger.exception("community_place_noun_read_failed place=%s", place_id)
        return {}
    out: dict[str, str] = {}
    for field in ("noun", "emoji"):
        counts = Counter(
            str((r or {}).get(field) or "").strip()
            for r in rows
            if str((r or {}).get(field) or "").strip()
        )
        if counts:
            out[field] = counts.most_common(1)[0][0]
    if out.get("noun"):
        return out

    asked = _llm_place_noun(place if place is not None else _place_row(place_id))
    if not asked.get("noun"):
        # ponytail: no negative cache — a place the model refuses is re-asked on the
        # next read. Add one (places.features_jsonb) if that ever shows up in cost.
        return out
    out.update(asked)
    blanks = [
        str(r.get("id"))
        for r in rows
        if str(r.get("id") or "") and not str(r.get("noun") or "").strip()
    ]
    if blanks:
        try:
            # Fills BLANKS only — a member who told us what they call it keeps their word.
            service_client().table("circle_affiliations").update(
                {k: v for k, v in out.items() if v}
            ).in_("id", blanks).execute()
        except Exception:
            logger.exception("community_place_noun_backfill_failed place=%s", place_id)
    return out


def _unique_key(base: str, taken: set[str]) -> str:
    key = base or "spot"
    if key not in taken:
        return key
    for n in range(2, 20):
        candidate = f"{key}_{n}"[:64]
        if candidate not in taken:
            return candidate
    return f"{key}_x"[:64]


def join_community(
    user_id: str,
    place_id: str,
    *,
    circle_type: str | None = None,
    membership: str = "member",
) -> dict[str, Any]:
    """Join a community the user found in Lana.

    `membership` is the joiner's own answer to "do you actually go here?" (§19):
    'member' is membership as always; 'curious' parks the row as status='curious', which
    every member count, roster and matcher excludes — she gets the place in her own list
    and nobody gets her as a neighbour they have never met. Tapping Join again as a
    member promotes the same row.

    Three cases, in order:
      1. already a confirmed member  → no-op, `already_member: True`
      2. a candidate of theirs (a place they mentioned, or an ungrounded row with
         the same key) → CONFIRM that row and pin it here. `source` keeps its
         original value — the community really did come from what they said — and
         `confirmed_via` records that the Join tap is what closed it.
      3. otherwise → a fresh row, `source` and `confirmed_via` both
         `community_join`.

    Raises ValueError('place_not_found' | 'place_required').
    """
    if not place_id:
        raise ValueError("place_required")
    place = _place_row(place_id)
    if not place:
        raise ValueError("place_not_found")

    joined_status = "curious" if str(membership or "").strip().lower() == "curious" else "confirmed"

    sb = service_client()
    rows = _existing_rows(user_id)
    mine_here = next((r for r in rows if str(r.get("place_ref") or "") == place_id), None)
    if mine_here and str(mine_here.get("status") or "") == joined_status:
        return {
            "affiliation_id": str(mine_here["id"]),
            "place_id": place_id,
            "place_name": place.get("name"),
            "status": joined_status,
            "already_member": True,
            "source": mine_here.get("source"),
            "confirmed_via": mine_here.get("confirmed_via"),
        }

    resolved_type = (
        (circle_type or "").strip().lower()
        or str(place.get("place_type") or "").strip().lower()
        or "other"
    )
    if resolved_type not in CIRCLE_TYPES:
        resolved_type = "other"
    base_key = _slugify(str(place.get("name") or "")) or resolved_type

    # A candidate to promote: the row already pointing here, else an ungrounded row
    # the user parked under the same name (the "you mentioned a gym" case).
    candidate = mine_here or next(
        (
            r
            for r in rows
            if not r.get("place_ref") and str(r.get("circle_key") or "") == base_key
        ),
        None,
    )

    patch: dict[str, Any] = {
        "place_ref": place_id,
        "status": joined_status,
        "confidence": 1.0,  # a deliberate tap, not an inference
        "confirmed_via": CONFIRMED_VIA_JOIN,
    }
    # A join never asked an LLM for a noun, so every joiner's row went in with
    # noun NULL and place_relation_noun fell through to the TYPE word — which is
    # "spot" for a place_type of 'other'. Seven neighbours joined one restaurant
    # and every fellow card read "You both: your spot" (prod, 2026-08-31).
    # The community already knows what it is called: take the noun/emoji a member
    # who was asked at capture already stored. Blank when nobody has one — a wrong
    # noun is worse than the bucket word.
    inherited = _place_noun_emoji(place_id, place=place)
    if candidate:
        affiliation_id = str(candidate["id"])
        for field, value in inherited.items():
            if value and not str(candidate.get(field) or "").strip():
                patch[field] = value
        try:
            sb.table("circle_affiliations").update(patch).eq("id", affiliation_id).execute()
        except Exception:
            logger.exception("community_join_promote_failed aff=%s", affiliation_id)
            raise ValueError("join_failed") from None
        origin_source = str(candidate.get("source") or "community_join")
        promoted_from_candidate = True
    else:
        taken = {str(r.get("circle_key") or "") for r in rows}
        row = {
            "user_id": user_id,
            "circle_type": resolved_type,
            "circle_key": _unique_key(base_key, taken),
            "source": CONFIRMED_VIA_JOIN,
            **inherited,
            **patch,
        }
        try:
            res = sb.table("circle_affiliations").insert(row).execute()
            affiliation_id = str(res.data[0]["id"]) if res.data else ""
        except Exception:
            logger.exception("community_join_insert_failed user=%s place=%s", user_id, place_id)
            raise ValueError("join_failed") from None
        if not affiliation_id:
            raise ValueError("join_failed")
        origin_source = CONFIRMED_VIA_JOIN
        promoted_from_candidate = False

    # Housekeeping the grounding path already does: flush any feature notes parked
    # on the candidate, close its "which spot?" ask so the tile stops asking, and
    # open the one enrichment question ("what do you enjoy most at X?"). Skipped for a
    # curious join — she just said she does NOT go there, so asking what she enjoys
    # most about it would be Lana not listening.
    if joined_status == "confirmed":
        _after_join(user_id, affiliation_id, candidate, place_id, str(place.get("name") or ""))
        # The members already there hear about it — same roster the community's meets mail.
        notify_members_of_join(place_id, str(place.get("name") or ""), user_id)

    logger.info(
        "community_joined user=%s place=%s source=%s promoted=%s",
        user_id,
        place_id,
        origin_source,
        promoted_from_candidate,
    )
    return {
        "affiliation_id": affiliation_id,
        "place_id": place_id,
        "place_name": place.get("name"),
        "status": joined_status,
        "already_member": False,
        "source": origin_source,
        "confirmed_via": CONFIRMED_VIA_JOIN,
        "promoted_from_candidate": promoted_from_candidate,
    }


def set_membership(user_id: str, affiliation_id: str, membership: str) -> dict[str, Any]:
    """"I'm a member — I go here" / "Not yet — just curious" (§19), answered AFTER the
    join: the sheet is a separate step from the tap, so it posts the answer here.

    'member' → status='confirmed' (counted, named, matched). 'curious' → status='curious'
    (hers to see, excluded everywhere else). Idempotent.

    Raises ValueError('affiliation_not_found' | 'place_required').
    """
    status = "curious" if str(membership or "").strip().lower() == "curious" else "confirmed"
    sb = service_client()
    try:
        res = (
            sb.table("circle_affiliations")
            .select("id, place_ref, status")
            .eq("id", affiliation_id)
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("membership_lookup_failed aff=%s", affiliation_id)
        raise ValueError("affiliation_not_found") from None
    if not rows:
        raise ValueError("affiliation_not_found")
    row = rows[0]
    place_id = str(row.get("place_ref") or "")
    if not place_id:
        # An ungrounded candidate is not a community yet — nothing to be a member of.
        raise ValueError("place_required")
    if str(row.get("status") or "") != status:
        try:
            sb.table("circle_affiliations").update({"status": status}).eq(
                "id", affiliation_id
            ).execute()
        except Exception:
            logger.exception("membership_write_failed aff=%s", affiliation_id)
            raise ValueError("membership_write_failed") from None
        logger.info(
            "membership_set user=%s place=%s status=%s", user_id, place_id, status
        )
    return {
        "affiliation_id": affiliation_id,
        "place_id": place_id,
        "membership": "member" if status == "confirmed" else "curious",
    }


# ── the chat turn ─────────────────────────────────────────────────────────────

# Nearby communities named in one reply. More than this and the prose stops being
# readable; the cards carry the rest.
_MINE_NAMED_MAX = 6
_CHAT_NEARBY_MAX = 5

# Roster cards one reply can carry — the wire cap in main._peer_matches_from_ctx. Kept in
# step with it so the count the reply states is the count that ships.
_ROSTER_CARDS_MAX = 8


# Words that carry no place in them, so "the Mizu Sushi community" and "Mizu Sushi" name
# the same spot.
_NAME_NOISE = frozenset(
    {"the", "a", "an", "at", "in", "of", "my", "our", "community", "communities",
     "group", "groups", "place", "spot",
     # Connectors. "and" matters most because _name_tokens CREATES it out of "&":
     # "barnes and nobel" and "Mizu Sushi & Steakhouse" shared the word "and", so a
     # sushi restaurant was offered as a candidate for a bookstore, and the two
     # candidates then triggered a clarifier instead of the single-candidate answer
     # (QA 2026-08-21).
     "and", "or", "y", "e"}
)


def _name_tokens(name: str) -> list[str]:
    """Lowercase alphanumeric words, "&" read as "and".

    People do not type the ampersand: "mizu sushi and steakhouse" against a row named
    "Mizu Sushi & Steakhouse" failed raw containment, and the turn then said it could not
    find a community that the same reply went on to say they were already in
    (QA 2026-08-21)."""
    folded = name.casefold().replace("&", " and ")
    return "".join(ch if ch.isalnum() else " " for ch in folded).split()


def _same_place_name(said: str, row_name: str) -> bool:
    """Do these two name one place? Normalized containment, then a word-subset pass.

    No regex and no fuzzy library ([[no-new-regex-use-ai-signals]]) — the name itself came
    from the AI slot, and this only has to survive punctuation and filler."""
    a, b = _name_tokens(said), _name_tokens(row_name)
    if not a or not b:
        return False
    # Padded, so containment lands on WHOLE words: unpadded, "a" matched "and" and "fit"
    # matched "Fitness", and any short fragment claimed a specific place.
    ja, jb = f" {' '.join(a)} ", f" {' '.join(b)} "
    if ja in jb or jb in ja:
        return True
    # "Mizu Sushi Steakhouse" vs "Mizu Sushi & Steakhouse" — same words, and normalizing
    # left them in a different order than containment can see. Two meaningful words
    # minimum: a lone "fit" would otherwise claim every gym on the list.
    sa = {t for t in a if t not in _NAME_NOISE}
    sb = {t for t in b if t not in _NAME_NOISE}
    if len(sa) < 2 or not sb:
        return False
    return sa <= sb or sb <= sa


def _resolve_named_community(user_id: str, name: str) -> dict[str, Any] | None:
    """The community the user NAMED — hers first, then the ones near her."""
    if not str(name or "").strip():
        return None
    for pool in (
        _my_communities(user_id),
        discover_communities(user_id, limit=_MAX_LIMIT),
    ):
        for c in pool:
            if _same_place_name(name, str(c.get("place_name") or "")):
                return c
    return None


def find_named_community(user_id: str, said: str) -> dict[str, Any] | None:
    """The one community a free-text name means, or None — for a caller that needs a
    place, not a reply (attaching a chapter to "SJSU").

    The same chain the communities turn uses, minus the "did you mean?" branch: hers,
    then near her, then anywhere by name, then the AI alias matcher over those plus
    meaning candidates ("SJSU" → San Jose State University). Ambiguity is a None here;
    the caller asks, it does not guess."""
    return resolve_community_name(user_id, said)["hit"]


def resolve_community_name(user_id: str, said: str) -> dict[str, Any]:
    """What a free-text community name means, for every caller that has one — the
    communities turn, a chapter attach, and the events browse ("what's on at SJSU?").

    {"hit": row | None, "inexact": what they said when the hit is the one near-miss
    (name it back so a wrong guess is correctable) | None, "near": the candidates when
    it is a genuine "which one?" | []}. Every arm is a real read; `hit` is only ever a
    row that exists, and None with no `near` is an honest miss.

    The chain: hers, then near her, then anywhere by name and meaning, then one shared
    word, then a short form. A hit nobody near her or in hers carries `far=True`, so a
    reply never calls San Jose State "near you" from Orlando."""
    out: dict[str, Any] = {"hit": None, "inexact": None, "near": []}
    name = str(said or "").strip()[:80]
    if not user_id or not name:
        return out
    hit = _resolve_named_community(user_id, name)
    if hit:
        out["hit"] = hit
        return out
    # Not theirs and not near them: a community with no location, or a named place far
    # away ("San Jose State" from Orlando), is still a real answer (20270109120000).
    far = discover_communities_anywhere(
        user_id, name, placeless_only=False, limit=_CHAT_NEARBY_MAX, by_meaning=True
    )
    exact = next(
        (c for c in far if c.get("matched_on") == "name" and _same_place_name(name, c["place_name"])),
        None,
    )
    if exact:
        out["hit"] = dict(exact, far=True)
        return out
    mine = _my_communities(user_id)
    nearby = discover_communities(user_id, limit=_MAX_LIMIT)
    pools = [mine, nearby, far]
    near = _near_name_candidates(name, pools)
    if len(near) == 1:
        # Exactly one place shares a word with what they said, so asking "did you mean
        # X?" only stalls — a typo ("barnes and nobel") looped that question three times
        # without ever answering (QA 2026-08-21). Take it and NAME it.
        out["hit"], out["inexact"] = near[0], name
        return out
    if near:
        # Genuinely ambiguous: three Lake Nona gyms are a "which one".
        out["near"] = near
        return out
    # No shared word, but maybe a short form of one ("SJSU"). For someone who is not in it
    # and not near it, nothing above ever held San Jose State — the name and meaning reads
    # search the literal "SJSU" — so the matcher below had no candidate to recognise, and
    # the reply said "yours would be the first" (prod 2026-10-07). The model says what the
    # short form stands for; those full names are searched everywhere by name.
    spelled = _alias_expansion_rows(user_id, name)
    named = [c for c in spelled if c.get("_expanded_exact")]
    if len(named) > 1:
        out["near"] = named[:3]
        return out
    local = {str(c.get("place_id") or "") for c in mine + nearby}
    hit = named[0] if named else _ai_alias_match(name, pools + [spelled])
    if hit:
        hit = {k: v for k, v in hit.items() if k != "_expanded_exact"}
        if str(hit.get("place_id") or "") not in local:
            hit["far"] = True
        out["hit"] = hit
    return out


def _near_name_candidates(
    said: str, pools: list[list[dict[str, Any]]], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Communities that share a meaningful word with what they said.

    A near miss is not a miss. "Fitness CF" against three gyms, or a name typed a little
    wrong, is a question about WHICH one — answering "there is no community by that name"
    is wrong, and picking one for them is a guess."""
    want = {t for t in _name_tokens(said) if t not in _NAME_NOISE}
    if not want:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in pools:
        for c in pool:
            pid = str(c.get("place_id") or "")
            name = str(c.get("place_name") or "")
            if not pid or pid in seen or not name:
                continue
            if want & {t for t in _name_tokens(name) if t not in _NAME_NOISE}:
                seen.add(pid)
                out.append(c)
                if len(out) >= limit:
                    return out
    return out


_ALIAS_PROMPT = """You match what someone called a community to the real community they \
meant. People shorten names: initials ("BMCC" is Borough of Manhattan Community \
College), nicknames ("Mass General" is Massachusetts General Hospital), dropped words \
("Stanford" is Stanford University).

Output ONLY JSON: {"match": <index of the community they meant, or null>}

Rules:
- Pick an index ONLY when what they said is a common way of naming that exact place.
- Sharing a single generic word ("fitness", "church", "cafe") is NOT a match.
- If two could fit, or none clearly does, answer null. A wrong guess is worse than null."""


_EXPAND_PROMPT = """Someone named a community (a school, gym, church, club, hospital, \
company or other place people belong to) by a short form — initials, a nickname, a \
misspelling, a translation, or the name with words dropped. Say what full name(s) it \
commonly stands for, so they can be looked up.

Output ONLY JSON: {"names": [<full name>, ...]}

Rules:
- At most 3 names, most likely first, each the way the place itself would be written.
- Each name is a community of the kinds above — somewhere people belong to as students, \
members, congregants, patients or staff — never a sports team, brand, product or person. \
A leading "the" is part of the nickname, not noise.
- Only names it is a COMMON way of saying. Never invent one to fill the list.
- [] when it already is a full name, or you do not know what it stands for."""

_EXPAND_MAX = 3


def _alias_expansion_rows(user_id: str, said: str) -> list[dict[str, Any]]:
    """Communities anywhere whose name is what a short form stands for, by the model.

    The model spells the short form out ("SJSU" → "San Jose State University") and each
    spelling is looked up with the same anywhere-by-name read a full name takes — so the
    community does not have to be hers or near her to be found. Rows whose own name IS
    one of the spellings carry `_expanded_exact`; the rest are candidates for
    _ai_alias_match. [] when the model is unsure, unavailable, or nothing exists by
    those names: a miss stays an honest miss ([[no-new-regex-use-ai-signals]])."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return []
        data = llm_json(
            model=router_model(),
            system=_EXPAND_PROMPT,
            user_payload=json.dumps({"they_said": str(said)[:80]}),
            max_tokens=80,
            temperature=0.0,
        )
    except Exception:
        logger.exception("community_alias_expand_failed said=%r", said)
        return []
    names = (data or {}).get("names")
    if not isinstance(names, list):
        return []
    spelled = [
        str(n).strip()[:80] for n in names
        if isinstance(n, str) and str(n).strip()
        and not _same_place_name(said, str(n))  # the short form itself was already searched
    ][:_EXPAND_MAX]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for full in spelled:
        for c in discover_communities_anywhere(
            user_id, full, placeless_only=False, limit=_CHAT_NEARBY_MAX
        ):
            pid = str(c.get("place_id") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            exact = c.get("matched_on") == "name" and _same_place_name(full, c["place_name"])
            out.append(dict(c, _expanded_exact=True) if exact else c)
    logger.info("community_alias_expand said=%r spelled=%r rows=%d", said, spelled, len(out))
    return out


def _ai_alias_match(said: str, pools: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
    """The community an abbreviation or nickname refers to, read by the model.

    "SJSU" shares no word with "San Jose State University", so neither the name match nor
    the near-miss pass could see it, and the reply said there was no SJSU community while
    the same message listed San Jose State University as theirs (QA 2026-10-04). Names
    are judged by meaning here, never by a regex or an initials rule
    ([[no-new-regex-use-ai-signals]]). None whenever the model is unsure or unavailable,
    so a miss stays an honest miss."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in pools:
        for c in pool:
            pid = str(c.get("place_id") or "")
            if pid and pid not in seen and str(c.get("place_name") or "").strip():
                seen.add(pid)
                rows.append(c)
    rows = rows[:_MAX_LIMIT]
    if not rows or not str(said or "").strip():
        return None
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return None
        data = llm_json(
            model=router_model(),
            system=_ALIAS_PROMPT,
            user_payload=json.dumps(
                {
                    "they_said": str(said)[:80],
                    "communities": [
                        {
                            "index": i,
                            "name": str(c.get("place_name") or ""),
                            "address": str(c.get("place_address") or "")[:120],
                        }
                        for i, c in enumerate(rows)
                    ],
                }
            ),
            max_tokens=20,
            temperature=0.0,
        )
    except Exception:
        logger.exception("community_alias_llm_failed said=%r", said)
        return None
    idx = (data or {}).get("match")
    if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(rows):
        return None
    return rows[idx]


# What a "did you mean" chip should ask on their behalf, per side of the question. The
# chip re-asks THEIR question about the place they picked — hard-coding the roster ask
# rewrote "what type of community is this" into "who is in it" (QA 2026-08-21).
_CHIP_ASK = {
    "people": "who is in {place}",
    "about": "what kind of place is {place}",
    "manage": "I want to update my {place} community",
}


def _did_you_mean_turn(
    *,
    said: str,
    candidates: list[dict[str, Any]],
    message: str,
    session_ctx: dict[str, Any],
    ask: str = "about",
) -> str:
    """"Did you mean this one?" — the honest answer to a near miss, with the real names
    tap-able so the next turn resolves exactly instead of guessing again."""
    from app.reply_compose import compose_reply

    names = [str(c.get("place_name") or "").strip() for c in candidates]
    names = [n for n in names if n]
    # policy_chips is the generic one-turn CTA surface (app/ui_actions.derive_ui_actions):
    # label is what they tap, `send` is posted back verbatim, and the exact row name is
    # what makes the next turn's match exact.
    template = _CHIP_ASK.get(ask, _CHIP_ASK["about"])
    session_ctx["policy_chips"] = [
        {"label": n, "send": template.format(place=n)} for n in names[:3]
    ]
    return compose_reply(
        goal=(
            "They named a place you are not sure about. Say you want to check WHICH one "
            "they mean — never claim it does not exist, and never pick one for them. ONE "
            "short question; the names are tap-able buttons under your message, so do not "
            "spell out more than two of them."
        ),
        facts=[
            f'They said: "{said}"',
            "Closest real communities, which are the buttons under your message: "
            + ", ".join(names),
            "You do NOT know which of these they meant",
        ],
        fallback=(
            f"Did you mean {names[0]}?" if len(names) == 1
            else f"Which one did you mean — {names[0]} or {names[1]}?"
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


def _arm_join(
    session_ctx: dict[str, Any], place_id: str, place_name: str, *, add_chip: bool = True
) -> None:
    """Offer to add them, and make the offer TAP-ABLE.

    Arming the pending state alone left "want me to add you?" with nothing to press —
    the answer had to be typed, which is not what an offer looks like (QA 2026-08-21).
    "Join <name>" is the payload the join lane already reads (read_join_reply), and it is
    what the discovery cards' own Join button has always posted."""
    session_ctx["community_join_pending"] = {
        "places": [{"place_id": place_id, "place_name": place_name}]
    }
    session_ctx["policy_chips"] = (
        [{"label": "Add me", "send": f"Join {place_name}"}] if add_chip else []
    ) + [{"label": "Show me others", "send": "what communities are near me"}]


def _community_about_turn(
    user_id: str,
    *,
    community: dict[str, Any],
    message: str,
    session_ctx: dict[str, Any],
    inexact: str | None = None,
) -> str | None:
    """Anything about the PLACE itself — what kind it is, what it has, how big, what is on.

    The community screen renders all of this and chat could not reach any of it, so "what
    type of community is Barnes & Noble" was answered with a roster refusal and then a
    list of other communities (QA 2026-08-21). Reads the same profile the screen does, and
    that read opens for a visitor too ([[visitor-opens-community]]) — you do not have to
    join a bookstore to be told it is a bookstore.

    None when the place cannot be read, so the caller can fall through honestly."""
    from app.community_surface import community_profile
    from app.reply_compose import compose_reply

    pid = str(community.get("place_id") or "")
    try:
        prof = community_profile(user_id, place_id=pid, phone_verified=True)
    except Exception:  # noqa: BLE001 — ValueError('place_not_found') and any read failure
        logger.exception("community_about_turn_failed place=%s", pid)
        return None

    # An about-turn is not a people-turn: no faces, and no scored strip from earlier.
    # activity_previews starts cleared too — it is not turn-scoped, so a browse lane's
    # events would otherwise ride in under a question about a different place.
    session_ctx["peer_matches"] = None
    session_ctx["discovery_surface"] = None
    session_ctx["activity_previews"] = None

    place = str(prof.get("place_name") or "").strip()
    membership = str(prof.get("membership") or "visitor")
    count = int(prof.get("member_count") or 0)
    curious = int(prof.get("curious_count") or 0)
    features = [
        str((f or {}).get("label") or "").strip()
        for f in (prof.get("features") or [])
        if str((f or {}).get("label") or "").strip()
    ]
    events = [
        str((e or {}).get("title") or "").strip()
        for e in (prof.get("upcoming_events") or [])
        if str((e or {}).get("title") or "").strip()
    ]
    far = bool(community.get("far"))
    facts = [f"Community: {place} — it exists on Lana"]
    if far:
        # Found by name, not by distance: it is nowhere near them, so "near you" — the
        # fallback's word for every other hit — would be false (prod 2026-10-06, SJSU
        # asked about from Rawalpindi).
        facts.append(
            "It is NOT near them — they found it by name; never say it is near or local. "
            "Anyone can join it from anywhere"
        )
    # Chapters, both directions: "tell me about RCC" says it is part of SJSU, and "tell me
    # about SJSU" says it has clubs — the only way someone asking about one learns the other
    # exists. The count is what this caller may see (a chapter-only member: her own).
    parent = prof.get("parent") if isinstance(prof.get("parent"), dict) else None
    if parent and parent.get("place_name"):
        facts.append(f"It is a chapter (a club inside) of {parent['place_name']}")
    else:
        try:
            n_chapters = len(community_chapters(user_id, pid).get("chapters") or [])
        except ValueError:
            n_chapters = 0
        if n_chapters:
            facts.append(
                f"It has {n_chapters} club{'s' if n_chapters != 1 else ''} (chapters) inside "
                "it on Lana — mention it in passing; they can ask to see them"
            )
    # The community the chat is INSIDE carries what its creator said it is for — the one
    # fact that answers "what do people do here?" on day one, when nobody has added
    # features or meets yet (every creator community today).
    from app.community_opening import active_community_facts

    here = active_community_facts(session_ctx)
    here = here if here and here.get("place_id") == pid else None
    creator_here = bool(here and here.get("kind") == "creator community")
    if inexact:
        # They did not name it exactly, so the reply has to say which place it answered
        # about — otherwise a wrong guess reads as a confident answer.
        facts.append(
            f'They said "{inexact}" and this is the one place near them it could be — '
            "name it in your reply so they can correct you"
        )
    if creator_here:
        # Not the stored member noun: for a creator community that was model-guessed from
        # the name alone ("creative studio 🎨" for a founders' group), and it is not a place.
        facts.append(
            "(How to talk about it, not something to say: it is a creator's community, so "
            "describe it by its topic and purpose only — never a spot, place, venue, local "
            "or nearby, and never whether it is online or physical)"
        )
        if not (
            here.get("about") or here.get("members_help") or here.get("creator_wants")
            or prof.get("description")
        ):
            # "Big Bros" with no description came back as "supporting and mentoring others"
            # — invented from the name. Nothing described is an answer, not a gap to fill.
            facts.append(
                "Its creator has not described it yet — say so plainly and never guess what "
                "it is about from its name"
            )
    elif prof.get("relation"):
        facts.append(f"What kind of place it is: {prof['relation']}")
    if prof.get("description"):
        facts.append(f"How it is described: {prof['description']}")
    if prof.get("place_address"):
        facts.append(f"Where it is: {prof['place_address']}")
    if here:
        if here.get("about") and not prof.get("description"):
            facts.append(f"How it is described: {here['about']}")
        if here.get("members_help"):
            facts.append(f"What members help each other with: {here['members_help']}")
        if here.get("creator_wants"):
            facts.append(
                f"A first question its creator expects people to ask here: {here['creator_wants']}"
            )
        if here.get("creator"):
            facts.append(
                f"It is run by {here['creator']}, whose community this is — refer to them "
                "by name, never with he/she (you do not know their pronouns)"
            )
        facts.append("The person asking is inside this community's chat")
        if creator_here:
            facts.append('Call the people in it "members", never "neighbors"')
    facts.append(
        # "go here" reads as a venue, and the model turned it into "two neighbors who
        # consider it their spot" for a creator's group.
        (f"Members: {count}" if creator_here else f"People who go here: {count}")
        + (f", plus {curious} curious about it" if curious else "")
    )
    if features:
        facts.append("What members say it has: " + ", ".join(features[:6]))
    if events:
        facts.append("Coming up there: " + "; ".join(events[:3]))
        # The meets themselves, as cards — the community screen shows them and chat only
        # described them, so "there's a Sushi & Social Meetup coming up" arrived with
        # nothing to open (QA 2026-08-21). Same rows the browse lane renders.
        from app.discovery_route import _format_event_when

        session_ctx["activity_previews"] = [
            {
                "activity_id": str(e.get("event_id") or "") or None,
                "title": str(e.get("title") or "").strip(),
                "starts_at": str(e.get("starts_at") or "") or None,
                "has_time": e.get("has_time") is not False,
                "starts_label": _format_event_when(e.get("starts_at")),
                "venue_name": str(e.get("venue_name") or "").strip() or place,
                # They are all at THIS place, which the reply already named — except a
                # family meet (a chapter's on its parent), which says where it is from.
                "community": None,
                "preview": True,
                "origin_place_id": str(e.get("origin_place_id") or "").strip() or None,
                "origin_place_name": str(e.get("origin_place_name") or "").strip() or None,
            }
            for e in (prof.get("upcoming_events") or [])
            if str((e or {}).get("title") or "").strip()
        ][:5]
        facts.append(
            "Their cards are under your message — say it is right below rather than "
            "describing it and leaving them to ask for it"
        )
    else:
        # An empty calendar is an ANSWER, not a failed read. Without saying so she wrote
        # "I can't pull up any events for Barnes & Noble right now", which claims a
        # limitation where there is simply nothing on (QA 2026-08-21).
        facts.append(
            "Nothing is scheduled there at the moment — mention it ONLY if they asked about "
            "events, plans or what is on, and then say that as a fact, never as something "
            "you were unable to look up"
        )
    if not features:
        facts.append(
            "Nobody has said yet what it has — only relevant if they asked what it has; "
            "again a fact, not a failed look-up"
        )
    facts.append(
        {
            # Background, not news: "you're already a member and can join the chat inside"
            # was the model narrating this line back to someone who had just joined.
            "member": "They are a member here (background — do not tell them unless asked)",
            "curious": "They joined as curious — they have not said they go here",
        }.get(
            membership,
            # Background for the closing offer, never the opening: "I don't see you in the
            # San Jose State University community yet" answered "is there SJSU?" with a
            # statement about them (prod 2026-10-06).
            "They are not in it yet — background for the offer at the END, never the opening",
        )
    )
    # The community ITSELF, as a card above its events — open it, or join it from the
    # card. Without it a "is there SJSU?" answer showed five events and no way into the
    # community they asked about (prod 2026-10-06).
    is_in = membership in ("member", "curious")
    session_ctx["community_discovery"] = {
        "communities": [
            {
                "place_id": pid,
                "place_name": place,
                "place_address": prof.get("place_address"),
                "place_type": prof.get("place_type") or prof.get("circle_type"),
                "relation": prof.get("relation"),
                "emoji": prof.get("emoji"),
                "member_count": count,
                "is_member": is_in,
                "status_line": _discovery_status_line(count, is_in),
                # "tell me about RCC": the card says it is SJSU's, same as the profile.
                "parent": parent,
            }
        ],
        "total": 1,
        # One named community, not a list — the card heads it as "Community".
        "named": True,
    }
    facts.append(
        "Its own card — open it, or Join from it — is the first thing under your message"
    )
    if membership == "visitor":
        # Being let in is a real next step, and a "yes" should mean something. The card
        # carries the Join, so the chip strip does not offer it a second time.
        _arm_join(session_ctx, pid, place, add_chip=False)
    return compose_reply(
        goal=(
            "Answer what they actually asked about this place, using ONLY the facts. If "
            "they asked whether it exists or is on here, the FIRST words are yes — it is "
            "here — then what it is. If the facts do not hold what they asked, say that "
            "plainly first and then say what you DO know about it — never answer a "
            "different question, never open with whether they are in it, and never "
            "list other communities instead. TWO SHORT SENTENCES."
            + (
                " They are not in it, so you may end by offering to add them."
                if membership == "visitor"
                else ""
            )
        ),
        facts=facts,
        fallback=(
            # A creator community has no geography: "near you" would be false.
            f"{place} — {'a community' if creator_here or far else (prof.get('relation') or 'a spot') + ' near you'}"
            f", with {count} {'person' if count == 1 else 'people'} in it."
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


def _roster_chat_turn(
    user_id: str,
    *,
    community: dict[str, Any],
    message: str,
    session_ctx: dict[str, Any],
) -> str:
    """"Who is in <place>" — answered with the people, not the count.

    The roster the community screen renders was reachable only over HTTP, so this ask
    had nothing to read and returned the member COUNT four times in a row while the UI
    showed all seven names one tap away (QA 2026-08-20). Same read, same rows, same
    Nudge the roster offers — served into chat.
    """
    from app.community_surface import community_members
    from app.reply_compose import compose_reply

    pid = str(community.get("place_id") or "")
    place = str(community.get("place_name") or "").strip()
    from app.community_opening import _community_row, active_community_facts

    _here = active_community_facts(session_ctx)
    if _here and _here.get("place_id") == pid:
        creator_group = _here.get("kind") == "creator community"
    else:
        try:
            creator_group = (_community_row(pid) or {}).get("place_type") == "creator"
        except Exception:  # noqa: BLE001 — wording only; the venue words are the old default
            creator_group = False
    try:
        roster: dict[str, Any] | None = community_members(
            user_id, place_id=pid, phone_verified=True
        )
    except ValueError:
        # 'not_a_member' — the names here belong to the people who go here.
        roster = None
    except Exception:  # noqa: BLE001
        logger.exception("communities_roster_turn_failed place=%s", pid)
        roster = None

    if roster is None:
        # Say the limit before the offer (constitution §9), and arm the join so a "yes"
        # lands: being let in is the actual answer to what they wanted.
        _arm_join(session_ctx, pid, place)
        return compose_reply(
            goal=(
                "Tell them plainly you cannot show who is in this place because they are "
                "not in it yet — the limit FIRST, in one clause, no apology. Then offer to "
                "add them, which is instant and reversible."
            ),
            facts=[
                f"Community they asked about: {place}",
                f"People in it: {int(community.get('member_count') or 0)}",
                "They are NOT in it, so its people are not theirs to see yet",
            ],
            fallback=(
                f"I can't show you who's in {place} until you're in it yourself — "
                "want me to add you?"
            ),
            session_ctx=session_ctx,
            user_message=message,
        )

    # A roster turn is not a join offer: leaving it armed made the next "yes" join
    # something instead of answering.
    session_ctx["community_join_pending"] = None
    everyone = [m for m in (roster.get("members") or []) if not m.get("me")]
    # main._peer_matches_from_ctx ships at most 8 rows, so a bigger roster would have put
    # "11 cards below" over 8 of them. Cap here instead, and say the remainder out loud —
    # a silent truncation reads as the whole roster.
    others = everyone[:_ROSTER_CARDS_MAX]
    hidden = len(everyone) - len(others)
    rows: list[dict[str, Any]] = []
    for m in others:
        attrs = [str(a).strip() for a in (m.get("attributes") or []) if str(a or "").strip()]
        rows.append(
            {
                "peer_user_id": m.get("peer_user_id"),
                "nickname": m.get("nickname"),
                "avatar_url": m.get("avatar_url"),
                # The tie is the PLACE, and their own threads are what else is true about
                # them ([[truthful-peer-match-model]]). similarity_score stays null and
                # there is no badge: nothing here compared two people.
                # "Goes to" is a venue's word; a creator's group has members.
                "matching_peer_label": attrs[0]
                if attrs
                else (f"Member of {place}" if creator_group else f"Goes to {place}"),
                "similarity_score": None,
                "preview": False,
                "trait_tags": attrs[1:4],
                "actions": m.get("actions") or [],
                "connection": m.get("connection"),
                # "member" | "curious" — the roster screen has always tagged a curious
                # joiner, so the chat card must too, or a watcher reads as someone who
                # actually goes there.
                "membership": m.get("membership"),
                # Already final — keeps stamp_peer_discovery_ctx from re-ranking these and
                # wiping their chips, the same contract tip_rec rows have.
                "community_roster": True,
            }
        )
    if rows:
        session_ctx["peer_matches"] = rows
        # A roster compared nobody: no cosine, no bands, no counts strip. Cleared with
        # None rather than popped ([[ctx-pop-resurrection]]) so a scored summary from an
        # earlier peer search cannot ride in over these cards.
        session_ctx["discovery_surface"] = None

    count = int(roster.get("member_count") or 0)
    curious = int(roster.get("curious_count") or 0)
    named = [str(m.get("nickname") or "").strip() for m in others if m.get("nickname")]
    # Three DIFFERENT numbers, all true, and the reply must not blend them: member_count
    # counts the caller and excludes curious joiners, the cards exclude the caller and
    # include them. Stating only one let "7 people" sit over a different number of cards
    # whenever curious_count was not exactly 1 — the count-vs-roster mismatch this whole
    # fix exists to end.
    facts = [
        f"Community: {place}",
        (f"Members: {count}, counting them" if creator_group else f"People who go here: {count}, counting them"),
        f"Cards under your message: {len(others)} — everyone here except them, each with "
        "a Nudge. Give the count and at most two names; do NOT read the names out one by one",
    ]
    if hidden:
        facts.append(
            f"{hidden} more are here without a card — say there are more rather than "
            "implying the cards are everyone"
        )
    if curious:
        facts.append(
            f"Also here without saying they go: {curious} — curious, not members, and "
            "their cards are below too"
        )
    if named:
        facts.append("Names, for anchoring at most TWO of them: " + ", ".join(named[:6]))
    elif others:
        # Guests from a creator's link have no name yet. Told to "anchor with two names"
        # and given none, the model made up "Alex and Jordan" (2026-10-01).
        facts.append(
            "None of them has shared a name yet — do NOT name anyone and never invent a name"
        )
    if creator_group:
        facts.append(
            'A creator\'s group, not a venue: call them "members", never "neighbors", and '
            "never say nearby, local or \"go to\""
        )
    if not others:
        facts.append("They are the only one here so far")
    return compose_reply(
        goal=(
            "Answer who is in this place. Say how many and that their cards are right "
            "below, "
            + ("anchor with at most TWO names from the facts, " if named else "")
            + "and offer an intro to any of them. TWO SHORT SENTENCES, never a list."
            if others
            else "Tell them they are the only one here so far, and offer to help them "
            "bring someone in or start something here. Never call the place dead."
        ),
        facts=facts,
        fallback=(
            f"{count} people are in {place} — the others are right below. "
            "Want an intro to any of them?"
            if others
            else f"You're the only one in {place} so far — want to invite someone?"
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


# What the PWA's community edit screen (CommunityEditDrawer) can change. Facts, not
# copy: the reply is authored from these, and anything not listed is not promised.
_EDITABLE_ON_SCREEN = (
    "the spot it is pinned to (pick a different place on the map — this is how its "
    "location changes)",
    "their own note about when they go",
    "the activities they do there",
    "removing it from their communities",
)


def _chapter_change_turn(
    user_id: str,
    *,
    community: dict[str, Any],
    action: str,
    parent_said: str | None,
    message: str,
    session_ctx: dict[str, Any],
) -> str:
    """"Put RCC under SJSU" / "make RCC standalone" — done, or why not, in one reply.

    The SQL holds the rule (20270125120000): she must run the community she is moving and
    belong to the one it goes inside; the runner of either side may take it out. Every
    refusal is said plainly with the one move that fixes it."""
    from app.community_chapter_ops import attach_chapter, detach_chapter
    from app.reply_compose import compose_reply

    pid = str(community.get("place_id") or "")
    name = str(community.get("place_name") or "").strip() or "your community"
    facts: list[str]
    if action == "detach":
        got = detach_chapter(user_id, pid)
        if got.get("ok") and got.get("was_attached"):
            facts = [f"Done: {name} is no longer part of {got.get('parent_name')} — it stands on its own now"]
        elif got.get("ok"):
            facts = [f"{name} was not part of any other community — nothing to change"]
        elif got.get("reason") == "not_your_community":
            facts = [
                f"Only whoever runs {name} or the community it is in can take it out — "
                "they don't, so it stays as it is"
            ]
        else:
            facts = [f"Taking {name} out did not work just now — nothing changed"]
        return compose_reply(
            goal="Tell them what happened, in one or two short sentences.",
            facts=facts,
            fallback=facts[0] + ".",
            session_ctx=session_ctx,
            user_message=message,
            max_sentences=2,
        )

    if not parent_said:
        return compose_reply(
            goal=f"Ask in one short question which community {name} should go inside.",
            facts=[f"They want {name} to be part of a bigger community but did not say which"],
            fallback=f"Which community should {name} be part of?",
            session_ctx=session_ctx,
            user_message=message,
            max_sentences=1,
        )
    parent = find_named_community(user_id, parent_said)
    if not parent:
        facts = [f'There is no community called "{parent_said}" on Lana that you can find']
    else:
        pname = str(parent.get("place_name") or parent_said)
        got = attach_chapter(user_id, pid, str(parent["place_id"]))
        reason = got.get("reason")
        if got.get("ok"):
            facts = [
                f"Done: {name} is now a club inside {pname}"
                + (" (it was already)" if got.get("already") else "")
                + f" — people looking at {pname} will see it"
            ]
            if got.get("inherited_location"):
                facts.append(f"It had no spot of its own, so it now shows at {pname}'s location")
        elif reason == "not_a_member_of_parent":
            _arm_join(session_ctx, str(parent["place_id"]), pname)
            facts = [
                f"They are not a member of {pname}, and only members can add a club to it. "
                f"Offer to add them to {pname} first (the button does that), then ask again"
            ]
        elif reason == "not_your_community":
            facts = [f"Only whoever started or runs {name} can put it inside another community"]
        elif reason in ("chapter_depth_exceeded", "parent_cannot_become_a_chapter"):
            facts = [
                f"It cannot go there: clubs are one level deep, and {pname} or {name} is "
                "already part of that structure the other way round"
            ]
        elif reason == "chapter_has_another_parent":
            facts = [f"{name} is already part of another community — take it out of that one first"]
        elif reason == "creator_community_cannot_be_chapter":
            facts = [f"{name} is a creator's community, which can't sit inside another one"]
        elif reason == "chapter_needs_location":
            facts = [f"Neither {name} nor {pname} has a location yet, and a club inside one needs a spot"]
        else:
            facts = [f"Putting {name} inside {pname} did not work just now — nothing changed"]
    return compose_reply(
        goal="Tell them what happened, plainly, in one or two short sentences.",
        facts=facts,
        fallback=facts[0] + ".",
        session_ctx=session_ctx,
        user_message=message,
        max_sentences=2,
    )


def _manage_turn(
    user_id: str,
    *,
    community: dict[str, Any] | None,
    message: str,
    session_ctx: dict[str, Any],
    chapter_change: tuple[str | None, str | None] = (None, None),
) -> str:
    """"I want to update my community" — point at the screen that does it.

    Chat cannot edit a community, and the turn used to say only that: "I can't update the
    San Jose State University community for you", with no way forward, to the person who
    started it (QA 2026-10-04). The edit screen already exists; this answers with a button
    that opens it on THEIR row, and names what it can change."""
    from app.community_scope import active_community
    from app.reply_compose import compose_reply

    session_ctx["peer_matches"] = None
    session_ctx["discovery_surface"] = None
    mine = _my_communities(user_id)

    if community is None:
        here = active_community(session_ctx)
        here_id = str((here or {}).get("place_id") or "")
        community = next((c for c in mine if str(c.get("place_id")) == here_id), None) if here_id else None
        if community is None and len(mine) == 1:
            community = mine[0]
    if community is None:
        if not mine:
            return compose_reply(
                goal=(
                    "They want to update a community, but they are not in any yet. Say so "
                    "in one warm line and offer to help them start or join one."
                ),
                fallback="You're not in any communities yet — want to start one or join one nearby?",
                session_ctx=session_ctx,
                user_message=message,
                max_sentences=1,
            )
        names = [str(c.get("place_name") or "") for c in mine[:3]]
        session_ctx["policy_chips"] = [
            {"label": n, "send": _CHIP_ASK["manage"].format(place=n)} for n in names if n
        ]
        return compose_reply(
            goal=(
                "They want to update one of their communities but did not say which. Ask "
                "which one in ONE short question — the names are buttons under your message."
            ),
            facts=["Their communities (the buttons): " + ", ".join(names)],
            fallback="Which community do you want to update?",
            session_ctx=session_ctx,
            user_message=message,
            max_sentences=1,
        )

    place_id = str(community.get("place_id") or "")
    name = str(community.get("place_name") or "").strip()
    own = next((c for c in mine if str(c.get("place_id")) == place_id), None)
    if own is None:
        _arm_join(session_ctx, place_id, name)
        return compose_reply(
            goal=(
                "They asked to change a community they are not part of. Say plainly that "
                "it isn't one of theirs, so there is nothing of theirs to edit there, and "
                "offer to add them — the buttons under your message do that."
            ),
            facts=[f"The community: {name}", "They are NOT a member of it"],
            fallback=f"{name} isn't one of your communities yet — want me to add you?",
            session_ctx=session_ctx,
            user_message=message,
        )

    if chapter_change and chapter_change[0]:
        return _chapter_change_turn(
            user_id,
            community=own,
            action=str(chapter_change[0]),
            parent_said=chapter_change[1],
            message=message,
            session_ctx=session_ctx,
        )

    # open_panel/affiliation_id make the chip open the edit screen on this row; `send` is
    # what an older client posts instead, which lists their communities — harmless.
    session_ctx["policy_chips"] = [
        {
            "label": f"Edit {name}",
            "send": "show my communities",
            "open_panel": "communities",
            "affiliation_id": str(own.get("id") or ""),
        }
    ]
    return compose_reply(
        goal=(
            "They want to change something about their community. You cannot edit it from "
            "chat, but the button under your message opens its edit screen. In two short "
            "sentences: say the button opens it, and name what they can change there that "
            "fits what they asked (a location change is 'change the spot'). If they asked "
            "for something not on the list, such as renaming it or editing the description "
            "everyone sees, say plainly that isn't editable yet. Never refuse without "
            "pointing at the button."
        ),
        facts=[
            f"The community: {name} ({_members_phrase(own)})",
            "On its edit screen they can change: " + "; ".join(_EDITABLE_ON_SCREEN),
            "Not editable anywhere in the app yet: the community's name and the description "
            "everyone sees",
        ],
        fallback=(
            f"Tap Edit {name} below — you can change its spot, your note, and your "
            "activities there."
        ),
        session_ctx=session_ctx,
        user_message=message,
    )


def communities_chat_turn(
    user_id: str,
    *,
    message: str,
    session_ctx: dict[str, Any],
    community_name: str | None = None,
    community_ask: str = "about",
    community_topic: str | None = None,
    chapter_change: tuple[str | None, str | None] = (None, None),
) -> str:
    """Answer a community ask (`discovery.communities`) from real rows.

    Before this existed the ask had no handler, so the classifier sent it to whichever
    arm looked closest — one probe got the area-not-open host bridge ("there aren't any
    local communities to show yet", asserted without counting anything), another got an
    attribute peer search for neighbours "interested in community". Both were wrong
    about the data: the asking account had two communities in its own ZIP.

    Answers with both halves, because either alone is misleading: the ones they are
    already in, and the ones nearby they could join. Every number here is a real read;
    when both halves are genuinely empty the reply says so and offers the one useful
    move (start one), never a claim that the area is too quiet to have any.
    """
    # "who is in <place>" — a named community is a roster ask, not a list ask. Falls
    # through to the list below when the name matches nothing we hold, with the miss
    # stated rather than papered over with a list they did not ask for.
    named_miss: str | None = None
    if community_ask == "chapters" and not community_name:
        # "What clubs are here?" — here is the community they are chatting inside. With no
        # community at all there is nothing to be inside of: community_name stays None and
        # the topic/list path below answers it as a search across communities.
        from app.community_scope import community_name as _active_name

        community_name = _active_name(session_ctx)
    if community_ask == "manage" and not community_name:
        # "update the community I created" names none — the manage turn picks theirs.
        return _manage_turn(
            user_id, community=None, message=message, session_ctx=session_ctx,
            chapter_change=chapter_change,
        )
    if community_name:
        said = community_name.strip()[:80]
        # One resolver for every named-community caller (resolve_community_name): hers,
        # near her, anywhere by name and meaning, a one-word near miss, then a short form
        # the model spells out ("SJSU" asked from far away, 2026-10-07).
        got = resolve_community_name(user_id, community_name)
        hit, inexact = got["hit"], got["inexact"]
        if not hit and got["near"]:
            # Genuinely ambiguous: three Lake Nona gyms are a "which one", and picking
            # for them would be a guess.
            session_ctx["peer_matches"] = None
            session_ctx["discovery_surface"] = None
            return _did_you_mean_turn(
                said=said,
                candidates=got["near"],
                message=message,
                session_ctx=session_ctx,
                ask=community_ask,
            )
        if hit:
            topic_s = str(community_topic or "").strip()[:80] or None
            if community_ask == "about" and topic_s and _has_chapters(user_id, hit):
                # The AI read a named community AND a subject being looked for ("any clubs
                # at San Jose State focused on AI ethics?" came back ask=about,
                # topic='AI ethics'). The subject is a search INSIDE it: answered as the
                # about card, it said "join SJSU to explore its clubs" and never named
                # RCC, the club whose whole blurb is AI ethics (prod 2026-10-07).
                community_ask = "chapters"
            if community_ask == "chapters":
                return _chapters_turn(
                    user_id,
                    parent=hit,
                    topic=topic_s,
                    message=message,
                    session_ctx=session_ctx,
                )
            if community_ask == "manage":
                return _manage_turn(
                    user_id, community=hit, message=message, session_ctx=session_ctx,
                    chapter_change=chapter_change,
                )
            if community_ask == "people":
                return _roster_chat_turn(
                    user_id, community=hit, message=message, session_ctx=session_ctx
                )
            about = _community_about_turn(
                user_id,
                community=hit,
                message=message,
                session_ctx=session_ctx,
                inexact=inexact,
            )
            if about:
                return about
        named_miss = said

    # Not a roster turn: whatever cards the last one shipped are not these cards.
    # peer_matches is not turn-scoped, and discovery.communities now renders it, so a
    # roster's faces would otherwise re-appear under an unrelated communities reply —
    # the "same card for the third time" shape from the QA report.
    session_ctx["peer_matches"] = None
    session_ctx["discovery_surface"] = None

    mine = _my_communities(user_id)
    topic = str(community_topic or "").strip()[:80]
    if topic and community_ask != "mine" and not named_miss:
        return _topic_communities_turn(
            user_id, topic=topic, message=message, session_ctx=session_ctx
        )
    nearby = [
        c for c in discover_communities(user_id, limit=_CHAT_NEARBY_MAX * 2)
        if not c.get("is_member")
    ][:_CHAT_NEARBY_MAX]
    # Same card as /lana/circles/discover, so the same number on it — one RPC, no LLM.
    # The authored fit line is endpoint-only: it costs a compose, and this turn is
    # already waiting on one.
    from app.community_affinity import attach_affinity

    attach_affinity(user_id, nearby)
    for c in nearby:
        c.pop("_fit_basis", None)

    session_ctx["community_discovery"] = {
        "communities": nearby,
        "total": len(nearby),
    }
    # Armed for ONE turn so the next message can be read as "Join <place>" — the
    # *_pending twin convention (turn_surfaces.py): the card is turn-scoped, this
    # is not, because the turn that answers it has to still see it.
    session_ctx["community_join_pending"] = (
        {"places": [{"place_id": c["place_id"], "place_name": c["place_name"]} for c in nearby]}
        if nearby
        else None
    )
    if mine:
        from app.community_surface import communities_card

        card = communities_card(user_id, top=len(mine))
        if card:
            session_ctx["communities_card"] = card

    facts: list[str] = []
    if named_miss:
        # Constitution §9: the thing they actually named comes first, even when the
        # answer is that we do not have it.
        facts.append(
            f'They asked about "{named_miss}" and there is NO community by that name '
            "in theirs or near them — say that plainly before anything else, and never "
            "add that they are in one by that name (that contradiction shipped 2026-10-04)"
        )
    # "Which communities am I in?" is a question about THEIRS. It used to get the nearby
    # answer — a count, one name, and a pitch for places to join — so SJSU and RCC were
    # never named to the person in both (QA 2026-10-05). A handful of their own names is
    # the answer; the roll-call rule below is about the list of strangers' communities.
    asks_mine = community_ask == "mine" and bool(mine)
    if asks_mine:
        names = [str(c["place_name"]) for c in mine[:_MINE_NAMED_MAX]]
        more = len(mine) - len(names)
        facts.append(
            f"The communities they are in ({len(mine)}): " + "; ".join(
                f"{c['place_name']} ({_members_phrase(c)})" for c in mine[:_MINE_NAMED_MAX]
            ) + (f"; and {more} more" if more > 0 else "")
        )
    elif mine:
        # COUNT + one name, not the roll-call. Handing the model six names and four more
        # made it read every one out, and the cards under the message then repeated all
        # ten: a ten-line wall answering a one-line question (QA 2026-08-18).
        facts.append(
            f"Communities they are already in: {len(mine)} "
            f"(one of them: {mine[0]['place_name']}, {_members_phrase(mine[0])})"
        )
    else:
        facts.append("They are not in any community yet")
    if nearby:
        facts.append(
            f"Nearby communities they could join: {len(nearby)} "
            f"(closest: {nearby[0]['place_name']} — {nearby[0]['status_line']}). "
            "The cards under your message list all of them with real member counts, "
            "so your text must NOT name them one by one"
        )
    else:
        facts.append(
            "Nobody nearby has a community yet that they are not already in — do NOT "
            "guess at reasons and do NOT say their area is too quiet to have any"
        )

    if asks_mine:
        goal = (
            "They asked which communities they are in. Answer exactly that: name each of "
            "their communities from the facts in one sentence (commas, not bullets). "
            + (
                "Then, in at most one short clause, mention there are others nearby they "
                "could join — the cards show them, so name none."
                if nearby
                else "Do not pitch anything else."
            )
        )
        fallback = "You're in " + ", ".join(names) + (f", and {more} more." if more > 0 else ".")
    elif nearby:
        goal = (
            "Answer what they asked: the communities near them. TWO SHORT SENTENCES, and "
            "never a list — the cards under your message carry every name, so a name-by-name "
            "roll-call in the text is the same information twice and unreadable on a phone. "
            "Give the counts, anchor with at most ONE name from the facts, and end by "
            "offering to add them to any — joining is instant and reversible, no warnings "
            "needed."
        )
        fallback = _nearby_fallback(mine, nearby)
    elif mine:
        goal = (
            "Tell them which communities they are already in (from the facts), then say "
            "honestly that nothing NEW has turned up nearby yet, and offer to keep an "
            "ear out or help them start something at a spot they already go to."
        )
        fallback = (
            "Right now you're in "
            + ", ".join(c["place_name"] for c in mine[:3])
            + ". Nothing new nearby yet — want me to keep an ear out?"
        )
    else:
        goal = (
            "Say honestly that no community near them has anyone in it yet — theirs "
            "would be the first — then offer the concrete move: name a spot they go to "
            "and you'll set it up. Never blame them, never call the area dead."
        )
        fallback = (
            "No communities near you have anyone in them yet — yours would be the "
            "first. Tell me a spot you go to and I'll set it up."
        )

    from app.reply_compose import compose_reply

    return compose_reply(
        goal=goal,
        facts=facts,
        fallback=fallback,
        session_ctx=session_ctx,
        user_message=message,
        # Two, not three: the cards are the list, so the text is a summary + an offer.
        max_sentences=2,
    )


def _has_chapters(user_id: str, community: dict[str, Any]) -> bool:
    """Does this community have any chapter the caller may see? False on any failure."""
    try:
        return bool(
            community_chapters(user_id, str(community.get("place_id") or "")).get("chapters")
        )
    except ValueError:
        return False


def _chapters_turn(
    user_id: str,
    *,
    parent: dict[str, Any],
    topic: str | None,
    message: str,
    session_ctx: dict[str, Any],
) -> str:
    """"What clubs does SJSU have?" / "any AI clubs at SJSU?" — the chapters INSIDE one
    community, never a search across communities.

    Lists them as the caller may see them (community_chapters: a parent member sees every
    chapter, a chapter-only member never a sibling). A topic narrows the list by name and
    meaning — the same match the topic search uses, intersected with this community's
    chapters. When nothing inside answers, the reply says so about THIS community and any
    cards shown from outside it are labelled as outside, so a wrong read is visible.
    """
    from app.reply_compose import compose_reply

    session_ctx["peer_matches"] = None
    session_ctx["discovery_surface"] = None
    pid = str(parent.get("place_id") or "")
    parent_name = str(parent.get("place_name") or "").strip() or "this community"
    try:
        chapters = community_chapters(user_id, pid).get("chapters") or []
    except ValueError:
        chapters = []
    head = {"place_id": pid, "place_name": parent_name}
    for c in chapters:
        c["parent"] = head

    inside = chapters
    outside: list[dict[str, Any]] = []
    if topic and chapters:
        ids = {c["place_id"] for c in chapters}
        matched = discover_communities_anywhere(
            user_id, topic, placeless_only=False, by_meaning=True, limit=20
        )
        order = [m["place_id"] for m in matched if m["place_id"] in ids]
        by_id = {c["place_id"]: c for c in chapters}
        inside = [by_id[i] for i in order]
    if topic and not inside:
        outside = [
            c for c in discover_communities_anywhere(
                user_id, topic, limit=_CHAT_NEARBY_MAX * 3, by_meaning=True,
                radius_m=radius_meters(),
            )
            if not c.get("is_member") and c["place_id"] != pid
            and (c.get("parent") or {}).get("place_id") != pid
            and c.get("reach") in (None, "placeless", "nearby")
        ][:_CHAT_NEARBY_MAX]

    cards = (inside or outside)[:_CHAT_NEARBY_MAX * 2]
    session_ctx["community_discovery"] = {
        "communities": cards,
        "total": len(cards),
        "topic": topic,
        # Which community these sit inside — the card heading is "Clubs in SJSU", not
        # "Communities near you". Absent when the cards are from outside it.
        "within": head if inside else None,
    }
    joinable = [c for c in cards if not c.get("is_member")]
    session_ctx["community_join_pending"] = (
        {"places": [{"place_id": c["place_id"], "place_name": c["place_name"]} for c in joinable]}
        if joinable
        else None
    )

    what = f"{topic} clubs" if topic else "clubs or groups"
    facts = [f"They asked what {what} there are INSIDE {parent_name} (its chapters)"]
    if inside:
        mine = [c for c in inside if c.get("is_member")]
        facts.append(
            f"{parent_name} has {len(inside)} on Lana"
            + (f" about {topic}" if topic else "")
            + f" (best match: {inside[0]['place_name']}, {inside[0]['status_line']})"
        )
        if mine:
            facts.append(f"They are already in {mine[0]['place_name']} — say so")
        facts.append(
            "The cards under your message list every one with real member counts, so your "
            "text must NOT name them one by one"
        )
        best = str(inside[0]["place_name"])
        # A narrowed ask is answered by NAME: "any clubs at SJSU about AI ethics?" is a
        # question whose answer is "yes — the Responsible Computing Club", and a count with
        # the name left to the cards read as "there are clubs, go look" (prod 2026-10-07).
        goal = (
            f"Answer what they asked: the {what} inside {parent_name}. TWO SHORT SENTENCES, "
            "never a list. "
            + (
                f"Open by naming the best match, {best}, as the answer — "
                if topic
                else "Say what turned up (at most ONE name), "
            )
            + "and offer to add them — joining is instant and reversible."
        )
        fallback = (
            (
                f"Yes — {best} at {parent_name} is about {topic}"
                + (f", plus {len(inside) - 1} more below" if len(inside) > 1 else "")
                + ". Want me to add you?"
            )
            if topic
            else (
                f"{parent_name} has {len(inside)} "
                f"{'club' if len(inside) == 1 else 'clubs'} on Lana — they're below. "
                "Want me to add you to one?"
            )
        )
    elif outside:
        facts.append(
            f"None of {parent_name}'s clubs on Lana are about {topic}"
            if chapters
            else f"{parent_name} has no clubs listed on Lana yet"
        )
        facts.append(
            f"Communities about {topic} near them, NOT part of {parent_name}: {len(outside)} "
            f"(best match: {outside[0]['place_name']}). Say plainly they are not part of "
            f"{parent_name}"
        )
        goal = (
            f"Say in a few words that nothing inside {parent_name} is about {topic}, then "
            f"offer the {topic} communities below, making clear they are not part of "
            f"{parent_name}. TWO SHORT SENTENCES, never a list."
        )
        fallback = (
            f"Nothing inside {parent_name} is about {topic} yet, but there "
            f"{'is one' if len(outside) == 1 else f'are {len(outside)}'} nearby — "
            "below. Want me to add you?"
        )
    else:
        facts.append(
            f"None of {parent_name}'s clubs on Lana are about {topic}"
            if chapters and topic
            else f"{parent_name} has no clubs listed on Lana yet"
        )
        subject = f"a {topic} club" if topic else "a club"
        goal = (
            f"Say plainly that {parent_name} has no {what} on Lana yet, then offer to start "
            f"{subject} as part of {parent_name}. Never blame them. ONE OR TWO SHORT SENTENCES."
        )
        fallback = (
            f"{parent_name} has no {what} on Lana yet. Want to start {subject} there?"
        )

    return compose_reply(
        goal=goal,
        facts=facts,
        fallback=fallback,
        session_ctx=session_ctx,
        user_message=message,
        max_sentences=2,
    )


def _topic_communities_turn(
    user_id: str,
    *,
    topic: str,
    message: str,
    session_ctx: dict[str, Any],
) -> str:
    """"Any communities for podcasters?" — answered by what communities ARE, not where.

    Placeless communities (made without a location, or a creator's) can only ever be found
    this way. Nearby ones whose NAME carries the subject come along, labelled as nearby;
    the rest of the neighbourhood list is not what they asked for and is left out.
    """
    # By name, own words AND meaning, local communities included: "a club about AI ethics"
    # must reach the Responsible Computing Club, which has a location, is a chapter of San
    # Jose State, and shares no word with the ask (prod 2026-10-06).
    found = discover_communities_anywhere(
        user_id,
        topic,
        limit=_CHAT_NEARBY_MAX * 3,
        by_meaning=True,
        radius_m=radius_meters(),
    )
    already = [c for c in found if c.get("is_member")]
    others = [c for c in found if not c.get("is_member")]
    anywhere = [c for c in others if c.get("reach") in (None, "placeless")][:_CHAT_NEARBY_MAX]
    seen = {c["place_id"] for c in found}
    nearby = [c for c in others if c.get("reach") == "nearby"]
    nearby += [
        c for c in discover_communities(user_id, query=topic, limit=_CHAT_NEARBY_MAX * 2)
        if not c.get("is_member") and c["place_id"] not in seen
    ]
    nearby = nearby[:_CHAT_NEARBY_MAX]
    # Beyond the radius: shown only when nothing closer answers, and said to be elsewhere —
    # "nothing near you, but there's one in San Jose" instead of "there is none".
    far = [] if (nearby or anywhere) else [
        c for c in others if c.get("reach") == "far"
    ][:_CHAT_NEARBY_MAX]
    joinable = nearby + anywhere + far
    # Theirs come first and are SHOWN, marked as theirs — hiding them made "Podcasters"
    # vanish for the person who started it (prod 2026-10-06).
    cards = already[:_CHAT_NEARBY_MAX] + joinable
    session_ctx["community_discovery"] = {
        "communities": cards,
        "total": len(cards),
        "topic": topic,
    }
    # Armed for one turn, exactly as the nearby list is, so "Join Podcast Club" works.
    # Only the ones they are not in: there is nothing to join in their own.
    session_ctx["community_join_pending"] = (
        {"places": [{"place_id": c["place_id"], "place_name": c["place_name"]} for c in joinable]}
        if joinable
        else None
    )

    facts: list[str] = [f'They are looking for communities about "{topic}"']
    if already:
        facts.append(
            f"They are ALREADY IN one about it: {already[0]['place_name']} — say so first, "
            "plainly (its card is marked as theirs)"
        )
    if anywhere:
        best = anywhere[0]
        facts.append(
            f"Communities about it that are not tied to one place, so anyone can join from "
            f"anywhere: {len(anywhere)} (best match: {best['place_name']} — "
            f"{best['status_line']}). Never call these near them"
        )
    if nearby:
        facts.append(
            f"Nearby communities about it: {len(nearby)} (best match: "
            f"{nearby[0]['place_name']})"
        )
    if far:
        best = far[0]
        where = f" in {best['area']}" if best.get("area") else ""
        facts.append(
            f"Nothing near them, but {len(far)} further away (best match: "
            f"{best['place_name']}{where}). Say plainly it is not near them and name where it "
            "is — never call it local"
        )
    chapters = [c for c in joinable + already if c.get("parent")]
    for c in chapters[:2]:
        facts.append(
            f"{c['place_name']} is a chapter of {c['parent'].get('place_name')} — say so if "
            "you name it"
        )
    if joinable:
        facts.append(
            "The cards under your message list every one with real member counts, so your "
            "text must NOT name them one by one"
        )

    if far:
        goal = (
            f"They asked for communities about {topic}. None is near them — say so in a few "
            "words, then name the best one further away and where it is, and offer to add "
            "them. TWO SHORT SENTENCES, never a list."
        )
        where = f" in {far[0]['area']}" if far[0].get("area") else ""
        fallback = (
            f"Nothing about {topic} near you, but {far[0]['place_name']}{where} is a match — "
            "it's below. Want me to add you?"
        )
    elif joinable:
        goal = (
            f"Answer what they asked: communities about {topic}. TWO SHORT SENTENCES, never a "
            "list. Say what turned up (at most ONE name), make clear any that are not local can "
            "be joined from anywhere, and offer to add them — joining is instant and reversible."
        )
        fallback = (
            f"I found {len(joinable)} communit{'y' if len(joinable) == 1 else 'ies'} about "
            f"{topic} — they're below. Want me to add you to one?"
        )
    elif already:
        goal = (
            f"They asked for communities about {topic}. The only one is theirs — say so in "
            "one warm line, naming it, and add that nobody else has started one yet."
        )
        fallback = (
            f"You're already in {already[0]['place_name']} — that's the one about {topic} "
            "so far."
        )
    else:
        goal = (
            f"Say plainly there is no community about {topic} yet. Then offer to start one: "
            f"anyone who looks for {topic} would find it. Never blame them or their area."
        )
        fallback = (
            f"There's no community about {topic} yet. Want to start one? Anyone looking "
            f"for {topic} would find it."
        )

    from app.reply_compose import compose_reply

    return compose_reply(
        goal=goal,
        facts=facts,
        fallback=fallback,
        session_ctx=session_ctx,
        user_message=message,
        max_sentences=2,
    )


_JOIN_VERB_RE = re.compile(
    r"\b(join|add me|sign me up|put me in|i go there|count me in|i'?m in)\b", re.IGNORECASE
)
# A tap posts "Join <place>"; typed replies are short too. Anything longer is a new ask.
_JOIN_REPLY_MAX_LEN = 120


def read_join_reply(user_id: str, message: str, session_ctx: dict[str, Any]) -> dict[str, Any] | None:
    """Read a reply to the "want to join one of these?" offer.

    Returns the join result plus the matched place, or None to fall through to normal
    routing (the offer is consumed either way — an unrelated next message must not stay
    armed, per [[ctx-pop-resurrection]] this clears with None, never pop).

    Matching is deterministic NAME matching, not intent classification: the chips post
    "Join <place>" verbatim, and a typed "join lp fit" names the place too. A bare "yes"
    only joins when exactly ONE community was offered — with three on screen, "yes"
    doesn't say which, and guessing would write the wrong membership.
    """
    pending = session_ctx.get("community_join_pending")
    if not isinstance(pending, dict):
        return None
    session_ctx["community_join_pending"] = None
    text = str(message or "").strip()
    if not text or len(text) > _JOIN_REPLY_MAX_LEN:
        return None
    places = [p for p in (pending.get("places") or []) if isinstance(p, dict)]
    if not places:
        return None

    named = _match_offered_place(text, places)
    wants_join = bool(_JOIN_VERB_RE.search(text)) or _is_bare_yes(text)
    if named is None:
        # "yes" / "join" with nothing named: only unambiguous with a single offer.
        if wants_join and len(places) == 1:
            named = places[0]
        else:
            return None
    if not wants_join and not _looks_like_only_a_name(text, named):
        return None

    try:
        result = join_community(user_id, str(named.get("place_id") or ""))
    except ValueError as exc:
        logger.warning("community_join_reply_failed place=%s err=%s", named.get("place_id"), exc)
        return None
    result["place_name"] = result.get("place_name") or named.get("place_name")
    return result


_BARE_YES_RE = re.compile(r"^(yes|yeah|yep|yup|sure|ok(?:ay)?|please|do it|go ahead)\b[\s!.]*$",
                          re.IGNORECASE)


def _is_bare_yes(text: str) -> bool:
    return bool(_BARE_YES_RE.match(text.strip()))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _match_offered_place(text: str, places: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The offered place whose name the message actually contains. Longest name first,
    so "Heroes Community Park" wins over a shorter name nested inside it."""
    hay = _norm(text)
    if not hay:
        return None
    for place in sorted(places, key=lambda p: -len(str(p.get("place_name") or ""))):
        name = _norm(place.get("place_name"))
        if name and name in hay:
            return place
    return None


def _looks_like_only_a_name(text: str, place: dict[str, Any]) -> bool:
    """The user typed just the place name with no verb ("Lp Fit") — still an answer to
    "which one?". Guarded so a sentence ABOUT the place isn't read as a join."""
    return _norm(text) == _norm(place.get("place_name"))


def join_confirm_reply(
    result: dict[str, Any],
    *,
    session_ctx: dict[str, Any],
    message: str,
    member_count: int | None = None,
) -> str:
    """Confirm a join in one warm line, from facts only. Says whether they were already
    in, and offers the ONE next step that is real: seeing who else is there."""
    name = str(result.get("place_name") or "the spot")
    if result.get("already_member"):
        facts = [f"They are already in {name} — nothing changed"]
        goal = (
            "Tell them they're already in this one (no double-join), and offer the real "
            "next step: seeing who else is there."
        )
        fallback = f"You're already in {name}. Want to see who else is there?"
    else:
        facts = [f"They just joined {name}"]
        if member_count and member_count > 1:
            facts.append(f"{member_count} people are in it now, including them")
        if result.get("promoted_from_candidate"):
            facts.append(
                "This is the same place they had mentioned to you before — now it's "
                "confirmed. Do not treat it as new information about them."
            )
        goal = (
            "Confirm the join warmly in one line using the real count if given, then "
            "offer the one real next step: seeing who else is there. Never promise "
            "anyone is waiting for them."
        )
        fallback = f"Done — you're in {name}. Want to see who else is there?"

    from app.reply_compose import compose_reply

    return compose_reply(
        goal=goal,
        facts=facts,
        fallback=fallback,
        session_ctx=session_ctx,
        user_message=message,
    )


def _my_communities(user_id: str) -> list[dict[str, Any]]:
    from app.circles_flow import list_my_circles

    try:
        rows = list_my_circles(user_id)
    except Exception:
        logger.exception("communities_chat_mine_failed user=%s", user_id)
        return []
    return [r for r in rows if r.get("place_name")]


def _members_phrase(community: dict[str, Any]) -> str:
    n = int(community.get("member_count") or 0)
    if n <= 1:
        return "just them so far"
    return f"{n} people"


def _nearby_fallback(mine: list[dict[str, Any]], nearby: list[dict[str, Any]]) -> str:
    """Counts and one anchor name — the cards below carry the full list."""
    n = len(nearby)
    more = f" and {n - 1} more" if n > 1 else ""
    head = (
        f"You're in {len(mine)} already, and there {'is' if n == 1 else 'are'} {n} more "
        f"near you — {nearby[0]['place_name']}{more}."
        if mine
        else (
            f"{n} near you — {nearby[0]['place_name']} ({nearby[0]['status_line']})"
            f"{more}."
        )
    )
    return f"{head} Want me to add you to any of them?"


def _confirmed_member_count(place_id: str) -> int:
    """How many people are confirmed members here — the one fact that makes a "somebody
    joined" mail feel like a community growing. 0 on any failure, and the row is dropped
    rather than showing a wrong number."""
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("id", count="exact")
            .eq("place_ref", place_id)
            .eq("status", "confirmed")
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        return int(res.count or 0)
    except Exception:  # noqa: BLE001
        return 0


def mail_join_to_members(place_id: str, place_name: str, joiner_id: str) -> int:
    """Email the community's existing confirmed members that somebody new joined.
    Same roster the community's meets mail. Returns how many were mailed."""
    from app.i18n import t
    from app.notifications import _user_contact, email_html, mail_community_members

    _, nickname = _user_contact(joiner_id)
    members = _confirmed_member_count(place_id)

    def render(lang: str | None) -> tuple[str, str]:
        name = nickname or t("notify.community_join.somebody", lang)
        return (
            t("notify.community_join.subject", lang, name=name, place=place_name),
            email_html(
                t("notify.community_join.title", lang, name=name, place=place_name),
                t("notify.community_join.body", lang, place=place_name),
                t("notify.community_join.cta", lang),
                # Opens the chat INSIDE this community (the PWA reads ?inside=, checks they
                # belong, sets the pill and starts there) — "/" dropped them in plain Lana.
                f"/chat?inside={place_id}",
                preheader=t("notify.community_join.preheader", lang, name=name),
                badge="👋",
                kicker=t("notify.community_note", lang, name=place_name),
                facts=[
                    (t("notify.facts.community", lang), place_name),
                    (
                        t("notify.facts.members", lang),
                        t("notify.facts.member_count", lang, n=members) if members else "",
                    ),
                ],
            ),
        )

    return mail_community_members(place_id, exclude_user_id=joiner_id, render=render)


def notify_members_of_join(place_id: str, place_name: str, joiner_id: str) -> None:
    """Fire-and-forget wrapper — the join tap never waits on a mail-out, and never
    fails because of one. An unnamed community has nothing to say, so it stays quiet."""
    if not place_id or not place_name:
        return

    def _run() -> None:
        try:
            mail_join_to_members(place_id, place_name, joiner_id)
        except Exception:
            logger.exception("community_join_mail_failed place=%s", place_id)

    threading.Thread(
        target=_run, daemon=True, name=f"circle-join-{str(place_id)[:8]}"
    ).start()


def _after_join(
    user_id: str,
    affiliation_id: str,
    candidate: dict[str, Any] | None,
    place_id: str,
    place_name: str,
) -> None:
    """Best-effort: none of this may cost the user the join they just made."""
    from app.circles_flow import _close_grounding_gap, _flush_parked_features

    if candidate:
        try:
            _flush_parked_features(user_id, candidate, place_id)
        except Exception:
            logger.exception("community_join_feature_flush_failed aff=%s", affiliation_id)
        try:
            _close_grounding_gap(affiliation_id)
        except Exception:
            logger.exception("community_join_gap_close_failed aff=%s", affiliation_id)
    if not place_name:
        return
    try:
        from app.circles_flow import _place_affinity_question
        from app.rapport_gaps import open_semantic_gap

        question, teaser, chips = _place_affinity_question(place_name)
        open_semantic_gap(
            user_id,
            None,
            question,
            label=place_name,
            bucket="interest",
            teaser=teaser,
            place_ref=place_id,
            answer_options=chips,
        )
    except Exception:
        logger.exception("community_join_enrichment_failed place=%s", place_id)
