"""Widen means RELATED, and the location pill decides where "near you" is (2026-10-07).

Asjid, prod: "find a meet about AI" → nothing → "Widen the search" → a books club, headed
"near you" — in Orlando, while the location pill said Rawalpindi. Widen now relaxes the
topic bar to related meets instead of dropping the topic; the pill's point is searched
when it is in the US, and outside it the home area is searched and named as such.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.activity_browse as ab

_BOOKS = {"id": "books", "title": "Books & Neighbors Gathering", "distance_meters": 2000.0}
_PYTHON = {"id": "python", "title": "Python Study Group", "distance_meters": 3000.0}
_SCORES = {"books": 0.1, "python": 0.6}


def _filt(rows: list[dict], q: str) -> tuple[list[dict], str]:
    for r in rows:
        r["topic_score"] = _SCORES.get(r["id"], 0.0)
        r["topic_mismatch"] = "not AI"
    return [r for r in rows if r["topic_score"] >= 0.8], "AI"


def _turn(msg: str, ctx: dict, *, events: list[dict], slots: dict | None = None,
          point_area: Any = "unset", far: tuple = ([], "", [])) -> tuple[str, dict, mock.Mock]:
    fetch = mock.Mock(return_value=[dict(e) for e in events])
    patches = [
        mock.patch.object(ab, "_fetch_block_events", fetch),
        mock.patch.object(ab, "_fetch_admitted_events", return_value=None, create=True),
        mock.patch.object(ab, "_filter_events_by_query", side_effect=_filt),
        mock.patch.object(ab, "_zip_gate_frame", return_value=None),
        mock.patch.object(ab, "_far_offer", return_value=far),
        mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False),
        mock.patch("app.orchestrator.llm.llm_configured", return_value=False),
        mock.patch("app.discovery_route._try_assign_home_block"),
    ]
    if point_area != "unset":
        patches.append(mock.patch.object(ab, "_point_area", return_value=point_area))
    for p in patches:
        p.start()
    try:
        reply = ab.run_activity_browse_turn(
            user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt",
            home_block_id="b-home", slots=slots or {},
        )
    finally:
        for p in patches:
            p.stop()
    return reply, ctx, fetch


def _fresh() -> dict:
    return {"activity_browse_active": True, "browse_draft": {"_asked": True},
            "phone_verified": True}


def test_widen_shows_related_meets_never_unrelated_ones() -> None:
    ctx = _fresh()
    _turn("find a meet about AI", ctx, events=[_BOOKS, _PYTHON])
    assert ctx["activity_previews"] == []  # nothing on AI itself
    reply, ctx, _ = _turn("Widen the search", ctx, events=[_BOOKS, _PYTHON])
    titles = [p["title"] for p in ctx["activity_previews"]]
    assert titles == ["Python Study Group"]  # related, and the books club never
    assert "related to AI" in reply
    assert ctx["browse_draft"]["interest"]  # the topic was kept, not cleared


def test_widen_with_nothing_related_says_so_and_offers_hosting_not_widen_again() -> None:
    ctx = _fresh()
    _turn("find a meet about AI", ctx, events=[_BOOKS])
    reply, ctx, _ = _turn("Widen the search", ctx, events=[_BOOKS])
    assert ctx["activity_previews"] == []
    assert ctx["browse_draft"]["suggestions"] == ["Yes, listen for me", "Host a meet"]
    assert "anything close to it" in reply


def test_a_new_topic_after_widen_is_strict_again() -> None:
    ctx = _fresh()
    _turn("find a meet about AI", ctx, events=[_BOOKS, _PYTHON])
    _turn("Widen the search", ctx, events=[_BOOKS, _PYTHON])
    _turn("pottery", ctx, events=[_BOOKS, _PYTHON])
    assert not ctx["browse_draft"].get("_widen_related")


def test_the_location_pill_in_the_us_is_where_it_searches() -> None:
    ctx = _fresh()
    ctx["search_point"] = {"lat": 37.33, "lng": -121.88, "label": "San Jose, CA"}
    reply, ctx, fetch = _turn(
        "any events?", ctx, events=[_PYTHON],
        point_area={"block_id": "b-sanjose", "label": "San Jose, CA"},
    )
    assert fetch.call_args.args[1] == "b-sanjose"
    assert "San Jose, CA" in reply and "near you" not in reply


def test_outside_the_us_it_searches_home_and_says_so() -> None:
    ctx = _fresh()
    ctx["search_point"] = {"lat": 33.6, "lng": 73.0, "label": "Rawalpindi, PK"}
    reply, ctx, fetch = _turn("any events?", ctx, events=[_PYTHON], point_area=None)
    assert fetch.call_args.args[1] == "b-home"
    assert "your home area" in reply and "near you" not in reply


def test_the_pill_at_home_keeps_near_you() -> None:
    ctx = _fresh()
    ctx["search_point"] = {"lat": 28.4, "lng": -81.2, "label": "Lake Nona"}
    reply, ctx, fetch = _turn("any events?", ctx, events=[_PYTHON],
                              point_area={"block_id": "b-home", "label": "Lake Nona"})
    assert fetch.call_args.args[1] == "b-home"
    assert "near you" in reply


def test_point_area_reverse_geocodes_once_per_place() -> None:
    ctx: dict[str, Any] = {}
    with mock.patch("app.search_place._postal_code_at", return_value=("95113", "US")) as geo, \
         mock.patch("app.discovery_route.resolve_zip_coverage",
                    return_value=({"block_id": "b-sj", "display_name": "San Jose"}, "covered")):
        a = ab._point_area("jwt", {"lat": 37.331, "lng": -121.881, "label": None}, ctx)
        b = ab._point_area("jwt", {"lat": 37.332, "lng": -121.882, "label": None}, ctx)
    assert a == b == {"block_id": "b-sj", "label": "San Jose"}
    assert geo.call_count == 1
    with mock.patch("app.search_place._postal_code_at", return_value=(None, "PK")):
        assert ab._point_area("jwt", {"lat": 33.6, "lng": 73.0}, {}) is None


def test_the_message_model_takes_the_pill_point() -> None:
    from app.models import SendMessageRequest

    m = SendMessageRequest(message="hi", search_lat=33.6, search_lng=73.0,
                           search_label="Rawalpindi, PK")
    assert (m.search_lat, m.search_lng, m.search_label) == (33.6, 73.0, "Rawalpindi, PK")
    assert SendMessageRequest(message="hi").search_lat is None
