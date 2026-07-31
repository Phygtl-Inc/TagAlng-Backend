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
