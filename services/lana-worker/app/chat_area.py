"""Where the user said they are, for the rest of this conversation.

"I'm in <town>" got "I'll use <town> for this chat" — and the next search still ran from
the profile ZIP, with distances measured from it: a meet 1,300 miles away suggested as
"here", and "nothing nearby" right after a list headed with the stated town (prod
2026-10-07). Nothing kept the place: `search_place` is one browse's travel ask and
`resolve_block_id` is home-first by design.

The AI reads the place (discovery_slots `current_place`); this resolves it ONCE to an
area through the same town → ZIP → block path travel searches use, and keeps it on the
session as `chat_area` until they name another place or change their home ZIP. It is
never written to the profile — the home ZIP stays theirs to change.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def note_chat_area(session_ctx: dict[str, Any], slots: dict[str, Any] | None, user_jwt: str) -> None:
    """Record the town this turn says they are in as the session's search area.

    No-op when the turn names no place, or names the one already kept. A place that cannot
    be placed (outside the US, unknown) leaves the previous area as it was — the search
    lane says honestly where it looked."""
    from app.discovery_slots import slots_current_place

    place = slots_current_place(slots)
    if not place:
        return
    kept = session_ctx.get("chat_area")
    if isinstance(kept, dict) and str(kept.get("asked") or "").casefold() == place.casefold():
        return
    try:
        from app.discovery_route import resolve_zip_coverage
        from app.search_place import resolve_search_place

        got = resolve_search_place(place)
        zip5 = str((got or {}).get("zip5") or "")
        if not zip5:
            logger.info("chat_area_unplaced place=%r", place)
            return
        block, _status = resolve_zip_coverage(user_jwt, zip5)
    except Exception:  # noqa: BLE001 — remembering a place must never break the turn
        logger.exception("chat_area_resolve_failed place=%r", place)
        return
    if not block or not block.get("block_id"):
        logger.info("chat_area_no_block place=%r zip=%s", place, zip5)
        return
    session_ctx["chat_area"] = {
        "asked": place,
        "label": str((got or {}).get("label") or place),
        "zip5": zip5,
        "block_id": str(block["block_id"]),
        "lat": (got or {}).get("lat"),
        "lng": (got or {}).get("lng"),
    }


def chat_area(session_ctx: dict[str, Any] | None) -> dict[str, Any] | None:
    """The kept area ({"label", "zip5", "block_id", "lat", "lng"}), or None."""
    area = (session_ctx or {}).get("chat_area")
    if isinstance(area, dict) and area.get("block_id"):
        return area
    return None
