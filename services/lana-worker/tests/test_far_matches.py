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


# ── Travel: search a town that is not where they are ─────────────────────────────────


def test_the_place_slot_is_read_from_the_ai() -> None:
    from app.discovery_slots import slots_search_place

    assert slots_search_place({"search_place": " San Jose "}) == "San Jose"
    assert slots_search_place({"search_place": None}) is None


def test_a_us_city_resolves_to_its_zip_and_abroad_does_not() -> None:
    from app import search_place as sp

    with mock.patch("app.community_hq.geocode_city",
                    return_value={"city": "San Jose, CA", "lat": 37.3, "lng": -121.9}), \
         mock.patch.object(sp, "_postal_code_at", return_value=("95113", "US")):
        assert sp.resolve_search_place("San Jose") == {
            "label": "San Jose, CA", "zip5": "95113", "lat": 37.3, "lng": -121.9,
        }
    with mock.patch("app.community_hq.geocode_city",
                    return_value={"city": "Lisbon, Portugal", "lat": 38.7, "lng": -9.1}), \
         mock.patch.object(sp, "_postal_code_at", return_value=("11000", "PT")):
        assert sp.resolve_search_place("Lisbon")["zip5"] is None
    with mock.patch("app.community_hq.geocode_city", return_value=None):
        assert sp.resolve_search_place("asdfgh") is None


def _travel_turn(*, place: dict | None, events: list[dict], block: dict | None = None,
                 ctx: dict | None = None) -> tuple[str, dict, mock.Mock, mock.Mock]:
    ctx = ctx if ctx is not None else {"activity_browse_active": True,
                                       "browse_draft": {"_asked": True},
                                       "phone_verified": True}
    fetch = mock.Mock(return_value=list(events))
    assign = mock.Mock()

    def filt(rows: list[dict], q: str) -> tuple[list[dict], str]:
        for r in rows:
            r["topic_score"] = 1.0
        return list(rows), "language exchange"

    with mock.patch("app.search_place.resolve_search_place", return_value=place), \
         mock.patch("app.discovery_route.resolve_zip_coverage",
                    return_value=(block, "covered")), \
         mock.patch.object(ab, "_fetch_block_events", fetch), \
         mock.patch.object(ab, "_fetch_admitted_events", return_value=None, create=True), \
         mock.patch.object(ab, "_filter_events_by_query", side_effect=filt), \
         mock.patch.object(ab, "_zip_gate_frame", return_value=None), \
         mock.patch.object(ab, "_far_offer", return_value=([], "", [])), \
         mock.patch("app.discovery_route._try_assign_home_block", assign), \
         mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False), \
         mock.patch("app.orchestrator.llm.llm_configured", return_value=False), \
         mock.patch("app.reply_compose.compose_reply",
                    side_effect=lambda *, goal, facts, fallback, **k: fallback):
        reply = ab.run_activity_browse_turn(
            user_message="language events in San Jose", session_ctx=ctx, history=[],
            user_jwt="jwt", home_block_id="b-home", slots={"search_place": "San Jose"},
        )
    return reply, ctx, fetch, assign


def test_a_town_they_ask_about_is_searched_there_not_at_home() -> None:
    ev = {"id": "e1", "title": "Language Exchange Club", "distance_meters": 800.0}
    reply, ctx, fetch, assign = _travel_turn(
        place={"label": "San Jose, CA", "zip5": "95113"}, events=[ev],
        block={"block_id": "b-sanjose", "display_name": "San Jose"},
    )
    assert fetch.call_args.args[1] == "b-sanjose"
    assert "in San Jose, CA" in reply and "near you" not in reply
    # Travel never moves their home, or the session's own area.
    assign.assert_not_called()
    assert ctx.get("preview_block_id") is None


def test_nothing_there_says_there_not_near_you() -> None:
    reply, _ctx, _f, _a = _travel_turn(
        place={"label": "San Jose, CA", "zip5": "95113"}, events=[],
        block={"block_id": "b-sanjose", "display_name": "San Jose"},
    )
    assert "in San Jose, CA" in reply and "near you" not in reply


def test_a_place_outside_the_us_is_said_plainly_and_home_is_not_searched() -> None:
    reply, _ctx, fetch, _a = _travel_turn(
        place={"label": "Lisbon, Portugal", "zip5": None}, events=[], block=None,
    )
    assert "Lisbon, Portugal" in reply and "only in the US" in reply
    fetch.assert_not_called()


# ── After "Widen the search", far meets are judged for RELATED too (prod 2026-10-08) ─────
# From a Bronx account nothing was near at all, and the far probe only accepted exact
# matches, so "jazz" -> Widen could never reach a guitar jam in Orlando.

_FAR = [
    {"id": "guitar", "title": "Neighborhood Guitar Meetup", "distance_meters": 1_500_000.0,
     "venue_name": "Guitar Center", "hosted_by_you": False},
    {"id": "books", "title": "Books & Neighbors Gathering", "distance_meters": 1_510_000.0,
     "venue_name": "Library"},
]


