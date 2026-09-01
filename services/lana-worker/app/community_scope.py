"""The community filter at the top of the app (SWITCH COMMUNITY sheet).

One selection, one session key, one rule: everything the user asks for this turn
is read inside that community — neighbours, meets, recommendations — and a meet
they create there is tagged to it. Nothing selected is the default and the old
behaviour: their ZIP area.

A community IS a canonical place (`places.id`), and membership is
`circle_affiliations` — see [[circle-place-mandatory]]. So a scoped read is a
FILTER over the read that already exists: the searches keep their own ranking,
they just stop at the roster. No new search, no new table, no new RPC.

Empty is never a dead end and never a silent widen ([[far-supply-honest-empty]]):
a scoped read that finds nothing hands the caller `widened_from` — the
community's name — so the reply says which community was empty before it shows
the wider area.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# session_ctx key. {"place_id": str, "name": str} or None (= the ZIP default).
CTX_KEY = "active_community"


def active_community(session_ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    val = (session_ctx or {}).get(CTX_KEY)
    return val if isinstance(val, dict) and val.get("place_id") else None


def active_community_id(session_ctx: dict[str, Any] | None) -> str | None:
    comm = active_community(session_ctx)
    return str(comm["place_id"]) if comm else None


def community_name(session_ctx: dict[str, Any] | None) -> str | None:
    comm = active_community(session_ctx)
    return str(comm.get("name") or "").strip() or None if comm else None


def apply_community_selection(
    session_ctx: dict[str, Any],
    raw: str | None,
    *,
    user_id: str | None,
) -> dict[str, Any] | None:
    """Stamp what the switcher sent onto the session.

    `None` means the client said nothing about the filter — keep what's there.
    `""` is the explicit "no community" pick, which clears it. Membership is
    re-checked here (the id comes from the client), and a place the caller
    doesn't belong to clears the filter rather than scoping to it.
    """
    if raw is None:
        return active_community(session_ctx)
    place_id = str(raw).strip()
    if not place_id:
        session_ctx[CTX_KEY] = None  # None, not pop — [[ctx-pop-resurrection]]
        return None
    current = active_community(session_ctx)
    if current and current.get("place_id") == place_id:
        return current
    from app.community_surface import _place_row, caller_affiliation_at

    if not user_id or not caller_affiliation_at(
        user_id, place_id, statuses=("confirmed", "curious")
    ):
        logger.info("community_scope.not_a_member user=%s place=%s", user_id, place_id)
        session_ctx[CTX_KEY] = None
        return None
    name = str((_place_row(place_id) or {}).get("name") or "").strip()
    comm = {"place_id": place_id, "name": name}
    session_ctx[CTX_KEY] = comm
    return comm


def clear_active_community(session_ctx: dict[str, Any]) -> None:
    """The user asked to look past the filter (the widen pill). Cleared for the rest
    of the session — they can pick the community again in the switcher."""
    session_ctx[CTX_KEY] = None


def community_events(
    place_id: str, *, limit: int = 30, exclude_host_id: str | None = None
) -> list[dict[str, Any]]:
    """Upcoming meets of the community: created for it, or held at its place.

    A community read, NOT a neighbourhood read intersected with one — the place can
    sit outside the caller's block radius, and a meet of the community she is
    looking at is hers to see wherever it is.
    """
    from app.auth import service_client
    from app.event_publish import roll_recurring_events

    try:
        roll_recurring_events()  # a weekly meet's row carries its NEXT occurrence
        from datetime import datetime

        res = (
            service_client()
            .table("events")
            .select(
                "id, title, starts_at, has_time, venue_name, cohort_tags, host_id, "
                "recurrence, circle_place_ref, place_ref"
            )
            .eq("status", "open")
            .gte("starts_at", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"))
            .or_(f"circle_place_ref.eq.{place_id},place_ref.eq.{place_id}")
            .order("starts_at")
            .limit(max(limit, 1))
            .execute()
        )
        rows = res.data if isinstance(res.data, list) else []
    except Exception:  # noqa: BLE001 - an empty community reads as empty, never a 500
        logger.exception("community_scope.events_failed place=%s", place_id)
        return []
    out = [
        r
        for r in rows
        if isinstance(r, dict)
        and not (exclude_host_id and str(r.get("host_id") or "") == exclude_host_id)
    ]
    logger.info("community_scope.events place=%s rows=%d", place_id, len(out))
    return out


def here_place(session_ctx: dict[str, Any] | None, user_id: str | None) -> dict[str, Any] | None:
    """What "here" means for the extractor: {place_id, name, circle_key} of the community.

    The circle_key is the CALLER'S own affiliation key, because that is what the
    feature router matches on (circles_capture.persist_place_feature_candidates) —
    a feature emitted against it lands on that place's profile for every member.
    """
    comm = active_community(session_ctx)
    if not comm or not user_id:
        return None
    from app.community_surface import caller_affiliation_at

    aff = caller_affiliation_at(user_id, str(comm["place_id"])) or {}
    key = str(aff.get("circle_key") or "").strip()
    name = str(comm.get("name") or "").strip()
    if not (key and name):
        return None
    return {"place_id": str(comm["place_id"]), "name": name, "circle_key": key}


def member_ids(place_id: str) -> set[str]:
    """Everyone at the place — members and curious joiners, non-dismissed."""
    from app.community_surface import _member_rows

    return {
        str(r.get("user_id"))
        for r in _member_rows(place_id)
        if str(r.get("user_id") or "")
    }


def rows_by_members(
    rows: list[dict[str, Any]], place_id: str, *, key: str = "peer_user_id"
) -> list[dict[str, Any]]:
    """Keep only the rows authored by / about someone in the community."""
    ids = member_ids(place_id)
    if not ids:
        return []
    return [r for r in rows if str((r or {}).get(key) or "") in ids]


def events_in_community(
    events: list[dict[str, Any]], place_id: str
) -> list[dict[str, Any]]:
    """Meets that belong to the community: created for it (`circle_place_ref`) or
    held at its place (`place_ref`). The second half matters because tagging is
    optional — a meet AT the gym is a gym meet whether or not the host said so."""
    return [
        e
        for e in events
        if str((e or {}).get("circle_place_ref") or "") == place_id
        or str((e or {}).get("place_ref") or "") == place_id
    ]


def peers_in_community(
    user_id: str, place_id: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Fellow members, the ones who share the most with the caller first.

    The roster read already proves membership, drops blocks, and carries the
    shared threads and the Nudge ([[truthful-peer-match-model]]) — this only
    re-orders it into the peer-card shape the find-peers surfaces render. No
    similarity score: nothing here compared two people by vector.
    """
    from app.community_surface import MEMBERS_PAGE, community_members

    try:
        page = community_members(
            user_id, place_id=place_id, limit=max(MEMBERS_PAGE, limit)
        )
    except ValueError:  # not_a_member — a curious joiner has no roster
        return []
    except Exception:  # noqa: BLE001 - a scoped read must never break the turn
        logger.exception("community_scope.peers_failed place=%s", place_id)
        return []
    from app.onion_blend import _caller_place_tags

    # "your gym" — the one fact every row here proves, worded the way the onion arm
    # already words it so both lists read the same.
    place_tag = _caller_place_tags(user_id).get(place_id) or page.get("place_name")
    rows: list[dict[str, Any]] = []
    for m in page.get("members") or []:
        if m.get("me") or not m.get("nickname"):
            continue
        shared = [str(place_tag)] if place_tag else []
        shared += [str(t) for t in (m.get("trait_tags") or []) if str(t).strip()]
        rows.append(
            {
                "peer_user_id": m.get("peer_user_id"),
                "nickname": m.get("nickname"),
                "avatar_url": m.get("avatar_url"),
                "connection": m.get("connection"),
                # No cosine was computed for this pair — never invent one. The proof
                # is membership (+ any shared threads), which is the onion's own
                # unscored class, so the card renders a badge and no stars.
                "similarity_score": None,
                "onion_match": True,
                "matching_peer_label": shared[0] if shared else None,
                "matching_peer_concept": None,
                "has_exact_concept_match": len(shared) > 1,
                "shared_labels": shared,
                "community_name": page.get("place_name"),
            }
        )
    rows.sort(key=lambda r: -len(r.get("shared_labels") or []))
    # The one line that answers "did the filter apply to this turn?" in the worker log.
    logger.info(
        "community_scope.peers place=%s name=%r rows=%d", place_id, place_tag, len(rows)
    )
    return rows[: max(limit, 1)]


def widen_chip(name: str | None) -> str:
    """Pill label for the escape hatch. A label, not prose — the message itself
    is AI-authored from the `widened_from` fact ([[ai-authored-copy-not-canned]])."""
    return f"Look beyond {name}" if name else "Widen the search"
