"""Google Places suggestions for the tip-share flow — real nearby spots a neighbor
might recommend (parks, restaurants, clinics), searched around the block centroid.

Reuses GOOGLE_MAPS_API_KEY (same key as the event geocoder) and the zip_centroids
table + dev block fallback. Best-effort: returns [] on any failure or missing key,
so the flow always degrades gracefully to free-type + AI option chips.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.auth import service_client
from app.event_location import (
    _BLOCK_FALLBACK,
    _normalize_zip5,
    _zip_centroid,
    resolve_event_location,
)


def _centroid(
    zip_code: str | None, block_id: str | None, user_id: str | None = None
) -> tuple[float, float] | None:
    """Map center to bias the search. Tries (1) a passed ZIP, (2) a dev block, then
    (3) the user's home location — the same resolution events use (home_zip → centroid,
    defaulting to the Lake Nona pilot area), so it works for real blocks too."""
    if block_id and block_id in _BLOCK_FALLBACK:
        return _BLOCK_FALLBACK[block_id]
    try:
        z = _zip_centroid(service_client(), _normalize_zip5(zip_code))
        if z:
            return z
    except Exception:  # noqa: BLE001
        pass
    if user_id:
        try:
            lat, lng, _ = resolve_event_location(user_id, None)
            return (lat, lng)
        except Exception:  # noqa: BLE001
            return None
    return None


# Places API (New) — the legacy Text Search isn't enabled on newer GCP projects.
_PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


def _places_search_text(
    *, query: str, zip_code: str | None, block_id: str | None, user_id: str | None,
    field_mask: str, limit: int, radius: float,
) -> list[dict[str, Any]]:
    """One call to Places API (New) searchText, biased to the block centroid. Returns
    the raw `places` list, or [] on any failure / missing key."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    q = str(query or "").strip()
    if not api_key or len(q) < 2:
        return []
    loc = _centroid(zip_code, block_id, user_id)
    if not loc:
        # No block centroid → do NOT run an unbiased search. Places would return results
        # near the SERVER's location (e.g. the wrong country), which is worse than nothing.
        return []
    body: dict[str, Any] = {
        "textQuery": q,
        "maxResultCount": max(1, min(limit, 20)),
        "locationBias": {
            "circle": {"center": {"latitude": loc[0], "longitude": loc[1]}, "radius": radius}
        },
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(
                _PLACES_SEARCH_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": field_mask,
                },
                json=body,
            )
        data = res.json()
        return data.get("places") or [] if isinstance(data, dict) else []
    except Exception:  # noqa: BLE001 - best-effort
        return []


def _display_name(place: dict[str, Any]) -> str:
    return str(((place or {}).get("displayName") or {}).get("text") or "").strip()


def nearby_place_suggestions(
    *, query: str, zip_code: str | None = None, block_id: str | None = None,
    user_id: str | None = None, limit: int = 4,
) -> list[str]:
    """Names of nearby places matching `query` (e.g. "pediatric dentist", "park"),
    around the block/ZIP centroid. [] if no key, no location, or no results."""
    places = _places_search_text(
        query=query, zip_code=zip_code, block_id=block_id, user_id=user_id,
        field_mask="places.displayName", limit=limit, radius=8000.0,
    )
    names: list[str] = []
    for p in places:
        name = _display_name(p)
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return names


def search_places(
    *, query: str, zip_code: str | None = None, block_id: str | None = None,
    user_id: str | None = None, limit: int = 6,
) -> list[dict[str, Any]]:
    """Free-text place search (Google Places API New), biased to the block. Returns
    [{name, address, place_id, lat, lng}], or [] with no key / no results — the exact
    place the host picks, so publish stores a precise, navigable pin."""
    places = _places_search_text(
        query=query, zip_code=zip_code, block_id=block_id, user_id=user_id,
        field_mask="places.displayName,places.formattedAddress,places.id,places.location",
        limit=limit, radius=16000.0,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in places:
        name = _display_name(p)
        if not name or name in seen:
            continue
        seen.add(name)
        loc = (p or {}).get("location") or {}
        out.append({
            "name": name,
            "address": str((p or {}).get("formattedAddress") or "").strip(),
            "place_id": str((p or {}).get("id") or "").strip(),
            "lat": loc.get("latitude"),
            "lng": loc.get("longitude"),
        })
        if len(out) >= limit:
            break
    return out