def _far_widen(*, related: bool, picks: list[str] | None, exact: list[str] = ()):
    draft: dict = {}

    def stamp(rows, _q):
        for r in rows:
            r["topic_score"] = 1.0 if r["id"] in exact else 0.0
        return [r for r in rows if r["id"] in exact], "jazz"

    rel = mock.Mock(side_effect=lambda rows, req: (
        None if picks is None else [r for r in rows if r["id"] in picks]))
    with mock.patch("app.discovery_route.activities_beyond_radius",
                    return_value=[dict(r) for r in _FAR]), \
         mock.patch.object(ab, "_filter_events_by_query", side_effect=stamp), \
         mock.patch.object(ab, "_related_alternatives", rel), \
         mock.patch("app.discovery_route.far_activity_details",
                    side_effect=lambda row: {"title": row["title"], "venue": row.get("venue_name"),
                                             "miles": int(row["distance_meters"] / 1609.34),
                                             "area_label": "Orlando", "zip5": None,
                                             "block_id": "b-orl"}), \
         mock.patch("app.auth.jwt_user_id", return_value="me"):
        facts, chip, cards = ab._far_offer("jwt", "zip-10451", draft, interest="jazz",
                                           related=related, request="any jazz related events")
    return facts, chip, cards, draft, rel


def test_widened_far_probe_offers_related_meets_as_related() -> None:
    facts, chip, cards, draft, rel = _far_widen(related=True, picks=["guitar"])
    assert rel.call_args.args[1] == "any jazz related events"
    assert [c["id"] for c in cards] == ["guitar"]
    assert draft["_far_lead"]["related"] is True
    text = " ".join(facts)
    assert "RELATED" in text and "NOT jazz itself" in text and "Never offer to widen again" in text
    assert chip == ""


def test_without_widen_the_far_probe_still_accepts_only_exact_matches() -> None:
    facts, chip, cards, draft, rel = _far_widen(related=False, picks=["guitar"])
    rel.assert_not_called()
    assert (facts, chip, cards) == ([], "", [])


def test_an_exact_far_match_wins_and_is_never_called_related() -> None:
    facts, _chip, cards, draft, rel = _far_widen(related=True, picks=["guitar"], exact=["books"])
    rel.assert_not_called()
    assert [c["id"] for c in cards] == ["books"]
    assert "related" not in draft["_far_lead"]


def test_a_failed_related_call_offers_nothing_rather_than_guessing() -> None:
    facts, chip, cards, _d, _rel = _far_widen(related=True, picks=None)
    assert (facts, chip, cards) == ([], "", [])


def test_the_no_model_fallback_says_related_not_match() -> None:
    lead = {"title": "Neighborhood Guitar Meetup", "area_label": "Orlando", "miles": 932,
            "related": True}
    with mock.patch("app.orchestrator.llm.llm_configured", return_value=False), \
         mock.patch.object(ab, "_far_where", return_value="Orlando"):
        out = ab._compose_empty_seek_offer("jazz", far_lead=lead, far_facts=["x"])
    assert "related" in out.lower() and "Neighborhood Guitar Meetup" in out


# ── The reply names the far meet it sits above (prod 2026-10-08) ─────────────────────────
_LEAD = {"title": "Neighborhood Guitar Meetup", "area_label": "Orlando", "miles": 956,
         "related": True}


def _compose(model_message: str):
    seen = {}

    def llm(**kw):
        seen.update(kw)
        return {"message": model_message}

    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=llm), \
            mock.patch.object(ab, "_far_where", return_value="Orlando"):
        out = ab._compose_empty_seek_offer("jazz", far_lead=_LEAD, far_facts=["far fact"])
    return out, seen


def test_a_reply_that_drops_the_far_meet_falls_back_to_one_that_names_it() -> None:
    out, _ = _compose("Nothing on jazz near you right now — want me to listen for one?")
    assert "Neighborhood Guitar Meetup" in out and "956" in out


def test_a_reply_that_names_the_far_meet_is_kept() -> None:
    msg = "No jazz near you; the closest related meet is the Neighborhood Guitar Meetup, 956 miles away."
    out, seen = _compose(msg)
    assert out == msg
    assert "name the one farther-away meet" in seen["system"]
    assert "the ONLY events you may name are the farther-away ones" in seen["user_payload"]


def test_without_a_far_meet_the_old_instruction_and_rule_stand() -> None:
    seen = {}

    def llm(**kw):
        seen.update(kw)
        return {"message": "Nothing near you — want me to listen?"}

    with mock.patch("app.orchestrator.llm.llm_configured", return_value=True), \
            mock.patch("app.orchestrator.llm.llm_json", side_effect=llm):
        out = ab._compose_empty_seek_offer("jazz", far_facts=["x"])
    assert out == "Nothing near you — want me to listen?"
    assert "farther-away" not in seen["system"]
    assert "Never invent or promise events, and never claim" in seen["user_payload"]
