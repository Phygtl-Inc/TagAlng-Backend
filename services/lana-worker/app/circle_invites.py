"""Labeled invite links — the growth path (Circles master §A.2, §I.1).

The one rule everything here protects: AN INVITE IS NOT MEMBERSHIP. Redeeming a
link records the growth edge (users.invited_by, set once) and moves the ZIP
counter — unconditionally; no circle row is ever written here. Redeem discloses
the invite's place_ref so the joiner can JOIN that community deliberately
(/lana/circles/join — the same self-claim its profile offers anyone); an
unlabeled link discloses nothing and still falls back to the generic
self-confirm prompt.

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
        return {
            "ok": True,
            "confirm_prompt": False,
            "circle_type": None,
            "place_id": None,
            "inviter_user_id": None,
            "inviter_name": None,
            "inviter_avatar_url": None,
            "link": None,
        }
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

            recount_zip(home_zip, notify_on_open=False)
    except Exception:
        logger.exception("invite_attribution_failed user=%s", user_id)

    # place_id names the community the link is FOR, so the joiner can join it
    # instead of self-confirming a lookalike of her own (LANA-57). §A.2 M6 stays
    # intact: /circles/join is a self-claim anyone seeing the profile can make,
    # reversible via /circles/remove and answerable as 'curious'.
    # Who sent it — an unlabeled link still has a sender, so these ride every
    # redemption (§27). One read for both: the guest who opens most invites cannot
    # read an avatar herself (get_profile_summary_authed blurs for her), and name may
    # be null anyway — not everyone has told Lana one yet.
    from app.community_surface import _users_by_id

    owner = _users_by_id([owner_id]).get(owner_id) or {}
    circle_type = invite.get("circle_type")
    return {
        "ok": True,
        "confirm_prompt": bool(circle_type and circle_type in CIRCLE_TYPES),
        "circle_type": circle_type if circle_type in CIRCLE_TYPES else None,
        "place_id": invite.get("place_ref"),
        "inviter_user_id": owner_id,
        "inviter_name": str(owner.get("nickname") or "").strip() or None,
        "inviter_avatar_url": str(owner.get("profile_photo_url") or "").strip() or None,
        # The community's public link when it has a page — same value as
        # invite_destination(token). Additive.
        "link": invite_link(token) if invite.get("place_ref") else None,
    }


def invite_link(token: str) -> str | None:
    """The public community link an invite lands on (invite_destination, 20270201120000):
    "{handle}" or "{parent}/{chapter}", or None when the invite's place has no public
    page. Never raises — a missing link only means the generic card."""
    try:
        res = service_client().rpc("invite_destination", {"p_token": str(token or "")}).execute()
        data = res.data
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            return str(data.get("link") or "").strip() or None
    except Exception:
        logger.exception("invite_destination_failed")
    return None


def attribute_join(
    user_id: str,
    token: str,
    place_id: str,
    *,
    affiliation_id: str | None = None,
) -> dict[str, Any] | None:
    """Credit a join of the invite's community to that invite (/lana/circles/join with
    invite_token). Called only AFTER the join succeeded; it never raises, so a join can
    never fail because of attribution.

    - the invite must be active and FOR this place (place_ref == place_id); a token
      carried onto another community's Join is logged and ignored;
    - self-invites are ignored;
    - the (invite, user) redemption is upserted with joined_at / joined_place_ref — an
      invitee who never redeemed (guest who then signed into an existing account under
      another id) gets the row here, subject to the per-invite hourly cap; a row that
      already has joined_at keeps its first join;
    - users.invited_by is set only when empty (first inviter wins);
    - the joined circle_affiliations row gets invite_id + invited_by only when it has no
      invite_id yet (first attribution wins).

    Returns {invite_id, inviter_user_id, redemption_created} or None when nothing was
    attributed."""
    try:
        place_id = str(place_id or "").strip()
        invite = _active_invite(token) if str(token or "").strip() else None
        if not invite or not place_id:
            return None
        invite_id = str(invite["id"])
        owner_id = str(invite.get("owner_user_id") or "")
        if str(invite.get("place_ref") or "") != place_id:
            logger.info(
                "invite_join_place_mismatch invite=%s invite_place=%s joined=%s",
                invite_id,
                invite.get("place_ref"),
                place_id,
            )
            return None
        if not owner_id or owner_id == user_id:
            return None

        sb = service_client()
        now = datetime.now(timezone.utc).isoformat()

        # 1 · the redemption row
        res = (
            sb.table("circle_invite_redemptions")
            .select("id, joined_at")
            .eq("invite_id", invite_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        existing = (res.data or [None])[0]
        created = False
        if existing:
            if not existing.get("joined_at"):
                sb.table("circle_invite_redemptions").update(
                    {"joined_at": now, "joined_place_ref": place_id}
                ).eq("id", existing["id"]).is_("joined_at", "null").execute()
        else:
            if _rate_limited(invite_id):
                logger.info("invite_join_rate_limited invite=%s user=%s", invite_id, user_id)
                return None
            try:
                sb.table("circle_invite_redemptions").insert(
                    {
                        "invite_id": invite_id,
                        "user_id": user_id,
                        "joined_at": now,
                        "joined_place_ref": place_id,
                    }
                ).execute()
                created = True
            except Exception:
                # unique(invite_id, user_id): a concurrent redeem landed first.
                sb.table("circle_invite_redemptions").update(
                    {"joined_at": now, "joined_place_ref": place_id}
                ).eq("invite_id", invite_id).eq("user_id", user_id).is_(
                    "joined_at", "null"
                ).execute()

        # 2 · growth edge, first inviter wins
        try:
            ures = sb.table("users").select("invited_by").eq("id", user_id).limit(1).execute()
            profile = (ures.data or [{}])[0] or {}
            if not profile.get("invited_by"):
                sb.table("users").update({"invited_by": owner_id}).eq("id", user_id).execute()
        except Exception:
            logger.exception("invite_join_invited_by_failed user=%s", user_id)

        # 3 · the membership row, first attribution wins
        try:
            q = (
                sb.table("circle_affiliations")
                .select("id, invite_id")
                .eq("user_id", user_id)
                .is_("dismissed_at", "null")
            )
            q = q.eq("id", affiliation_id) if affiliation_id else q.eq("place_ref", place_id)
            ares = q.execute()
            rows = [r for r in (ares.data or []) if isinstance(r, dict)]
            # One live row per (user, invite) is a unique index: if a self-confirm
            # candidate already holds this invite, that row IS the attribution.
            held = bool(
                (
                    sb.table("circle_affiliations")
                    .select("id")
                    .eq("user_id", user_id)
                    .eq("invite_id", invite_id)
                    .is_("dismissed_at", "null")
                    .limit(1)
                    .execute()
                ).data
            )
            already = any(r.get("invite_id") for r in rows)
            target = rows[0] if rows and not already and not held else None
            if target:
                sb.table("circle_affiliations").update(
                    {"invite_id": invite_id, "invited_by": owner_id}
                ).eq("id", target["id"]).is_("invite_id", "null").execute()
        except Exception:
            logger.exception("invite_join_affiliation_stamp_failed user=%s", user_id)

        logger.info(
            "invite_join_attributed invite=%s user=%s place=%s created=%s",
            invite_id,
            user_id,
            place_id,
            created,
        )
        return {
            "invite_id": invite_id,
            "inviter_user_id": owner_id,
            "redemption_created": created,
        }
    except Exception:
        logger.exception("invite_join_attribution_failed user=%s place=%s", user_id, place_id)
        return None


def self_confirm(
    user_id: str,
    token: str,
    *,
    circle_type: str,
    detail: str | None = None,
    membership: str | None = None,
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
        # One candidate per INVITE, not per kind (§28(c)); the membership answer rides
        # the candidate until she pins a place (§23).
        invite_id=str(invite.get("id") or "") or None,
        membership=membership,
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
