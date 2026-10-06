"""Where a community is RUN FROM — its headquarters (HQ).

A community with no location (podcasters, a book club, a creator's audience) still comes
from somewhere. The HQ is a city: a label on its card ("Run from Orlando, FL") and a pin on
its map. It is never a location and never a distance — places.hq_* is display-only by rule
(20261214120000), so a global community never shows up as a neighbour's gym.

Two writers share this module: the create-a-community chat (asks before publishing) and
POST /lana/circles/hq (the community's edit screen). Who may write is SQL's call
(set_community_hq_for, 20270109120000).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Result types that describe a town or region, not a street address or a business. A
# typed "Orlando" geocodes to a locality; "123 Main St" to a street_address, which is not
# where a community is run from, and asking again beats pinning someone's front door.
_AREA_TYPES = frozenset(
    {
        "locality",
        "postal_town",
        "sublocality",
        "neighborhood",
        "administrative_area_level_1",
        "administrative_area_level_2",
        "administrative_area_level_3",
        "postal_code",
        "country",
    }
)


def _label(components: list[dict[str, Any]], fallback: str) -> str:
    """ "Orlando, FL" / "Lisbon, Portugal": town plus region (US) or country."""
    by_type: dict[str, dict[str, Any]] = {}
    for c in components or []:
        for t in c.get("types") or []:
            by_type.setdefault(t, c)
    town = (
        by_type.get("locality")
        or by_type.get("postal_town")
        or by_type.get("sublocality")
        or by_type.get("neighborhood")
        or by_type.get("administrative_area_level_2")
        or by_type.get("administrative_area_level_1")
    )
    country = by_type.get("country") or {}
    region = by_type.get("administrative_area_level_1") or {}
    if not town:
        return fallback
    name = str(town.get("long_name") or "").strip()
    if country.get("short_name") == "US" and region and region is not town:
        return f"{name}, {region.get('short_name')}"
    if country and country is not town:
        return f"{name}, {country.get('long_name')}"
    return name or fallback


def geocode_city(text: str) -> dict[str, Any] | None:
    """{"city", "lat", "lng"} for a typed town/region, or None when it is not one."""
    q = str(text or "").strip()
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not q or len(q) > 120 or not api_key:
        return None
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": q, "key": api_key},
            )
        results = (res.json() or {}).get("results") or []
    except Exception:  # noqa: BLE001
        logger.exception("hq_geocode_failed q=%r", q[:60])
        return None
    for r in results[:3]:
        if not _AREA_TYPES.intersection(r.get("types") or []):
            continue
        loc = (r.get("geometry") or {}).get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            continue
        return {
            "city": _label(r.get("address_components") or [], q)[:120],
            "lat": float(lat),
            "lng": float(lng),
        }
    return None


def save_community_hq(user_id: str, place_id: str, city_text: str) -> dict[str, Any]:
    """Geocode `city_text` and record it as `place_id`'s HQ, as `user_id`.

    Returns the database's verdict ({"status": "saved", "hqCity": …} or a refusal), or
    {"status": "not_found"} when the text is not a town we can place."""
    if not user_id or not place_id:
        return {"status": "sign_in_required" if not user_id else "place_not_found"}
    got = geocode_city(city_text)
    if not got:
        return {"status": "not_found"}
    return write_community_hq(user_id, place_id, got)


def write_community_hq(user_id: str, place_id: str, hq: dict[str, Any]) -> dict[str, Any]:
    """Record an already-geocoded HQ. {"status": "error"} on transport failure."""
    try:
        from app.auth import service_client

        res = (
            service_client()
            .rpc(
                "set_community_hq_for",
                {
                    "p_user_id": user_id,
                    "p_place_id": place_id,
                    "p_city": hq.get("city"),
                    "p_lat": hq.get("lat"),
                    "p_lng": hq.get("lng"),
                },
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("community_hq_write_failed place=%s", place_id)
        return {"status": "error"}
    return res.data if isinstance(res.data, dict) else {"status": "error"}
