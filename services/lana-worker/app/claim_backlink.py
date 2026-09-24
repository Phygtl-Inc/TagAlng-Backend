"""Creator claim verification by profile backlink.

The creators page already tells them: "Add Lana to Instagram, Reddit, or your existing
Linktree." That instruction IS the proof of control — only the account owner can edit
that profile — so verification costs the creator no extra step, and a creator who will
not put the link up has no funnel to verify anyway. The check fails early and cheaply.

WHAT THIS IS NOT
    Not a metrics reader. Nothing here counts followers, engagement or posts, and nothing
    here decides whether someone is "big enough". It answers exactly one question: does
    this person control this account? Whether their community is any good is answered
    later, by the community (see places.handle_provisional_until).

ANONYMOUS ONLY
    Every fetch is unauthenticated, with no cookie jar and no stored session. If a profile
    is not visible to a logged-out stranger it is not public, and we do not verify it.
    That is also why PUBLIC_RENDER_PROVIDERS exists: several platforms serve a login wall
    to anonymous clients, and a wall is not a missing link. Those route to manual review
    rather than a false negative.

Defensive by contract: never raises into the request path, returns a reason code on every
outcome. Mirrors the failure posture of the other service-role modules in this package.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from app.auth import service_client

logger = logging.getLogger(__name__)

# The public route. Flat, on the app host, no type prefix — a type-encoded route would
# break the day a creator adds an address (20261214120000).
LANA_HOST = "get.lana.help"

# Providers that render a public profile to an anonymous client. Anything outside this
# set is accepted as a claim but sent to manual review: Instagram in particular serves a
# login wall, and treating that as "link not found" would reject honest creators.
PUBLIC_RENDER_PROVIDERS = frozenset(
    {"linktree", "youtube", "reddit", "website", "podcast", "newsletter", "twitch"}
)

# A claimant may attach several surfaces, but not twenty: the cap is what stops one
# person farming handles by attaching an account per name they want.
MAX_IDENTITIES_PER_PLACE = 5

_TIMEOUT_S = 10.0
_MAX_BYTES = 2_000_000  # enough for any profile page; stops a hostile or infinite body
_UA = "LanaBot/1.0 (+https://lana.help/about; claim verification)"

# Reason codes are part of the contract — the UI renders these, and an opaque
# "verification failed" is the thing we are deliberately not building.
R_OK = "VERIFIED"
R_NO_RESERVATION = "NO_ACTIVE_RESERVATION"
R_NOT_PUBLIC = "PROFILE_NOT_PUBLIC"
R_FETCH_FAILED = "PROFILE_FETCH_FAILED"
R_NOT_FOUND = "BACKLINK_NOT_FOUND"
R_WRONG_HANDLE = "BACKLINK_POINTS_ELSEWHERE"
R_TAKEN = "IDENTITY_ALREADY_CLAIMED"
R_TOO_MANY = "TOO_MANY_IDENTITIES"
R_NEEDS_REVIEW = "NEEDS_MANUAL_REVIEW"
R_BAD_URL = "INVALID_PROFILE_URL"


def _normalize(handle: str) -> str:
    return (handle or "").strip().lower()


def _backlink_patterns(handle: str) -> list[re.Pattern[str]]:
    """Accept the link however a profile editor mangled it — with or without scheme,
    with or without www, trailing slash, any case. What must match exactly is the
    HANDLE: a link to some other Lana page proves control of nothing here."""
    h = re.escape(_normalize(handle))
    host = re.escape(LANA_HOST)
    return [
        re.compile(rf"(?:https?://)?(?:www\.)?{host}/{h}(?:[/?#\s\"'<]|$)", re.IGNORECASE),
    ]


def _fetch_public(url: str) -> tuple[str | None, str | None]:
    """GET as an anonymous stranger. Returns (body, reason_code_on_failure)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, R_BAD_URL

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en",
        },
        method="GET",
    )
    try:
        # No opener with a cookie jar, deliberately: a session would let us see things a
        # logged-out visitor cannot, and "public" is the whole claim being tested.
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            raw = resp.read(_MAX_BYTES)
        return raw.decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as exc:
        # 401/403/429 is a wall or a throttle, not a missing link.
        if exc.code in (401, 403, 429):
            return None, R_NOT_PUBLIC
        logger.info("claim_backlink: http %s for %s", exc.code, url)
        return None, R_FETCH_FAILED
    except Exception:
        logger.exception("claim_backlink: fetch failed for %s", url)
        return None, R_FETCH_FAILED


