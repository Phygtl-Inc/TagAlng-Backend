"""A town stated in chat is the conversation's search area (prod 2026-10-07).

"I'm in <town>" was acknowledged, then the next browse searched the profile home and
labelled a meet 1,300 miles away as nearby; a peer search said "nothing nearby" measured
from home. The AI's `current_place` slot is now kept as `chat_area` and every later search
in the session reads it until another place (or a new home ZIP) replaces it.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

import app.activity_browse as ab
from app.chat_area import chat_area, note_chat_area

# Several towns, other states, and a non-English utterance — the slot carries the place,
# whatever the phrasing was.
_PLACES = [
    ("Austin", {"label": "Austin", "zip5": "78701", "lat": 30.27, "lng": -97.74}),
    ("Boulder", {"label": "Boulder", "zip5": "80302", "lat": 40.01, "lng": -105.27}),
    ("Ann Arbor", {"label": "Ann Arbor", "zip5": "48104", "lat": 42.28, "lng": -83.74}),
    ("Nueva York", {"label": "New York", "zip5": "10007", "lat": 40.71, "lng": -74.0}),
]


def _note(ctx: dict, place: str | None, got: Any, block: Any = "auto") -> mock.Mock:
    if block == "auto":
        block = ({"block_id": "b-" + str((got or {}).get("zip5")), "display_name": "x"}, "covered")
    with mock.patch("app.search_place.resolve_search_place", return_value=got) as geo, \
         mock.patch("app.discovery_route.resolve_zip_coverage", return_value=block):
        note_chat_area(ctx, {"current_place": place}, "jwt")
    return geo


@pytest.mark.parametrize("place,got", _PLACES)
def test_a_stated_town_becomes_the_session_area(place: str, got: dict) -> None:
    ctx: dict[str, Any] = {}
    _note(ctx, place, got)
    area = chat_area(ctx)
    assert area and area["block_id"] == "b-" + got["zip5"]
    assert area["label"] == got["label"] and area["lat"] == got["lat"]


def test_no_place_this_turn_keeps_the_area() -> None:
    ctx: dict[str, Any] = {}
    _note(ctx, "Austin", _PLACES[0][1])
    geo = _note(ctx, None, None)
    geo.assert_not_called()
    assert chat_area(ctx)["label"] == "Austin"


def test_the_same_town_again_is_not_geocoded_again() -> None:
    ctx: dict[str, Any] = {}
    _note(ctx, "Austin", _PLACES[0][1])
    assert _note(ctx, "austin", _PLACES[0][1]).call_count == 0


def test_a_new_town_replaces_the_old_one() -> None:
    ctx: dict[str, Any] = {}
    _note(ctx, "Austin", _PLACES[0][1])
    _note(ctx, "Boulder", _PLACES[1][1])
    assert chat_area(ctx)["label"] == "Boulder"


@pytest.mark.parametrize("got,block", [
    (None, "auto"),                                         # not a place
    ({"label": "Lahore", "zip5": None}, "auto"),           # outside the US
    ({"label": "Somewhere", "zip5": "99999"}, (None, "uncovered")),  # cannot place
])
def test_an_unplaceable_town_never_overwrites_the_area(got: Any, block: Any) -> None:
    ctx: dict[str, Any] = {}
    _note(ctx, "Austin", _PLACES[0][1])
    _note(ctx, "Elsewhere", got, block)
    assert chat_area(ctx)["label"] == "Austin"


# ── browse reads it ───────────────────────────────────────────────────────────────

_EV = {"id": "e1", "title": "Python Study Group", "distance_meters": 3000.0}


def _browse(ctx: dict, slots: dict | None = None) -> mock.Mock:
    fetch = mock.Mock(return_value=[dict(_EV)])
    patches = [
        mock.patch.object(ab, "_fetch_block_events", fetch),
        mock.patch.object(ab, "_fetch_admitted_events", return_value=None, create=True),
        mock.patch.object(ab, "_filter_events_by_query",
                          side_effect=lambda rows, q: (rows, q)),
        mock.patch.object(ab, "_zip_gate_frame", return_value=None),
        mock.patch.object(ab, "_far_offer", return_value=([], "", [])),
        mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False),
        mock.patch("app.orchestrator.llm.llm_configured", return_value=False),
        mock.patch("app.discovery_route._try_assign_home_block"),
    ]
    for p in patches:
        p.start()
    try:
        ab.run_activity_browse_turn(
            user_message="any events?", session_ctx=ctx, history=[], user_jwt="jwt",
            home_block_id="b-home", slots=slots or {},
        )
    finally:
        for p in patches:
            p.stop()
    return fetch


def _ctx(**extra: Any) -> dict:
    return {"activity_browse_active": True, "browse_draft": {"_asked": True},
            "phone_verified": True, **extra}


def test_browse_searches_the_stated_town_not_home() -> None:
    ctx = _ctx(chat_area={"label": "Austin", "zip5": "78701", "block_id": "b-austin"})
    assert _browse(ctx).call_args.args[1] == "b-austin"
    assert ctx["browse_draft"]["_place_name"] == "Austin"


def test_browse_without_a_stated_town_stays_home() -> None:
    assert _browse(_ctx()).call_args.args[1] == "b-home"


# ── peers read it ─────────────────────────────────────────────────────────────────

def test_peer_search_is_measured_from_the_stated_town() -> None:
    from app.discovery_route import _fetch_verified_peer_matches_unstamped

    ctx = {"chat_area": {"label": "Austin", "block_id": "b-a", "lat": 30.27, "lng": -97.74}}
    with mock.patch("app.peer_radius.fetch_peer_matches_near_point",
                    return_value=[{"user_id": "p1"}]) as near, \
         mock.patch("app.discovery_route.fetch_peer_matches_within_radius") as home, \
         mock.patch("app.discovery_route.kick_claim_embedding_backfill"), \
         mock.patch("app.onion_blend.blend_onion_matches", side_effect=lambda p, **k: p):
        peers = _fetch_verified_peer_matches_unstamped(
            "jwt", user_id="u1", block_id="b-home", session_ctx=ctx
        )
    assert peers == [{"user_id": "p1"}]
    assert near.call_args.kwargs["lat"] == 30.27
    home.assert_not_called()


def test_peer_search_without_a_stated_town_uses_home_radius() -> None:
    from app.discovery_route import _fetch_verified_peer_matches_unstamped

    with mock.patch("app.peer_radius.fetch_peer_matches_near_point") as near, \
         mock.patch("app.discovery_route.fetch_peer_matches_within_radius",
                    return_value=[]) as home, \
         mock.patch("app.discovery_route.kick_claim_embedding_backfill"), \
         mock.patch("app.onion_blend.blend_onion_matches", side_effect=lambda p, **k: p):
        _fetch_verified_peer_matches_unstamped("jwt", user_id="u1", block_id="b-home", session_ctx={})
    near.assert_not_called()
    home.assert_called_once()


def test_slot_parses_current_place() -> None:
    from app.discovery_slots import slots_current_place

    assert slots_current_place({"current_place": "  Boulder "}) == "Boulder"
    assert slots_current_place({"current_place": None}) is None
    assert slots_current_place(None) is None
