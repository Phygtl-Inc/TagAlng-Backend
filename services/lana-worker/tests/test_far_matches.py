"""Nothing near you, something farther away: show the best matches, ranked, as cards.

Tommaso, 2026-10-06 (The Villages, FL, asking for language gatherings): Lana said there was
nothing near and offered ONE pill, "Look in San Jose (95138)". With several areas holding
a match a pill can only name one, and it hid the actual events behind another tap. Now the
best few matches come back as cards — exact topic first, then nearest — and the copy names
the closest with its distance. Without a topic ("anything on?") the single-area pill stays.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.activity_browse as ab

_ROWS = [
    {"id": "near_loose", "title": "Coffee & conversation", "distance_meters": 1_500_000.0,
     "venue_name": "Cafe"},
    {"id": "far_exact", "title": "Language Exchange Club", "distance_meters": 3_900_000.0,
     "venue_name": "King Library"},
    {"id": "mid_exact", "title": "Spanish Conversation Night", "distance_meters": 2_000_000.0,
     "venue_name": "Community Hall"},
    {"id": "farthest_exact", "title": "Polyglot Meetup", "distance_meters": 4_500_000.0,
     "venue_name": None},
]
_SCORES = {"near_loose": 0.8, "far_exact": 1.0, "mid_exact": 0.9, "farthest_exact": 0.9}
_AREAS = {"near_loose": "Atlanta", "far_exact": "San Jose", "mid_exact": "Austin",
          "farthest_exact": "Seattle"}


def _stamp(rows: list[dict], _q: str) -> tuple[list[dict], str]:
    for r in rows:
        r["topic_score"] = _SCORES[r["id"]]
    return list(rows), "language exchange"


def _details(row: dict) -> dict:
    return {"title": row["title"], "venue": row.get("venue_name"),
            "miles": int(round(row["distance_meters"] / 1609.34)),
            "area_label": _AREAS[row["id"]], "zip5": None, "block_id": f"b-{row['id']}"}


def _offer(interest: str = "language exchange") -> tuple[list, str, list, dict]:
    draft: dict = {}
    with mock.patch("app.discovery_route.activities_beyond_radius",
                    return_value=[dict(r) for r in _ROWS]), \
         mock.patch.object(ab, "_filter_events_by_query", side_effect=_stamp), \
         mock.patch("app.discovery_route.far_activity_details", side_effect=_details), \
         mock.patch("app.auth.jwt_user_id", return_value="me"):
        facts, chip, cards = ab._far_offer("jwt", "b-home", draft, interest=interest)
    return facts, chip, cards, draft


def test_rank_is_topic_first_then_distance() -> None:
    rows = [dict(r, topic_score=_SCORES[r["id"]]) for r in _ROWS]
    ids = [r["id"] for r in ab._rank_far_matches(rows)]
    # Exact matches nearest-first, then the merely related one — even though it is the
    # closest of all.
    assert ids == ["mid_exact", "far_exact", "farthest_exact", "near_loose"]


def test_a_topic_ask_returns_the_best_three_as_cards_not_one_pill() -> None:
    facts, chip, cards, draft = _offer()
    assert chip == ""
    assert [c["id"] for c in cards] == ["mid_exact", "far_exact", "farthest_exact"]
    # No single-area re-anchor is armed: the cards ARE the answer.
    assert draft.get("_area_offer_chip") is None
    assert draft["_far_lead"]["title"] == "Spanish Conversation Night"
    # Each card says where it is — the venue alone hides that it is far away.
    assert cards[0]["venue_name"] == "Community Hall · Austin"
    assert cards[2]["venue_name"] == "Seattle"
    joined = " ".join(facts)
    assert "Name ONLY the first" in joined and "about 1,243 miles away" in joined
    assert "no 'look in' or 'widen' option" in joined


def test_without_a_topic_the_single_area_pill_stays() -> None:
    facts, chip, cards, draft = _offer(interest="")
    assert cards == []
    assert chip.startswith("Look in ")
    assert draft["_area_offer_chip"] == chip


def test_unchecked_rows_offer_nothing() -> None:
    draft: dict = {}
    with mock.patch("app.discovery_route.activities_beyond_radius",
                    return_value=[dict(r) for r in _ROWS]), \
         mock.patch.object(ab, "_filter_events_by_query", return_value=([], "")), \
         mock.patch("app.auth.jwt_user_id", return_value="me"):
        assert ab._far_offer("jwt", "b-home", draft, interest="x") == ([], "", [])


def test_the_turn_shows_the_cards_and_offers_only_to_listen() -> None:
    ctx: dict[str, Any] = {"activity_browse_active": True,
                           "browse_draft": {"_asked": True}, "phone_verified": True}
    near_rows = [{"id": "n1", "title": "Book club", "distance_meters": 3000.0}]

    def filt(rows: list[dict], q: str) -> tuple[list[dict], str]:
        if rows and rows[0]["id"] == "n1":
            for r in rows:
                r["topic_score"] = 0.1
            return [], "language exchange"
        return _stamp(rows, q)

    with mock.patch.object(ab, "_fetch_block_events", return_value=list(near_rows)), \
         mock.patch.object(ab, "_fetch_admitted_events", return_value=None, create=True), \
         mock.patch.object(ab, "_filter_events_by_query", side_effect=filt), \
         mock.patch.object(ab, "_zip_gate_frame", return_value=None), \
         mock.patch("app.discovery_route.activities_beyond_radius",
                    return_value=[dict(r) for r in _ROWS]), \
         mock.patch("app.discovery_route.far_activity_details", side_effect=_details), \
         mock.patch("app.auth.jwt_user_id", return_value="me"), \
         mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False), \
         mock.patch("app.orchestrator.llm.llm_configured", return_value=False):
        reply = ab.run_activity_browse_turn(
            user_message="language exchange", session_ctx=ctx, history=[],
            user_jwt="jwt", home_block_id="b-home",
        )
    assert [p["title"] for p in ctx["activity_previews"]] == [
        "Spanish Conversation Night", "Language Exchange Club", "Polyglot Meetup"]
    assert ctx["browse_draft"]["suggestions"] == ["Yes, listen for me"]
    # The closest exact match, its area and distance — never a bare ZIP pill.
    assert "Spanish Conversation Night" in reply and "Austin" in reply
    assert "1,243 miles" in reply
    assert "Look in" not in reply


def test_the_far_probe_reads_a_full_page() -> None:
    from app import discovery_route as dr

    with mock.patch.object(dr, "block_centroid", return_value=(28.9, -82.0)), \
         mock.patch.object(dr, "activity_radius_meters", return_value=40000.0), \
         mock.patch("app.supabase_rpc.call_rpc", return_value=[]) as rpc:
        dr.activities_beyond_radius("jwt", "b-home")
    assert rpc.call_args.args[2]["p_limit"] == 50
