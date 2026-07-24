"""Labeled invite links — the growth path (Circles master §A.2, §I.1).

The one rule everything here protects: AN INVITE IS NOT MEMBERSHIP. Redeeming a
link records the growth edge (users.invited_by, set once) and moves the ZIP
counter — unconditionally. A circle row is written ONLY when the joiner
self-confirms her OWN place via the generic prompt, which NEVER names the
inviter's place (a forwarded link must stay harmless: it can grow a ZIP, it can
never pollute a circle).

Rate limiting (§I.1) rides circle_invite_redemptions: per-invite hourly cap here;
per-IP throttling belongs at the edge.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.auth import service_client
from app.circles_capture import CIRCLE_TYPES

logger = logging.getLogger(__name__)

# Generous for a real group chat burst, hostile to scripted redemption.
_REDEMPTIONS_PER_HOUR = 30


def _invite_url(token: str) -> str:
    from app.notifications import app_url

    return app_url(f"/i/{token}")


def mint_invite(user_id: str, *, circle_key: str | None = None) -> dict[str, Any]:
    """Mint a labeled link. The label (circle_type/place_ref) only drives the
    joiner's GENERIC self-confirm prompt — it is never shown to them."""
    sb = service_client()
    circle_type: str | None = None
    place_ref: str | None = None
    key = (circle_key or "").strip().lower() or None
    if key:
        res = (
            sb.table("circle_affiliations")
            .select("circle_type, place_ref")
            .eq("user_id", user_id)
            .eq("circle_key", key)
            .is_("dismissed_at", "null")
            .limit(1)
            .execute()
        )
        row = (res.data or [None])[0]
        if not row:
            raise ValueError("circle_not_found")
        circle_type = row.get("circle_type")
        place_ref = row.get("place_ref")
    token = secrets.token_urlsafe(9)
    sb.table("circle_invites").insert(
        {
            "token": token,
            "owner_user_id": user_id,
            "circle_type": circle_type,
            "circle_key": key,
            "place_ref": place_ref,
        }
    ).execute()
    return {"token": token, "url": _invite_url(token)}


def _active_invite(token: str) -> dict[str, Any] | None:
    res = (
        service_client()
        .table("circle_invites")
        .select("id, owner_user_id, circle_type, circle_key, place_ref, revoked_at")
        .eq("token", str(token or "").strip())
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or row.get("revoked_at"):
        return None
    return row


def _rate_limited(invite_id: str) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    try:
        res = (
            service_client()
            .table("circle_invite_redemptions")
            .select("id", count="exact")
            .eq("invite_id", invite_id)
            .gte("created_at", since)
            .execute()
        )
        return int(res.count or 0) >= _REDEMPTIONS_PER_HOUR
    except Exception:
        logger.exception("invite_rate_check_failed invite=%s", invite_id)
        return False  # fail open — a transient read error must not block signups


def redeem_invite(user_id: str, token: str) -> dict[str, Any]:
    """Record the growth edge and return the generic self-confirm hint (§A.2).

    Idempotent per (invite, user). Raises ValueError for the endpoint to map:
    invite_not_found / invite_rate_limited."""
    invite = _active_invite(token)
    if not invite:
        raise ValueError("invite_not_found")
    owner_id = str(invite["owner_user_id"])
    if owner_id == user_id:
        # Self-taps happen (owner previewing her own link) — no edge, no prompt.
        return {"ok": True, "confirm_prompt": False, "circle_type": None}
    if _rate_limited(str(invite["id"])):
        raise ValueError("invite_rate_limited")

    sb = service_client()
    try:
        sb.table("circle_invite_redemptions").insert(
            {"invite_id": str(invite["id"]), "user_id": user_id}
        ).execute()
    except Exception:
        # unique(invite_id, user_id) — an idempotent re-tap, not an error.
        logger.debug("invite_redemption_exists invite=%s user=%s", invite["id"], user_id)

    # Growth attribution: set ONCE, never overwritten (first inviter wins).
    try:
        res = (
            sb.table("users")
            .select("invited_by, home_zip")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        profile = (res.data or [{}])[0]
        if not profile.get("invited_by"):
            sb.table("users").update({"invited_by": owner_id}).eq("id", user_id).execute()
        home_zip = str(profile.get("home_zip") or "").strip()
        if home_zip:
            from app.zip_unlock import recount_zip

            recount_zip(home_zip)
    except Exception:
        logger.exception("invite_attribution_failed user=%s", user_id)

    # The generic prompt hint: type only, NEVER the inviter's place (§A.2 M6).
    circle_type = invite.get("circle_type")
    return {
        "ok": True,
        "confirm_prompt": bool(circle_type and circle_type in CIRCLE_TYPES),
        "circle_type": circle_type if circle_type in CIRCLE_TYPES else None,
    }


def self_confirm(
    user_id: str,
    token: str,
    *,
    circle_type: str,
    detail: str | None = None,
) -> dict[str, Any]:
    """The joiner says yes to "are you part of a <type> community nearby?" —
    writes HER OWN ungrounded affiliation (source='invite_confirmed', invited_by =
    the inviter). Grounding her own place happens through the normal
    /lana/circles/ground-options → /ground flow; only that makes it confirmed."""
    invite = _active_invite(token)
    if not invite:
        raise ValueError("invite_not_found")
    if str(invite["owner_user_id"]) == user_id:
        raise ValueError("cannot_confirm_own_invite")
    from app.circles_flow import add_circle

    return add_circle(
        user_id,
        circle_type=circle_type,
        detail=detail,
        source="invite_confirmed",
        invited_by=str(invite["owner_user_id"]),
    )


def contribution_count(user_id: str) -> int:
    """§E.2 mechanic 3, derived: verified people this user brought in. The 30-day
    activity half of the spec's definition rides the ZIP recount; this count is the
    simpler profile number ("You've brought 4 neighbors")."""
    try:
        res = (
            service_client()
            .table("users")
            .select("id", count="exact")
            .eq("invited_by", user_id)
            .not_.is_("phone_verified_at", "null")
            .execute()
        )
        return int(res.count or 0)
    except Exception:
        logger.exception("contribution_count_failed user=%s", user_id)
        return 0
