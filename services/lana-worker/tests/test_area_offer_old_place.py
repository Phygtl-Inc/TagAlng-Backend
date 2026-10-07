"""Tapping a pill that moves the search must not keep filtering on the place it moved from.

Prod 2026-10-08: "or in newyork" found nothing, Lana offered "Look in Lake Nona — Area A",
and the tap re-ran the typed request — "or in newyork" — against Lake Nona's meets. The
filter rejected each one as "asked for New York, this is Orlando", so the area she had
just offered came back empty. "Look beyond <community>" had the same trap (Pouya #3).
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.activity_browse as ab

_ORLANDO = {"id": "e1", "title": "Books & Neighbors Gathering", "distance_meters": 3000.0}


def _tap(draft: dict[str, Any], chip: str) -> tuple[str, list[str], dict[str, Any]]:
    ctx: dict[str, Any] = {
        "activity_browse_active": True, "browse_draft": draft, "phone_verified": True,
        "_offered_chip_msgs": [chip],
    }
    requests: list[str] = []

    def judged(rows, request, **_):
        requests.append(request)
        # The real matcher's verdict on an Orlando meet for a request naming New York.
        if "newyork" in request or "sjsu" in request:
            return [], ""
        return rows, ""

    with mock.patch.object(ab, "_fetch_block_events", return_value=[dict(_ORLANDO)]), \
         mock.patch.object(ab, "_fetch_admitted_events", return_value=([dict(_ORLANDO)], False)), \
         mock.patch.object(ab, "_filter_events_by_query", side_effect=judged), \
         mock.patch.object(ab, "_zip_gate_frame", return_value=None), \
         mock.patch.object(ab, "_far_offer", return_value=([], "", [])), \
         mock.patch.object(ab, "_attach_host_names"), \
         mock.patch("app.community_scope.clear_active_community"), \
         mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False), \
         mock.patch("app.orchestrator.llm.llm_configured", return_value=False):
        reply = ab.run_activity_browse_turn(
            user_message=chip, session_ctx=ctx, history=[], user_jwt="jwt",
            home_block_id="b-home", slots={}, user_id="u1",
        )
    return reply, requests, ctx


def test_look_in_area_searches_the_topic_not_the_old_place() -> None:
    chip = "Look in Lake Nona — Area A"
    reply, requests, ctx = _tap({
        "_seek_offer": True, "_asked": True, "interest": "", "_request": "or in newyork",
        "_area_offer_chip": chip, "_area_offer_block_id": "b-lake-nona",
        "_area_offer_name": "Lake Nona — Area A",
    }, chip)
    assert requests and all("newyork" not in r for r in requests)
    assert [p["title"] for p in ctx["activity_previews"]] == ["Books & Neighbors Gathering"]
    assert "Lake Nona" in reply


def test_look_in_area_keeps_the_topic() -> None:
    chip = "Look in Lake Nona — Area A"
    _, requests, _ = _tap({
        "_seek_offer": True, "_asked": True, "interest": "books",
        "_request": "any books club in newyork", "_area_offer_chip": chip,
        "_area_offer_block_id": "b-lake-nona", "_area_offer_name": "Lake Nona — Area A",
    }, chip)
    assert requests == ["books"]


def test_look_beyond_a_community_drops_its_name_from_the_filter() -> None:
    chip = "Look beyond San Jose State University"
    _, requests, ctx = _tap({
        "_seek_offer": True, "_asked": True, "interest": "",
        "_request": "at sjsu what events are going on this week",
        "_community_chip": chip, "_community": None,
    }, chip)
    assert requests and all("sjsu" not in r for r in requests)
    assert ctx["activity_previews"]
