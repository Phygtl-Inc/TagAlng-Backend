"""Thumbs up/down on Lana output (chat replies, rapport questions, fellows rec lines,
surfaced meets, communities).

The PWA posts a rating against a lana_messages id (an assistant reply), a rapport_gaps
gap_row_id (a "By the way…" question), a peer_rec_lines id (the authored "why this
neighbour" line on a fellows row), an events id (the map's meet card) or a places id
(the map's community card). One row per (user, target):
rating again with the other thumb flips it, `clear` deletes it. The rated text is
snapshotted from the DB at rating time — never trusted from the client — so the team
reviews exactly what Lana said, even if the source row is later deleted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.community_discovery import _discovery_status_line
from app.db import service_client

logger = logging.getLogger("lana.feedback")

RATINGS = ("up", "down")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_message_target(user_id: str, message_id: str) -> dict[str, Any]:
    """Load the rated message and prove it belongs to one of the caller's sessions."""
    sb = service_client()
    res = (
        sb.table("lana_messages")
        .select("id, session_id, role, content")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="message_not_found")
    ses = (
        sb.table("lana_sessions")
        .select("id, user_id")
        .eq("id", row["session_id"])
        .limit(1)
        .execute()
    )
    ses_row = (ses.data or [None])[0]
    if not ses_row or str(ses_row.get("user_id")) != str(user_id):
        raise HTTPException(status_code=404, detail="message_not_found")
    if row.get("role") != "assistant":
        # Only Lana's own output is rateable — a thumb on the user's message means nothing.
        raise HTTPException(status_code=400, detail="not_an_assistant_message")
    return {
        "target_kind": "message",
        "message_id": message_id,
        "snapshot": str(row.get("content") or "")[:2000],
        "session_id": str(row.get("session_id")),
    }