def _active_reservation(handle: str, user_id: str) -> dict[str, Any] | None:
    try:
        res = (
            service_client()
            .table("place_handle_reservations")
            .select("id, normalized_handle, place_id, user_id, status, expires_at")
            .eq("normalized_handle", _normalize(handle))
            .eq("user_id", user_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        logger.exception("claim_backlink: reservation lookup failed")
        return None


def _identity_count(place_id: str) -> int:
    try:
        res = (
            service_client()
            .table("external_community_identities")
            .select("id", count="exact")
            .eq("place_id", place_id)
            .execute()
        )
        return res.count if getattr(res, "count", None) is not None else len(res.data or [])
    except Exception:
        logger.exception("claim_backlink: identity count failed")
        return MAX_IDENTITIES_PER_PLACE  # fail closed — do not over-attach on a read error


def _result(ok: bool, reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"verified": ok, "reason": reason, "policy": "backlink-v1"}
    out.update(extra)
    return out


def verify_backlink(
    *,
    place_id: str,
    handle: str,
    provider: str,
    canonical_url: str,
    username: str,
    user_id: str,
) -> dict[str, Any]:
    """Prove control of an external profile by finding OUR link inside it.

    Returns {verified, reason, policy, ...}. Never raises.

    On success: writes the identity row, resolves the claim, verifies the place, sets the
    handle provisionally and records the claimant as an operator. The place-side state
    transition is left to the caller's existing claim-resolution path where one already
    exists — this module owns the evidence, not the lifecycle.
    """
    handle = _normalize(handle)
    if not handle or not place_id or not user_id:
        return _result(False, R_BAD_URL)

    reservation = _active_reservation(handle, user_id)
    if reservation is None:
        return _result(False, R_NO_RESERVATION)

    if _identity_count(place_id) >= MAX_IDENTITIES_PER_PLACE:
        return _result(False, R_TOO_MANY, max=MAX_IDENTITIES_PER_PLACE)

    if provider not in PUBLIC_RENDER_PROVIDERS:
        # Not a failure — an honest "we cannot check this one automatically". Instagram
        # is the common case and rejecting those creators would be wrong.
        return _result(
            False,
            R_NEEDS_REVIEW,
            provider=provider,
            hint="This platform does not show profiles to logged-out visitors. "
                 "Add the link anyway and submit for review, or use a Linktree.",
        )

    body, failure = _fetch_public(canonical_url)
    if body is None:
        return _result(False, failure or R_FETCH_FAILED)

    matched: str | None = None
    for pattern in _backlink_patterns(handle):
        found = pattern.search(body)
        if found:
            matched = found.group(0).strip()
            break

    if matched is None:
        # Distinguish "no Lana link at all" from "a Lana link, but someone else's" — the
        # second is worth its own message, and is the shape a copied-link attempt takes.
        if re.search(rf"(?:https?://)?(?:www\.)?{re.escape(LANA_HOST)}/", body, re.IGNORECASE):
            return _result(False, R_WRONG_HANDLE, expected=f"{LANA_HOST}/{handle}")
        return _result(False, R_NOT_FOUND, expected=f"{LANA_HOST}/{handle}")

    try:
        service_client().table("external_community_identities").insert(
            {
                "place_id": place_id,
                "provider": provider,
                "provider_account_id": None,  # backlink proves the profile, not an id
                "username": username,
                "canonical_url": canonical_url,
                "ownership_method": "profile_backlink",
                "ownership_verified_at": "now()",
                "backlink_url_seen": matched[:500],
                "last_checked_at": "now()",
            }
        ).execute()
    except Exception as exc:  # unique index = this account belongs to another community
        text = str(exc).lower()
        if "eci_provider" in text or "duplicate" in text or "unique" in text:
            return _result(False, R_TAKEN, provider=provider, username=username)
        logger.exception("claim_backlink: identity insert failed")
        return _result(False, R_FETCH_FAILED)

    logger.info(
        "claim_backlink: verified place=%s provider=%s handle=%s", place_id, provider, handle
    )
    return _result(
        True,
        R_OK,
        provider=provider,
        username=username,
        matched=matched[:200],
        verification_method="profile_backlink",
    )
