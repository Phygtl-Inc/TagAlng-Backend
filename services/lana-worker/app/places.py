"""Google Places suggestions for the tip-share flow — real nearby spots a neighbor
might recommend (parks, restaurants, clinics), searched around the block centroid.

Reuses GOOGLE_MAPS_API_KEY (same key as the event geocoder) and the zip_centroids
table + dev block fallback. Best-effort: returns [] on any failure or missing key,
so the flow always degrades gracefully to free-type + AI option chips.
"""

from __future__ import annotations

import os

import httpx

from app.auth import service_client
from app.event_location import _BLOCK_FALLBACK, _normalize_zip5, _zip_centroid


def _centroid(zip_code: str | None, block_id: str | None) -> tuple[float, float] | None:
    if block_id and block_id in _BLOCK_FALLBACK:
        return _BLOCK_FALLBACK[block_id]
    try:
        return _zip_centroid(service_client(), _normalize_zip5(zip_code))
    except Exception:  # noqa: BLE001
        return None


def nearby_place_suggestions(
    *, query: str, zip_code: str | None = None, block_id: str | None = None, limit: int = 4
) -> list[str]:
    """Names of nearby places matching `query` (e.g. "pediatric dentist", "park"),
    around the block/ZIP centroid. [] if no key, no location, or no results."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    q = str(query or "").strip()
    if not api_key or not q:
        return []
    loc = _centroid(zip_code, block_id)
    if not loc:
        return []
    lat, lng = loc
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": q,
                    "location": f"{lat},{lng}",
                    "radius": 8000,  # ~5 miles around the block
                    "key": api_key,
                },
            )
        results = res.json().get("results") or []
    except Exception:  # noqa: BLE001 - suggestions are best-effort
        return []

    names: list[str] = []
    for r in results:
        name = str((r or {}).get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names