def _resolve_rapport_target(user_id: str, gap_row_id: str) -> dict[str, Any]:
    """Load the rated rapport question and prove it belongs to the caller."""
    res = (
        service_client()
        .table("rapport_gaps")
        .select("gap_row_id, user_id, gap_id, question")
        .eq("gap_row_id", gap_row_id)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or str(row.get("user_id")) != str(user_id):
        raise HTTPException(status_code=404, detail="rapport_question_not_found")
    return {
        "target_kind": "rapport_question",
        "gap_row_id": gap_row_id,
        "snapshot": str(row.get("question") or "")[:2000],
        "gap_id": row.get("gap_id"),
    }


def _resolve_rec_target(user_id: str, rec_id: str) -> dict[str, Any]:
    """Load the rated fellows line and prove it was authored FOR the caller.

    peer_rec_lines rows are per-viewer (app/peer_rec_line.py), so the ownership check is
    also the disclosure check: a rec_id belonging to someone else must not echo its line
    back in content_snapshot.
    """
    res = (
        service_client()
        .table("peer_rec_lines")
        .select("id, user_id, peer_user_id, line")
        .eq("id", rec_id)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row or str(row.get("user_id")) != str(user_id):
        raise HTTPException(status_code=404, detail="rec_not_found")
    return {
        "target_kind": "peer_rec",
        "rec_id": rec_id,
        "snapshot": str(row.get("line") or "")[:2000],
        "peer_user_id": str(row.get("peer_user_id") or "") or None,
    }


def _viewer_fit_line(user_id: str, event_id: str) -> str:
    """The "why Lana sees a fit" line this viewer was shown for this meet (§50(b)), from
    event_fit_lines (20270110130001) — newest first, since a new overlap authors a new
    row. "" when none was ever authored for them, or the read fails."""
    try:
        res = (
            service_client()
            .table("event_fit_lines")
            .select("line, created_at")
            .eq("user_id", user_id)
            .eq("event_id", event_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001 — the title + tags snapshot still stands
        logger.warning("feedback: fit-line read failed event=%s", event_id, exc_info=True)
        return ""
    row = (res.data or [None])[0] or {}
    return " ".join(str(row.get("line") or "").split())


def _event_snapshot(row: dict[str, Any], fit_line: str = "") -> str:
    """The text a thumb on a meet card is about.

    §51 names the meet's fit line (§50) first: the line the viewer was actually shown,
    read back from its cache table, never from the client. A viewer who was never shown
    one (no shared threads, compose failed) rated what every viewer sees: the title and
    its cohort, by display label rather than taxonomy id."""
    title = str(row.get("title") or "").strip()
    if fit_line:
        return fit_line
    tags = [str(t).strip() for t in (row.get("cohort_tags") or []) if str(t or "").strip()]
    if tags:
        from app.context import cohort_tag_labels_for

        try:
            tags = [str(t) for t in cohort_tag_labels_for(tags) if str(t or "").strip()] or tags
        except Exception:  # noqa: BLE001 — the raw ids are still the truth
            logger.warning("feedback: cohort label lookup failed", exc_info=True)
    return f"{title} · {', '.join(tags)}" if tags else title


def _resolve_event_target(user_id: str, event_id: str) -> dict[str, Any]:
    """Load the rated meet. Any meet that exists is rateable: the snapshot is never echoed
    back to the caller, so there is nothing to disclose and no ownership to prove."""
    res = (
        service_client()
        .table("events")
        .select("id, title, cohort_tags")
        .eq("id", event_id)
        .limit(1)
        .execute()
    )
    row = (res.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="event_not_found")
    fit_line = _viewer_fit_line(user_id, event_id)
    return {
        "target_kind": "event",
        "event_id": event_id,
        "snapshot": _event_snapshot(row, fit_line)[:2000],
        # The fit line alone does not say WHICH meet; the title rides in context so it
        # survives the event row being deleted (same as place_name).
        "event_title": str(row.get("title") or "").strip() or None,
    }


def _resolve_place_target(user_id: str, place_id: str) -> dict[str, Any]:
    """Load the rated community and snapshot the status line its discovery row shipped.

    The line is NOT taken from the client (nothing in lana_feedback is): it is recomputed
    with the same function /lana/circles/discover uses, over the same member view
    (visible_place_members — confirmed, block-filtered), for this viewer. It only differs
    from what the card showed if membership changed between the fetch and the tap. A place
    nobody visible belongs to has no status line, so the snapshot falls back to its name.
    """
    sb = service_client()
    res = sb.table("places").select("id, name").eq("id", place_id).limit(1).execute()
    row = (res.data or [None])[0]
    if not row:
        raise HTTPException(status_code=404, detail="place_not_found")
    name = str(row.get("name") or "").strip()
    members_res = (
        sb.rpc("visible_place_members", {"p_user_id": user_id})
        .eq("place_ref", place_id)
        .execute()
    )
    member_ids = {str(m.get("user_id")) for m in (members_res.data or []) if m.get("user_id")}
    status_line = (
        _discovery_status_line(len(member_ids), str(user_id) in member_ids) if member_ids else ""
    )
    return {
        "target_kind": "place",
        "place_id": place_id,
        "snapshot": (status_line or name)[:2000],
        # The status line alone ("3 people") does not say WHICH community; the name rides
        # in context so it too survives the place row being deleted.
        "place_name": name or None,
    }


def record_feedback(
    user_id: str,
    *,
    rating: str,
    message_id: str | None = None,
    gap_row_id: str | None = None,
    rec_id: str | None = None,
    event_id: str | None = None,
    place_id: str | None = None,
    comment: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upsert (rating in RATINGS) or delete (rating == 'clear') the caller's feedback.

    `comment` is the optional free-text follow-up (the PWA offers it on a thumbs-down).
    It always tracks the latest rating write: re-rating without a comment clears the old
    one, so a stale 👎 explanation never survives onto a flipped 👍.

    Returns {"rating": <current rating or None>, "target_kind": ...} so the FE can
    render the thumb state straight from the response.
    """
    if sum(1 for t in (message_id, gap_row_id, rec_id, event_id, place_id) if t) != 1:
        raise HTTPException(status_code=400, detail="exactly_one_target_required")
    if rating not in (*RATINGS, "clear"):
        raise HTTPException(status_code=400, detail="invalid_rating")

    if message_id:
        target = _resolve_message_target(user_id, message_id)
        match_col, match_val = "message_id", message_id
    elif rec_id:
        target = _resolve_rec_target(user_id, str(rec_id))
        match_col, match_val = "rec_id", str(rec_id)
    elif event_id:
        target = _resolve_event_target(user_id, str(event_id))
        match_col, match_val = "event_id", str(event_id)
    elif place_id:
        target = _resolve_place_target(user_id, str(place_id))
        match_col, match_val = "place_id", str(place_id)
    else:
        target = _resolve_rapport_target(user_id, str(gap_row_id))
        match_col, match_val = "gap_row_id", str(gap_row_id)

    sb = service_client()
    existing = (
        sb.table("lana_feedback")
        .select("id, rating")
        .eq("user_id", user_id)
        .eq(match_col, match_val)
        .limit(1)
        .execute()
    )
    existing_row = (existing.data or [None])[0]

    if rating == "clear":
        # Un-thumb: the team should only see ratings the user still stands behind.
        if existing_row:
            sb.table("lana_feedback").delete().eq("id", existing_row["id"]).execute()
        return {"rating": None, "target_kind": target["target_kind"]}

    ctx = {k: v for k, v in (context or {}).items() if v is not None}
    if target.get("session_id"):
        ctx.setdefault("session_id", target["session_id"])
    if target.get("gap_id"):
        ctx.setdefault("gap_id", target["gap_id"])
    if target.get("peer_user_id"):
        # Who the rated line was about — the team reads "this pairing got a 👎" without a
        # join back into a table whose row may have been re-authored since.
        ctx.setdefault("peer_user_id", target["peer_user_id"])
    if target.get("place_name"):
        ctx.setdefault("place_name", target["place_name"])
    if target.get("event_title"):
        ctx.setdefault("event_title", target["event_title"])
    comment = (comment or "").strip()[:2000] or None

    if existing_row:
        sb.table("lana_feedback").update(
            {"rating": rating, "comment": comment, "context": ctx, "updated_at": _now_iso()}
        ).eq("id", existing_row["id"]).execute()
    else:
        sb.table("lana_feedback").insert(
            {
                "user_id": user_id,
                "target_kind": target["target_kind"],
                "message_id": message_id,
                "gap_row_id": gap_row_id,
                "rec_id": rec_id,
                "event_id": event_id,
                "place_id": place_id,
                "rating": rating,
                "comment": comment,
                "content_snapshot": target["snapshot"],
                "context": ctx,
            }
        ).execute()
    return {"rating": rating, "target_kind": target["target_kind"]}
