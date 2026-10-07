"""A place someone asked to search that is not where they are — "language events in San
Jose", "anything this weekend in Austin?" (travel; Tommaso, 2026-10-06).

The AI reads the place out of the message (discovery_slots `search_place`); this turns it
into the ZIP the browse lane already knows how to search. A city is geocoded to its centre
and that point to its postal code, so the existing ZIP → area path does the rest, with its
out-of-coverage handling intact. US only, like the rest of the pilot: a place outside the
US comes back with no ZIP, and the caller says so honestly rather than searching home.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _postal_code_at(lat: float, lng: float) -> tuple[str | None, str | None]:
    """(ZIP5, country code) for a point, from Google reverse geocoding."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return None, None
    try:
        with httpx.Client(timeout=8.0) as client:
            res = client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"latlng": f"{lat},{lng}", "result_type": "postal_code", "key": api_key},
            )
        results = (res.json() or {}).get("results") or []
    except Exception:  # noqa: BLE001
        logger.exception("search_place_reverse_failed")
        return None, None
    for r in results[:1]:
        zip5 = country = None
        for c in r.get("address_components") or []:
            types = c.get("types") or []
            if "postal_code" in types:
                zip5 = str(c.get("short_name") or "")[:5]
            if "country" in types:
                country = str(c.get("short_name") or "")
        if zip5 and zip5.isdigit() and len(zip5) == 5:
            return zip5, country
        return None, country
    return None, None


def resolve_search_place(text: str) -> dict[str, Any] | None:
    """{"label", "zip5", "lat", "lng"} for a named town or city; zip5 is None outside the US (or when it
    cannot be placed to a postal code). None when the text is not a place at all."""
    from app.community_hq import geocode_city

    got = geocode_city(text)
    if not got:
        return None
    zip5, country = _postal_code_at(got["lat"], got["lng"])
    return {
        "label": str(got["city"]),
        "zip5": zip5 if country in (None, "US") else None,
        "lat": got["lat"],
        "lng": got["lng"],
    }
