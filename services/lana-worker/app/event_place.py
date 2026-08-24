"""Events ↔ canonical places (Place Profile §2.4/§5.2).

An event whose venue resolved to a Google place gets anchored to the canonical
places row (events.place_ref), dual-written next to the legacy events.place_id
string for one release. Anchoring is what powers "N people already go here —
invite them?": the confirmed members of the event's place are the natural first
invite list.

Event creation stays completely un-gated by unlock_state (§D.2) — anchoring is
metadata, never a gate.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

_SUGGESTION_CAP = 10


def stamp_event_place_async(event_id: str, google_place_id: str | None, user_id: str) -> None:
    """Post-publish: resolve the venue's canonical place and stamp events.place_ref.
    Fire-and-forget — publishing never waits on Google."""
    pid = str(google_place_id or "").strip()
    eid = str(event_id or "").strip()
    if not pid or not eid:
        return

    def _run() -> None:
        try:
            from app.circles_flow import upsert_canonical_place
            from app.places import place_details

            details = place_details(pid)
            if not details:
                logger.info("event_place.stamp_skip event=%s reason=no_details", eid)
                return
            place_id = upsert_canonical_place(details, created_by=user_id)
            if not place_id:
                return
            service_client().table("events").update({"place_ref": place_id}).eq(
                "id", eid
            ).execute()
            logger.info("event_place.stamped event=%s place=%s", eid, place_id)
        except Exception:
            logger.exception("event_place.stamp_failed event=%s", eid)

    threading.Thread(target=_run, daemon=True, name=f"event-place-{eid[:8]}").start()


def _when_line(starts_at: str | None, has_time: bool) -> str:
    """"Sat, Aug 22 · 7:00 AM" — the meet's own timezone, human-first. A date-only meet
    (#56 midnight placeholder) says so rather than claiming 12:00 AM."""
    from datetime import datetime, timezone

    from app.event_publish import event_tz

    raw = str(starts_at or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    local = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(event_tz())
    day = local.strftime("%a, %b %d").replace(" 0", " ")
    if not has_time:
        return f"{day} · time to be set"
    return f"{day} · {local.strftime('%I:%M %p').lstrip('0')}"


def stamp_event_community(
    event_id: str, circle_place_id: str, host_id: str, title: str
) -> int:
    """Tag a published meet with the community the host picked (setup card 2/5) and email
    that community's other members. Returns how many were emailed.

    Membership is verified HERE: the picked id comes from the client, so a host can only
    tag — and mail — a community they are themselves a confirmed member of."""
    sb = service_client()
    member = (
        sb.table("circle_affiliations")
        .select("id")
        .eq("user_id", host_id)
        .eq("place_ref", circle_place_id)
        .eq("status", "confirmed")
        .is_("dismissed_at", "null")
        .limit(1)
        .execute()
    )
    if not (member.data or []):
        logger.info("event_community.skip event=%s reason=not_a_member", event_id)
        return 0

    sb.table("events").update({"circle_place_ref": circle_place_id}).eq(
        "id", event_id
    ).execute()

    place = (
        sb.table("places").select("name").eq("id", circle_place_id).limit(1).execute()
    )
    place_name = ((place.data or [{}])[0] or {}).get("name") or ""
    # When and where, in the mail itself: a meet invite that makes somebody open the app
    # just to learn the date is an invite most people never open.
    row = (
        sb.table("events")
        .select("starts_at,has_time,venue_name,cover_emoji")
        .eq("id", event_id)
        .limit(1)
        .execute()
    )
    event = (row.data or [{}])[0] or {}
    when = _when_line(event.get("starts_at"), bool(event.get("has_time")))
    where = str(event.get("venue_name") or "").strip()
    from app.i18n import t
    from app.notifications import _user_contact, email_html, mail_community_members

    _, host_name = _user_contact(host_id)

    def render(lang: str | None) -> tuple[str, str]:
        return (
            t("notify.community_event.subject", lang, place=place_name),
            email_html(
                t("notify.community_event.title", lang, title=title, place=place_name),
                t("notify.community_event.body", lang, place=place_name),
                t("notify.community_event.cta", lang),
                f"/meet/{event_id}",
                preheader=when or None,
                badge=str(event.get("cover_emoji") or "").strip() or "📍",
                kicker=t("notify.community_note", lang, name=place_name),
                facts=[
                    (t("notify.facts.when", lang), when),
                    (t("notify.facts.where", lang), where),
                    (t("notify.facts.host", lang), host_name or ""),
                ],
            ),
        )

    sent = mail_community_members(circle_place_id, exclude_user_id=host_id, render=render)
    logger.info(
        "event_community.mailed event=%s place=%s sent=%s", event_id, circle_place_id, sent
    )
    return sent


def stamp_event_community_async(
    event_id: str, circle_place_id: str | None, host_id: str, title: str
) -> None:
    """Fire-and-forget wrapper — publishing never waits on the community mail-out."""
    pid = str(circle_place_id or "").strip()
    eid = str(event_id or "").strip()
    if not pid or not eid:
        return

    def _run() -> None:
        try:
            stamp_event_community(eid, pid, host_id, title)
        except Exception:
            logger.exception("event_community.stamp_failed event=%s", eid)

    threading.Thread(target=_run, daemon=True, name=f"event-circle-{eid[:8]}").start()


def event_community(
    place_ref: str | None, host_id: str | None = None
) -> dict[str, Any] | None:
    """The community a meet was created for — {place_ref, name, emoji, circle_type, detail}
    — or None for a plain neighborhood meet.

    Thin wrapper over the SQL function of the same name (20261015120000) so the card in
    chat, the invite link, the similar-meets sheet and the notification copy all name the
    community identically. Best-effort: a failure just drops the tag."""
    pid = str(place_ref or "").strip()
    if not pid:
        return None
    try:
        res = service_client().rpc(
            "event_community",
            {"p_place_ref": pid, "p_host_id": str(host_id) if host_id else None},
        ).execute()
    except Exception:  # noqa: BLE001 - a missing tag must never break a turn or an email
        logger.exception("event_community.lookup_failed place=%s", pid)
        return None
    row = res.data if isinstance(res.data, dict) else None
    return row if row and row.get("name") else None


def community_line(community: dict[str, Any] | None) -> str | None:
    """One-line form of an already-resolved community, for prose surfaces (emails, push):
    "🏋️ Fitness CF"."""
    if not community or not community.get("name"):
        return None
    return " ".join(
        str(part) for part in (community.get("emoji"), community.get("name")) if part
    ).strip() or None


def community_label(place_ref: str | None, host_id: str | None = None) -> str | None:
    """`community_line` straight from a place id."""
    return community_line(event_community(place_ref, host_id))


def community_label_for_event(event_id: str | None) -> str | None:
    """`community_label` for an already-published meet — the notification hooks know the
    event id, not its community. None when the meet has no community."""
    eid = str(event_id or "").strip()
    if not eid:
        return None
    try:
        res = (
            service_client()
            .table("events")
            .select("circle_place_ref, host_id")
            .eq("id", eid)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None
    row = (res.data or [None])[0] or {}
    return community_label(row.get("circle_place_ref"), row.get("host_id"))


def invite_suggestions(user_id: str, event_id: str) -> dict[str, Any]:
    """Confirmed members of the event's place, for "N people already go here —
    invite them?" (§5.2). Host-only. First names only — that is what the ladder
    reveals at Stranger tier (§F.1); the place needs no gating here because the
    host already knows their own venue.

    Raises ValueError: event_not_found / not_event_host / event_has_no_place."""
    sb = service_client()
    res = (
        sb.table("events")
        .select("id, host_id, place_ref")
        .eq("id", event_id)
        .limit(1)
        .execute()
    )
    event = (res.data or [None])[0]
    if not event:
        raise ValueError("event_not_found")
    if str(event.get("host_id") or "") != user_id:
        raise ValueError("not_event_host")
    place_ref = str(event.get("place_ref") or "")
    if not place_ref:
        raise ValueError("event_has_no_place")

    members = (
        sb.table("circle_affiliations")
        .select("user_id")
        .eq("place_ref", place_ref)
        .eq("status", "confirmed")
        .is_("dismissed_at", "null")
        .neq("user_id", user_id)
        .limit(50)
        .execute()
    )
    member_ids = list(dict.fromkeys(
        str(r["user_id"]) for r in (members.data or []) if r.get("user_id")
    ))
    count = len(member_ids)
    out: list[dict[str, Any]] = []
    if member_ids:
        users = (
            sb.table("users")
            .select("id, nickname")
            .in_("id", member_ids[:_SUGGESTION_CAP])
            .execute()
        )
        nick_by_id = {str(u["id"]): u.get("nickname") for u in (users.data or [])}
        for uid in member_ids[:_SUGGESTION_CAP]:
            out.append({"user_id": uid, "nickname": nick_by_id.get(uid) or "A neighbor"})
    return {"count": count, "members": out}
