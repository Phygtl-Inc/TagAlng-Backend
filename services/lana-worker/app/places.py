"""Google Places suggestions for the tip-share flow — real nearby spots a neighbor
might recommend (parks, restaurants, clinics), searched around the block centroid.

Reuses GOOGLE_MAPS_API_KEY (same key as the event geocoder) and the zip_centroids
table + dev block fallback. Best-effort: returns [] on any failure or missing key,
so the flow always degrades gracefully to free-type + AI option chips.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

_log = logging.getLogger(__name__)

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
    included_type: str | None = None, strict_type: bool = False,
    min_rating: float | None = None, price_levels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One call to Places API (New) searchText, biased to the block centroid. Returns
    the raw `places` list, or [] on any failure / missing key.

    Optional server-side filters (all Places API (New) request params): `included_type`
    (+ `strict_type` to hard-restrict), `min_rating`, `price_levels` — narrow at the API
    instead of over-fetching and filtering here."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    q = str(query or "").strip()
    if not api_key or len(q) < 2:
        _log.info(
            "places_search.skip reason=%s query=%r",
            "no_api_key" if not api_key else "query_too_short", q,
        )
        return []
    loc = _centroid(zip_code, block_id, user_id)
    if not loc:
        # No block centroid → do NOT run an unbiased search. Places would return results
        # near the SERVER's location (e.g. the wrong country), which is worse than nothing.
        _log.info(
            "places_search.skip reason=no_centroid query=%r zip=%r block=%r",
            q, zip_code, block_id,
        )
        return []
    body: dict[str, Any] = {
        "textQuery": q,
        "maxResultCount": max(1, min(limit, 20)),
        "locationBias": {
            "circle": {"center": {"latitude": loc[0], "longitude": loc[1]}, "radius": radius}
        },
    }
    if included_type:
        body["includedType"] = included_type
        if strict_type:
            body["strictTypeFiltering"] = True
    if min_rating is not None:
        body["minRating"] = float(min_rating)
    if price_levels:
        body["priceLevels"] = list(price_levels)
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
        places = data.get("places") or [] if isinstance(data, dict) else []
        _log.info(
            "places_search.ok query=%r status=%s results=%d",
            q, getattr(res, "status_code", "?"), len(places),
        )
        if not places and isinstance(data, dict) and data.get("error"):
            _log.info("places_search.api_error query=%r error=%r", q, data.get("error"))
        return places
    except Exception:  # noqa: BLE001 - best-effort
        _log.exception("places_search.request_failed query=%r", q)
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
    included_type: str | None = None, strict_type: bool = False,
    min_rating: float | None = None, price_levels: list[str] | None = None,
    attr_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Free-text place search (Google Places API New), biased to the block. Returns
    [{name, address, place_id, lat, lng, rating, attrs}], or [] with no key / no results —
    the exact place the host picks, so publish stores a precise, navigable pin.

    `included_type`/`strict_type`/`min_rating`/`price_levels` are server-side narrowers.
    `attr_fields` (e.g. ['goodForChildren','servesVegetarianFood']) are per-place booleans
    added to the FieldMask and returned under `attrs` so the caller can VERIFY a claim
    ('kid-friendly') instead of asserting it. Requesting attrs bills at the pricier
    Enterprise+Atmosphere tier, so pass only what will be checked."""
    from app.place_types import valid_attrs

    attrs = valid_attrs(attr_fields)
    fields = [
        "places.displayName", "places.formattedAddress", "places.id", "places.location",
        # rating/count are Pro-tier (cheap) and useful for ordering — always request them.
        "places.rating", "places.userRatingCount",
    ]
    fields.extend(f"places.{a}" for a in attrs)
    places = _places_search_text(
        query=query, zip_code=zip_code, block_id=block_id, user_id=user_id,
        field_mask=",".join(fields), limit=limit, radius=16000.0,
        included_type=included_type, strict_type=strict_type,
        min_rating=min_rating, price_levels=price_levels,
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
            "rating": (p or {}).get("rating"),
            "user_rating_count": (p or {}).get("userRatingCount"),
            # Only the requested attribute booleans Google returned — used to VERIFY a claim.
            "attrs": {a: (p or {}).get(a) for a in attrs if a in (p or {})},
        })
        if len(out) >= limit:
            break
    return out


# Classic Geocoding API — reverse geocoding has no Places API (New) equivalent.
_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


def reverse_geocode(lat: float, lng: float) -> dict[str, Any] | None:
    """lat/lng → {name, address, place_id, lat, lng} for a bare device pin ("Use my
    current location", issue #42). The returned lat/lng are the CALLER's coordinates,
    not the geocode result's — the device pin stays authoritative; this only names it.
    Best-effort: None on any failure or missing key (a nameless pin is still correct)."""
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        with httpx.Client(timeout=6.0) as client:
            res = client.get(
                _GEOCODE_URL,
                params={
                    "latlng": f"{float(lat)},{float(lng)}",
                    "key": api_key,
                    # Prefer a human-scale label over a plus-code or a whole city.
                    "result_type": "street_address|premise|point_of_interest|neighborhood",
                },
            )
        data = res.json() if res.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return None
    results = data.get("results") or []
    if not results:
        return None
    top = results[0] or {}
    address = str(top.get("formatted_address") or "").strip()
    if not address:
        return None
    return {
        "name": address.split(",")[0].strip()[:120],
        "address": address[:300],
        "place_id": str(top.get("place_id") or "").strip() or None,
        "lat": float(lat),
        "lng": float(lng),
    }
