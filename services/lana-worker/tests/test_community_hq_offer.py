"""The run-from city step offers the user's own area (Asjid, 2026-10-09).

"Which city is it run from?" came with no control at all: a neighbour in 10451 still had
to type "New York". The ask now carries their ZIP's city as a chip, and a tap on it is
placed from the coordinates already looked up rather than geocoded again.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_capture as cc

_NYC = {"city": "New York, NY", "lat": 40.82, "lng": -73.93}


def _patches(monkeypatch: Any, geo: Any) -> mock.Mock:
    monkeypatch.setattr(cc, "compose_reply", lambda *, goal, facts, fallback, **k: fallback)
    monkeypatch.setattr(cc, "_link_check", lambda *a, **k: {"status": "error"})
    geocode = mock.Mock(side_effect=geo if callable(geo) else (lambda text: geo))
    monkeypatch.setattr("app.community_hq.geocode_city", geocode)
    return geocode


def _ctx(**extra: Any) -> dict[str, Any]:
    return {
        "community_create_active": True,
        "community_draft": {"name": "Music lovers", "circle_type": "hobby", "draft_id": "d1"},
        **extra,
    }


def _ask(ctx: dict[str, Any]) -> None:
    cc._after_questions(draft=ctx["community_draft"], session_ctx=ctx, user_id="u1")


def _turn(msg: str, ctx: dict[str, Any]) -> str:
    return cc.run_community_capture_turn(
        user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt",
        user_id="u1", home_block_id=None,
    )


def test_the_city_ask_offers_their_own_area(monkeypatch: Any) -> None:
    geocode = _patches(monkeypatch, _NYC)
    ctx = _ctx(zip_code="10451")
    _ask(ctx)
    d = ctx["community_draft"]
    assert ctx["community_pending_ask"] == "hq"
    assert d["suggestions"] == ["New York, NY"]
    assert ctx["community_offered"] == ["New York, NY"]
    geocode.assert_called_once_with("10451, USA")


def test_tapping_the_area_places_it_without_a_second_lookup(monkeypatch: Any) -> None:
    geocode = _patches(monkeypatch, _NYC)
    ctx = _ctx(zip_code="10451")
    _ask(ctx)
    _turn("New York, NY", ctx)
    d = ctx["community_draft"]
    assert (d["hq_city"], d["hq_lat"], d["hq_lng"]) == ("New York, NY", 40.82, -73.93)
    assert geocode.call_count == 1  # only the ask's own lookup
    # The chip answered its question; it does not ride on to the next card.
    assert d["suggestions"] == [] and ctx["community_offered"] == []


def test_another_city_is_still_looked_up(monkeypatch: Any) -> None:
    lisbon = {"city": "Lisbon, Portugal", "lat": 38.72, "lng": -9.14}
    geocode = _patches(monkeypatch, lambda text: _NYC if text == "10451, USA" else lisbon)
    ctx = _ctx(zip_code="10451")
    _ask(ctx)
    _turn("Lisbon", ctx)
    assert ctx["community_draft"]["hq_city"] == "Lisbon, Portugal"
    assert [c.args[0] for c in geocode.call_args_list] == ["10451, USA", "Lisbon"]


def test_no_zip_means_no_chip(monkeypatch: Any) -> None:
    geocode = _patches(monkeypatch, _NYC)
    ctx = _ctx()
    _ask(ctx)
    assert ctx["community_draft"]["suggestions"] == []
    geocode.assert_not_called()


def test_an_unplaceable_zip_means_no_chip(monkeypatch: Any) -> None:
    _patches(monkeypatch, None)
    ctx = _ctx(zip_code="10451")
    _ask(ctx)
    assert ctx["community_draft"]["suggestions"] == []


def test_the_area_is_looked_up_once_per_draft(monkeypatch: Any) -> None:
    geocode = _patches(monkeypatch, _NYC)
    ctx = _ctx(zip_code="10451")
    _ask(ctx)
    _ask(ctx)  # asked again (e.g. the city was reopened from the ready card)
    assert geocode.call_count == 1
