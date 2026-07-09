import os
from typing import Any

import httpx

from app.auth import service_client

# Dev block centroids (matches phase2 seed) when geography not returned via REST.
_BLOCK_FALLBACK: dict[str, tuple[float, float]] = {
    "8a2a1072b59ffff": (28.3647, -81.2568),
    "8a2a1072b5affff": (28.3689, -81.2621),
}


# Placeholder ZIPs that pass a bare 5-digit format check but aren't real — QA saw
# 99999 accepted in a production flow. Rejected everywhere a ZIP enters.
_BOGUS_ZIP5 = {"00000", "99999"}


def _normalize_zip5(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = "".join(c for c in str(raw) if c.isdigit())
    if len(digits) < 5:
        return None
    zip5 = digits[:5]  # keep the ZIP5 of a ZIP+4
    if zip5 in _BOGUS_ZIP5:
        return None
    return zip5


def is_valid_zip5(raw: str | None) -> bool:
    """Plausible 5-digit US ZIP — False for non-5-digit strings and the obviously
    invalid placeholders (00000/99999)."""
    return _normalize_zip5(raw) is not None


def geocode_zip(zip5: str) -> tuple[float, float, str] | None:
    """Geocode a US ZIP via Google → (lat, lng, city). None when the ZIP can't be placed
    (no API key, or genuinely not a real ZIP). Used to auto-create a block for a ZIP we
    don't cover yet, so signup is never blocked."""
    z = _normalize_zip5(zip5)
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not z or not api_key:
        return None
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                # components filter is far more precise than a free-text address for a ZIP.
                params={"components": f"postal_code:{z}|country:US", "key": api_key},
            )
        data = res.json()
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        loc = top.get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            return None
        city = ""
        for comp in top.get("address_components", []):
            types = comp.get("types", [])
            if "locality" in types or "postal_town" in types:
                city = str(comp.get("long_name") or "").strip()
                break
        if not city:
            city = str(top.get("formatted_address") or "").split(",")[0].strip()
        return float(lat), float(lng), city
    except Exception:
        return None


def _geocode_venue(venue_name: str, city_hint: str) -> tuple[float, float] | None:
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        return None
    query = f"{venue_name.strip()}, {city_hint}"
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": query, "key": api_key},
            )
        data = res.json()
        results = data.get("results") or []
        if not results:
            return None
        loc = results[0].get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except Exception:
        return None


def _zip_centroid(sb: Any, zip5: str | None) -> tuple[float, float] | None:
    if not zip5:
        return None
    row = (
        sb.table("zip_centroids")
        .select("lat, lng")
        .eq("zip5", zip5)
        .limit(1)
        .execute()
    )
    if not row.data:
        return None
    z = row.data[0]
    lat, lng = z.get("lat"), z.get("lng")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def resolve_event_location(
    user_id: str,
    venue_name: str | None,
    *,
    city_hint: str = "Lake Nona, FL",
) -> tuple[float, float, str | None]:
    """Return (lat, lng, block_id). Never stores street — coarse point only."""
    sb = service_client()
    user_row = (
        sb.table("users")
        .select("home_block_id, home_zip")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    user = user_row.data[0] if user_row.data else {}
    block_id = user.get("home_block_id")
    home_zip = _normalize_zip5(user.get("home_zip"))

    if venue_name and venue_name.strip():
        coords = _geocode_venue(venue_name.strip(), city_hint)
        if coords:
            return coords[0], coords[1], block_id

    zip_coords = _zip_centroid(sb, home_zip)
    if zip_coords:
        return zip_coords[0], zip_coords[1], block_id

    if block_id and block_id in _BLOCK_FALLBACK:
        lat, lng = _BLOCK_FALLBACK[block_id]
        return lat, lng, block_id

    return 28.3647, -81.2568, block_id
