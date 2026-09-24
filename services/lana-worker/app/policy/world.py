"""world_state(user_id) — one snapshot of everything the policy needs to know
about a user's situation, composed from stores that already exist.

Nothing here is computed fresh: users row, zip_unlock snapshot,
circle_affiliations, relationship tiers. Every sub-read is best-effort — a
failed read degrades to an empty field, never a failed turn.

The `states` list is the capability-grounding vocabulary: capability_index
.required_state rows are offered only when required_state ⊆ states
(engineering doc §C.3, "unlock gates consumption, never creation").
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client
from app.zip_unlock import gate_mode

logger = logging.getLogger(__name__)

_USER_FIELDS = (
    "nickname, full_name, home_zip, home_block_id, locale, kids_count, "
    "phone_verified_at, email_verified_at, role, grammatical_gender"
)
# Pre-20260909 environments miss role/grammatical_gender; retry without them.
_USER_FIELDS_LEGACY = (
    "nickname, full_name, home_zip, home_block_id, locale, kids_count, "
    "phone_verified_at, email_verified_at"
)


def _user_row(user_id: str) -> dict[str, Any]:
    sb = service_client()
    for fields in (_USER_FIELDS, _USER_FIELDS_LEGACY):
        try:
            res = sb.table("users").select(fields).eq("id", user_id).limit(1).execute()
            row = (res.data or [None])[0]
            if isinstance(row, dict):
                return row
            return {}
        except Exception:
            continue
    logger.warning("world_state_user_row_failed user=%s", user_id)
    return {}


def _zip_snapshot(home_zip: str | None) -> dict[str, Any]:
    if not home_zip:
        return {}
    try:
        from app.zip_unlock import _unlock_snapshot

        return _unlock_snapshot(home_zip) or {}
    except Exception:
        logger.exception("world_state_zip_failed zip=%s", home_zip)
        return {}


_CIRCLE_FIELDS = "circle_key, circle_type, grounded, status, detail, place_ref, noun"
# Pre-20260906 environments miss place_ref; retry without it rather than
# degrade the whole circles list to empty.
_CIRCLE_FIELDS_LEGACY = "circle_key, circle_type, grounded, status, detail, noun"


def _circles(user_id: str) -> list[dict[str, Any]]:
    sb = service_client()
    for fields in (_CIRCLE_FIELDS, _CIRCLE_FIELDS_LEGACY):
        try:
            res = (
                sb.table("circle_affiliations")
                .select(fields)
                .eq("user_id", user_id)
                .is_("dismissed_at", "null")
                .order("created_at", desc=True)
                .limit(8)
                .execute()
            )
            return [r for r in (res.data or []) if isinstance(r, dict)]
        except Exception:
            continue
    logger.warning("world_state_circles_failed user=%s", user_id)
    return []


def _place_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    """place_ref -> place name, one batched read. The policy needs the real name
    to offer something concrete ("a get-together for your squash group at Life
    Time"); without it an offer can only speak vaguely."""
    ids = sorted({str(r.get("place_ref")) for r in rows if r.get("place_ref")})
    if not ids:
        return {}
    try:
        res = service_client().table("places").select("id, name").in_("id", ids).execute()
        return {
            str(p["id"]): str(p.get("name") or "")
            for p in (res.data or [])
            if isinstance(p, dict) and p.get("id")
        }
    except Exception:
        logger.exception("world_state_place_names_failed")
        return {}


def world_state(user_id: str) -> dict[str, Any]:
    """The policy's read of the user's world. Shape:

    {user: {...}, area: {state, count, threshold},
     circles: [{key, type, grounded, confirmed, place}],
     states: ["verified", "zip_warming", "zip_open", ...]}

    `place` is the pinned place's real name (None until grounded) — the policy
    needs it to offer something concrete rather than gesture at "somewhere".

    Per-peer relationship tiers stay out on purpose — they're per-pair lookups
    (get_relationship_tiers_for_user needs peer ids) that only matter once the
    policy names specific people, which v1 hands off to the discovery engines.
    """
    user = _user_row(user_id)
    area = _zip_snapshot(str(user.get("home_zip") or "") or None)
    circles = _circles(user_id)
    place_names = _place_names(circles)

    states: list[str] = []
    if user.get("phone_verified_at") or user.get("email_verified_at"):
        states.append("verified")
    if user.get("home_zip"):
        states.append("has_home_zip")

    # Three-state area model — LANA_CIRCLES_ZIP_MASTER_v1 §D.2.
    #
    # `warming` (1 <= verified_active_count < threshold) is a real state that
    # returns thin-but-real results; `open` additionally promises reliable
    # same-block matches. Emitting only `zip_open` collapsed the two, so a
    # warming area behaved exactly like an empty one and every people-question
    # fell through to the host pitch.
    #
    # `open` implies `warming`: both tokens are emitted at open so that any
    # capability gated on `zip_warming` stays satisfied once the area opens.
    _area_state = str(area.get("state") or "")
    if _area_state in ("warming", "open"):
        states.append("zip_warming")
    if _area_state == "open":
        states.append("zip_open")

    if any(
        str(c.get("status") or "") == "confirmed" for c in circles
    ):
        states.append("has_circle")

    return {
        "user": {
            "nickname": user.get("nickname"),
            "locale": user.get("locale"),
            "role": user.get("role"),
            "grammatical_gender": user.get("grammatical_gender"),
            "kids_count": user.get("kids_count"),
            "verified": "verified" in states,
        },
        # Area state is withheld when the unlock gate is off. The gate is the ONE
        # switch that decides whether area status may shape a turn, and the policy
        # prompt reads a non-open state as "you can't act yet — pivot to hosting"
        # (prime directive). Leaving the state in with the gate off let decide_turn
        # keep authoring "your area is still coming alive" after every mechanical
        # block had already been removed.
        "area": (
            {
                "state": area.get("state"),
                "count": area.get("count"),
                "threshold": area.get("threshold"),
            }
            if gate_mode() != "off"
            else {"state": None, "count": None, "threshold": None}
        ),
        "circles": [
            {
                "key": c.get("circle_key"),
                "type": c.get("circle_type"),
                # What this community IS ("gym", "library regulars") — a bare-category
                # key alone can't tell a library from a gaming zone (see same_community).
                "noun": c.get("noun"),
                "grounded": bool(c.get("grounded")),
                "confirmed": str(c.get("status") or "") == "confirmed",
                "place": place_names.get(str(c.get("place_ref") or "")) or None,
            }
            for c in circles
        ],
        "states": states,
    }


def capabilities_available(world: dict[str, Any]) -> list[dict[str, Any]]:
    """capability_index rows whose required_state is satisfied by the user's
    current states (engineering doc §C.3 — the '<@' containment check, done
    worker-side so it works with or without a helper RPC). Sorted by
    surface_priority as a WEAK tiebreak only — the policy may ignore it."""
    states = set(world.get("states") or [])
    try:
        res = (
            service_client()
            .table("capability_index")
            .select("capability_id, capability_name, description, required_state, surface_priority")
            .eq("is_active", True)
            .execute()
        )
        rows = [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("capabilities_available_failed")
        return []
    out = [
        r for r in rows
        if set(r.get("required_state") or []) <= states
    ]
    out.sort(key=lambda r: -(int(r.get("surface_priority") or 0)))
    return out
