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


def _circles(user_id: str) -> list[dict[str, Any]]:
    try:
        res = (
            service_client()
            .table("circle_affiliations")
            .select("circle_key, circle_type, grounded, status, detail")
            .eq("user_id", user_id)
            .is_("dismissed_at", "null")
            .order("created_at", desc=True)
            .limit(8)
            .execute()
        )
        return [r for r in (res.data or []) if isinstance(r, dict)]
    except Exception:
        logger.exception("world_state_circles_failed user=%s", user_id)
        return []


def world_state(user_id: str) -> dict[str, Any]:
    """The policy's read of the user's world. Shape:

    {user: {...}, area: {state, count, threshold}, circles: [...],
     states: ["verified", "zip_open", ...]}

    Per-peer relationship tiers stay out on purpose — they're per-pair lookups
    (get_relationship_tiers_for_user needs peer ids) that only matter once the
    policy names specific people, which v1 hands off to the discovery engines.
    """
    user = _user_row(user_id)
    area = _zip_snapshot(str(user.get("home_zip") or "") or None)
    circles = _circles(user_id)

    states: list[str] = []
    if user.get("phone_verified_at") or user.get("email_verified_at"):
        states.append("verified")
    if user.get("home_zip"):
        states.append("has_home_zip")
    if str(area.get("state") or "") == "open":
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
        "area": {
            "state": area.get("state"),
            "count": area.get("count"),
            "threshold": area.get("threshold"),
        },
        "circles": [
            {
                "key": c.get("circle_key"),
                "type": c.get("circle_type"),
                "grounded": bool(c.get("grounded")),
                "confirmed": str(c.get("status") or "") == "confirmed",
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
