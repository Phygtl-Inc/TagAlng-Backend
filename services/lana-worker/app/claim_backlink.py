"""Creator claim verification by profile backlink.

The creators page already tells them: "Add Lana to Instagram, Reddit, or your existing
Linktree." That instruction IS the proof of control — only the account owner can edit
that profile — so verification costs the creator no extra step.

THREE WAYS A CREATOR ACTUALLY DOES THIS, and the first version only served one:

  1. Link already up when they claim.        One fetch, done.
  2. "I'll add it in a minute."              Most people. The natural order is claim the
                                             handle, THEN go edit the bio — so the first
                                             check runs BEFORE the link exists. A one-shot
                                             check turns the common path into a dead end.
                                             Now: the claim stays pending, the creator can
                                             ask us to look again, and a sweep retries with
                                             backoff until the reservation lapses.
  3. Link lives one level down.               "Add it to your existing Linktree" is our own
                                             instruction, and it means the Instagram bio
                                             holds a Linktree URL and OUR link is inside
                                             THAT. Fetching the Instagram page alone finds
                                             nothing. Now: one hop, never two.

WHAT THIS IS NOT
    Not a metrics reader. Nothing here counts followers, engagement or posts, and nothing
    decides whether someone is "big enough". It answers one question: does this person
    control this account?

ANONYMOUS ONLY
    Every fetch is unauthenticated, with no cookie jar and no stored session. If a profile
    is not visible to a logged-out stranger it is not public, and we do not verify it.

Defensive by contract: never raises into the request path, returns a reason code on every
outcome.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from app.auth import service_client

logger = logging.getLogger(__name__)

# The public route. Flat, on the app host, no type prefix — a type-encoded route would
# break the day a creator adds an address (20261214120000).
LANA_HOST = "get.lana.help"

# Providers that render a public profile to an anonymous client. Anything outside this
# set is accepted as a claim but sent to manual review: Instagram serves a login wall,
# and treating that as "link not found" would reject honest creators.
PUBLIC_RENDER_PROVIDERS = frozenset(
    {"linktree", "youtube", "reddit", "website", "podcast", "newsletter", "twitch"}
)

# Hop targets we prefer. Not an allowlist — any public host is followable (see _safe_host)
# — but our own copy says "your existing Linktree", so these get looked at first and the
# budget is small.
AGGREGATOR_HOSTS = frozenset({
    "linktr.ee", "beacons.ai", "bio.link", "lnk.bio", "campsite.bio", "taplink.cc",
    "solo.to", "carrd.co", "komi.io", "withkoji.com", "allmylinks.com", "msha.ke",
    "flowcode.com", "shorby.com", "liinks.co", "tap.bio", "hoo.be", "pillar.io",
})

MAX_IDENTITIES_PER_PLACE = 5
MAX_HOPS = 1              # one level down. Never two — that is a crawler, not a check.
MAX_HOP_CANDIDATES = 3    # how many links from the first page we are willing to open
MAX_ATTEMPTS = 6          # then stop asking. A creator who never adds it is not a bug.

# Backoff between sweep attempts, by attempt number. Front-loaded because the common case
# is someone editing their bio right now; then it stretches out so we are not hammering
# somebody else's server for a link that is never coming.
_BACKOFF_MINUTES = [5, 30, 120, 720, 1440]

_TIMEOUT_S = 10.0
_MAX_BYTES = 2_000_000
_UA = "LanaBot/1.0 (+https://lana.help/about; claim verification)"

# Reason codes are part of the contract — the UI renders these, and an opaque
# "verification failed" is the thing we are deliberately not building.
R_OK = "VERIFIED"
R_PENDING = "PENDING_LINK"          # looked, not there yet, will look again
R_NO_RESERVATION = "NO_ACTIVE_RESERVATION"
R_NOT_PUBLIC = "PROFILE_NOT_PUBLIC"
R_FETCH_FAILED = "PROFILE_FETCH_FAILED"
R_NOT_FOUND = "BACKLINK_NOT_FOUND"
R_WRONG_HANDLE = "BACKLINK_POINTS_ELSEWHERE"
R_TAKEN = "IDENTITY_ALREADY_CLAIMED"
R_TOO_MANY = "TOO_MANY_IDENTITIES"
R_NEEDS_REVIEW = "NEEDS_MANUAL_REVIEW"
R_BAD_URL = "INVALID_PROFILE_URL"
R_GAVE_UP = "GAVE_UP_AFTER_RETRIES"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(handle: str) -> str:
    return (handle or "").strip().lower()


def _backlink_pattern(handle: str) -> re.Pattern[str]:
    """Accept the link however a profile editor mangled it — scheme optional, www
    optional, trailing slash, any case, tracking params. What must match exactly is the
    HANDLE: a link to some other Lana page proves control of nothing here."""
    h = re.escape(_normalize(handle))
    host = re.escape(LANA_HOST)
    return re.compile(rf"(?:https?://)?(?:www\.)?{host}/{h}(?:[/?#\s\"'<)\]]|$)", re.IGNORECASE)


def _any_lana_link(body: str) -> bool:
    return bool(re.search(rf"(?:https?://)?(?:www\.)?{re.escape(LANA_HOST)}/", body, re.I))


# ── fetching ────────────────────────────────────────────────────────────────

def _safe_host(url: str) -> bool:
    """SSRF guard. We follow a link found on somebody else's page, so the target is
    attacker-influenced: a bio containing http://169.254.169.254/ would otherwise make us
    fetch cloud metadata and put the response in a log. Public unicast only."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False  # hops are https-only; hop 0 may be http, see _fetch_public
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _fetch_public(url: str, *, is_hop: bool = False) -> tuple[str | None, str | None]:
    """GET as an anonymous stranger. Returns (body, reason_code_on_failure)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, R_BAD_URL
    if is_hop and not _safe_host(url):
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
        if exc.code in (401, 403, 429):
            return None, R_NOT_PUBLIC  # a wall or a throttle, not a missing link
        logger.info("claim_backlink: http %s for %s", exc.code, url)
        return None, R_FETCH_FAILED
    except Exception:
        logger.exception("claim_backlink: fetch failed for %s", url)
        return None, R_FETCH_FAILED


def _hop_candidates(body: str, base_url: str) -> list[str]:
    """Outbound links worth opening, aggregators first. Deduped, capped."""
    seen: list[str] = []
    for raw in re.findall(r'href=["\']([^"\']+)["\']', body, re.IGNORECASE):
        url = urljoin(base_url, raw.strip())
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        if parsed.hostname.lower().endswith(LANA_HOST):
            continue  # a Lana link would already have matched
        if url not in seen:
            seen.append(url)
    seen.sort(key=lambda u: 0 if (urlparse(u).hostname or "").lower().lstrip("www.")
              in AGGREGATOR_HOSTS else 1)
    return seen[:MAX_HOP_CANDIDATES]


def find_backlink(url: str, handle: str) -> tuple[str | None, str | None, str | None]:
    """Look for our link at `url`, then one level down. Returns (matched, where, reason)."""
    pattern = _backlink_pattern(handle)

    body, failure = _fetch_public(url)
    if body is None:
        return None, None, failure or R_FETCH_FAILED

    found = pattern.search(body)
    if found:
        return found.group(0).strip(), url, None

    # Case 3. Our own instruction says "add it to your existing Linktree", so the link is
    # routinely one level below the profile we were given. One hop, never two.
    if MAX_HOPS >= 1:
        for candidate in _hop_candidates(body, url):
            hop_body, _ = _fetch_public(candidate, is_hop=True)
            if hop_body is None:
                continue
            hop_found = pattern.search(hop_body)
            if hop_found:
                return hop_found.group(0).strip(), candidate, None
            if _any_lana_link(hop_body):
                return None, candidate, R_WRONG_HANDLE

    if _any_lana_link(body):
        return None, url, R_WRONG_HANDLE
    return None, url, R_NOT_FOUND


# ── persistence ─────────────────────────────────────────────────────────────

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
        return MAX_IDENTITIES_PER_PLACE  # fail closed


def _next_check_after(attempts: int) -> str | None:
    if attempts >= MAX_ATTEMPTS:
        return None  # stop looking; the creator can still trigger a manual re-check
    idx = min(attempts - 1, len(_BACKOFF_MINUTES) - 1)
    return (_now() + timedelta(minutes=_BACKOFF_MINUTES[max(idx, 0)])).isoformat()


def _result(ok: bool, reason: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"verified": ok, "reason": reason, "policy": "backlink-v2"}
    out.update(extra)
    return out


# ── entry points ────────────────────────────────────────────────────────────

def start_backlink_claim(
    *,
    place_id: str,
    handle: str,
    provider: str,
    canonical_url: str,
    username: str,
    user_id: str,
    claim_id: str | None = None,
) -> dict[str, Any]:
    """Begin (or re-run) a backlink claim. Never raises.

    Case 1 verifies immediately. Case 2 returns PENDING_LINK and leaves a row the sweep
    and the "check again" button both act on — the claim is not lost because the creator
    had not finished editing their bio yet.
    """
    handle = _normalize(handle)
    if not handle or not place_id or not user_id:
        return _result(False, R_BAD_URL)

    if _active_reservation(handle, user_id) is None:
        return _result(False, R_NO_RESERVATION)

    if provider not in PUBLIC_RENDER_PROVIDERS:
        return _result(
            False, R_NEEDS_REVIEW, provider=provider,
            hint="This platform does not show profiles to logged-out visitors. "
                 "Point us at your Linktree or website instead, or submit for review.",
        )

    existing = _existing_row(place_id, provider, username)
    if existing is None and _identity_count(place_id) >= MAX_IDENTITIES_PER_PLACE:
        return _result(False, R_TOO_MANY, max=MAX_IDENTITIES_PER_PLACE)

    if existing is None:
        existing = _insert_pending(
            place_id=place_id, provider=provider, username=username,
            canonical_url=canonical_url, claim_id=claim_id,
        )
        if existing is None:
            return _result(False, R_TAKEN, provider=provider, username=username)

    return _run_check(existing, handle)


def recheck_backlink(*, identity_id: str, handle: str) -> dict[str, Any]:
    """"I've added it — check again." Same path, no backoff wait."""
    try:
        res = (
            service_client()
            .table("external_community_identities")
            .select("id, place_id, provider, username, canonical_url, check_attempts, "
                    "ownership_verified_at")
            .eq("id", identity_id)
            .limit(1)
            .execute()
        )
        rows = res.data or []
    except Exception:
        logger.exception("claim_backlink: recheck load failed")
        return _result(False, R_FETCH_FAILED)
    if not rows:
        return _result(False, R_BAD_URL)
    row = rows[0]
    if row.get("ownership_verified_at"):
        return _result(True, R_OK, already=True)
    return _run_check(row, _normalize(handle))


def sweep_pending_backlinks(limit: int = 25) -> dict[str, int]:
    """Background: re-check pending claims whose backoff has elapsed. Never raises."""
    out = {"checked": 0, "verified": 0}
    try:
        res = (
            service_client()
            .table("external_community_identities")
            .select("id, place_id, provider, username, canonical_url, check_attempts")
            .is_("ownership_verified_at", "null")
            .not_.is_("next_check_after", "null")
            .lte("next_check_after", _now().isoformat())
            .limit(limit)
            .execute()
        )
        rows = res.data or []
    except Exception:
        logger.exception("claim_backlink: sweep load failed")
        return out

    for row in rows:
        handle = _handle_for_place(str(row.get("place_id") or ""))
        if not handle:
            continue
        result = _run_check(row, handle)
        out["checked"] += 1
        if result.get("verified"):
            out["verified"] += 1
    logger.info("claim_backlink: sweep checked=%d verified=%d", out["checked"], out["verified"])
    return out


# ── internals ───────────────────────────────────────────────────────────────

def _handle_for_place(place_id: str) -> str | None:
    """The handle being claimed. Reservation first (the handle is not on places until
    verification — places_handle_needs_verification), then places as a fallback."""
    if not place_id:
        return None
    try:
        res = (
            service_client()
            .table("place_handle_reservations")
            .select("normalized_handle")
            .eq("place_id", place_id)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if rows:
            return _normalize(str(rows[0].get("normalized_handle") or ""))
        res2 = (
            service_client().table("places").select("handle").eq("id", place_id)
            .limit(1).execute()
        )
        rows2 = res2.data or []
        return _normalize(str(rows2[0].get("handle") or "")) if rows2 else None
    except Exception:
        logger.exception("claim_backlink: handle lookup failed for %s", place_id)
        return None


def _existing_row(place_id: str, provider: str, username: str) -> dict[str, Any] | None:
    try:
        res = (
            service_client()
            .table("external_community_identities")
            .select("id, place_id, provider, username, canonical_url, check_attempts, "
                    "ownership_verified_at")
            .eq("place_id", place_id)
            .eq("provider", provider)
            .ilike("username", username)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception:
        logger.exception("claim_backlink: existing-row lookup failed")
        return None


def _insert_pending(
    *, place_id: str, provider: str, username: str, canonical_url: str, claim_id: str | None
) -> dict[str, Any] | None:
    """Hold the account from the first attempt. The unique indexes then stop two people
    racing on the same profile while one of them is still editing their bio."""
    try:
        res = (
            service_client()
            .table("external_community_identities")
            .insert({
                "place_id": place_id,
                "provider": provider,
                "provider_account_id": None,
                "username": username,
                "canonical_url": canonical_url,
                "ownership_method": "profile_backlink",
                "ownership_verified_at": None,
                "check_attempts": 0,
                "claim_id": claim_id,
            })
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        text = str(exc).lower()
        if "eci_provider" in text or "duplicate" in text or "unique" in text:
            return None  # belongs to another community
        logger.exception("claim_backlink: pending insert failed")
        return None


def _run_check(row: dict[str, Any], handle: str) -> dict[str, Any]:
    identity_id = str(row.get("id") or "")
    url = str(row.get("canonical_url") or "")
    attempts = int(row.get("check_attempts") or 0) + 1

    matched, where, reason = find_backlink(url, handle)

    if matched:
        _update(identity_id, {
            "ownership_verified_at": "now()",
            "backlink_url_seen": matched[:500],
            "canonical_url": where or url,
            "last_checked_at": "now()",
            "last_check_reason": R_OK,
            "check_attempts": attempts,
            "next_check_after": None,
        })
        logger.info("claim_backlink: verified identity=%s handle=%s via=%s",
                    identity_id, handle, where)
        return _result(True, R_OK, matched=matched[:200], found_at=where,
                       verification_method="profile_backlink")

    gave_up = attempts >= MAX_ATTEMPTS
    _update(identity_id, {
        "last_checked_at": "now()",
        "last_check_reason": reason or R_NOT_FOUND,
        "check_attempts": attempts,
        "next_check_after": _next_check_after(attempts),
    })

    # NOT_FOUND is not a failure yet — it is "we looked, it is not up, we will look
    # again". That distinction is the whole of case 2, and the UI must not render the
    # pending state as a rejection.
    if reason in (R_NOT_FOUND, R_NOT_PUBLIC, R_FETCH_FAILED) and not gave_up:
        return _result(False, R_PENDING, attempts=attempts, last_reason=reason,
                       identity_id=identity_id, expected=f"{LANA_HOST}/{handle}")
    if gave_up:
        return _result(False, R_GAVE_UP, attempts=attempts, last_reason=reason,
                       identity_id=identity_id)
    return _result(False, reason or R_NOT_FOUND, identity_id=identity_id,
                   expected=f"{LANA_HOST}/{handle}", found_at=where)


def _update(identity_id: str, patch: dict[str, Any]) -> None:
    try:
        service_client().table("external_community_identities").update(patch).eq(
            "id", identity_id
        ).execute()
    except Exception:
        logger.exception("claim_backlink: update failed for %s", identity_id)
