"""Chapters you can make and unmake, and the family a community's content comes from.

The SQL decides everything (20270124120000): who may attach (runs the chapter AND belongs
to the parent), who may detach (runs either side), depth, and which communities' content a
viewer sees. This module only calls it, and turns its refusals into reasons a reply can be
written from — never into an exception a turn has to catch.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth import service_client

logger = logging.getLogger(__name__)

# SQL refusal → the reason a caller reads. Anything unlisted is 'failed'.
_REASONS = (
    "not_a_member_of_parent",
    "not_your_community",
    "chapter_has_another_parent",
    "chapter_depth_exceeded",
    "parent_cannot_become_a_chapter",
    "creator_community_cannot_be_chapter",
    "chapter_needs_location",
    "chapter_cannot_parent_itself",
    "community_suspended",
    "chapter_not_found",
    "parent_not_found",
)


def _reason(exc: Exception) -> str:
    text = str(getattr(exc, "message", "") or exc)
    return next((r for r in _REASONS if r in text), "failed")


def attach_chapter(user_id: str, chapter_id: str, parent_id: str) -> dict[str, Any]:
    """{ok, parent_name, inherited_location, already, reason}. Never raises."""
    try:
        res = service_client().rpc(
            "attach_chapter",
            {"p_user_id": user_id, "p_chapter": chapter_id, "p_parent": parent_id},
        ).execute()
        data = res.data if isinstance(res.data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — a refusal is an answer, not a crash
        reason = _reason(exc)
        if reason == "failed":
            logger.exception("attach_chapter_failed chapter=%s parent=%s", chapter_id, parent_id)
        return {"ok": False, "reason": reason}
    logger.info("attach_chapter chapter=%s parent=%s", chapter_id, parent_id)
    return {
        "ok": True,
        "parent_name": data.get("parentName"),
        "inherited_location": bool(data.get("inheritedLocation")),
        "already": bool(data.get("alreadyAttached")),
        "reason": None,
    }


def detach_chapter(user_id: str, chapter_id: str) -> dict[str, Any]:
    """{ok, was_attached, parent_name, reason}. Never raises."""
    try:
        res = service_client().rpc(
            "detach_chapter", {"p_user_id": user_id, "p_chapter": chapter_id}
        ).execute()
        data = res.data if isinstance(res.data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        reason = _reason(exc)
        if reason == "failed":
            logger.exception("detach_chapter_failed chapter=%s", chapter_id)
        return {"ok": False, "reason": reason}
    return {
        "ok": True,
        "was_attached": bool(data.get("wasAttached")),
        "parent_name": data.get("parentName"),
        "reason": None,
    }


def community_family(user_id: str | None, place_id: str) -> list[dict[str, Any]]:
    """[{place_id, place_name, relation}] — the communities whose content this viewer sees
    when looking at `place_id` (itself, its parent, its chapters as the contract allows).

    Always contains `place_id` itself (relation 'self'), even when the read fails or there
    is no viewer: a failed family read must shrink to the community alone, never widen."""
    pid = str(place_id or "").strip()
    self_only = [{"place_id": pid, "place_name": None, "relation": "self"}]
    if not pid or not user_id:
        return self_only
    try:
        res = service_client().rpc(
            "community_family", {"p_user_id": user_id, "p_place_id": pid}
        ).execute()
        rows = [r for r in (res.data or []) if isinstance(r, dict) and r.get("place_id")]
    except Exception:  # noqa: BLE001
        logger.exception("community_family_failed place=%s", pid)
        return self_only
    out = [
        {
            "place_id": str(r["place_id"]),
            "place_name": str(r.get("place_name") or "").strip() or None,
            "relation": str(r.get("relation") or ""),
        }
        for r in rows
    ]
    return out if any(r["place_id"] == pid for r in out) else self_only


def label_origin(
    rows: list[dict[str, Any]], place_id: str, family: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Stamp each meet that came from ANOTHER community in the family with where it came
    from (origin_place_id / origin_place_name), in place.

    The contract's one hard rule for content that crosses a boundary: it carries its
    origin, because an unlabelled chapter meet on the parent's screen is indistinguishable
    from the parent's own. A meet that belongs to `place_id` itself is left unlabelled."""
    names = {f["place_id"]: f.get("place_name") for f in family}
    for r in rows:
        refs = [str(r.get("circle_place_ref") or ""), str(r.get("place_ref") or "")]
        if place_id in refs:
            continue
        origin = next((x for x in refs if x in names and x != place_id), None)
        if origin:
            r["origin_place_id"] = origin
            r["origin_place_name"] = names.get(origin)
    return rows
