"""Browse fixes, 2026-10-07 — general across communities, chip labels and areas.

1. Own meets reached the app as plain RSVP cards: `hosted_by_you` was stamped by
   _mark_own but never copied onto the wire row.
2. A card beyond the local radius must say where it is — never borrow a "near you" header.
3. Tapping a "Look beyond <community>" / "Look in <area>" pill Lana rendered is an answer
   to her own offer: it must not release the browse and wipe the draft holding the search.
4. After leaving a community, the caller's own matching meet out past the radius is a
   real far match, not "nothing".
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

import app.activity_browse as ab

# (community chip, area chip) pairs — deliberately varied, none from QA scripts.
_CHIPS = [
    ("Look beyond Riverside Chess Club", "Look in Boise (83702)"),
    ("Look beyond Harbor Knitters", "Look in Portland (04101)"),
    ("Look beyond Northside Cyclists", "Look in Tucson (85701)"),
]
_COMMUNITY_CHIPS = [c for c, _ in _CHIPS]
_AREA_CHIPS = [a for _, a in _CHIPS]

# (far area label, zip) for far-card labelling.
_AREAS = [("Duluth", "55802"), ("Asheville", "28801"), ("Bend", "97701")]


# ── 1. hosted_by_you on the wire ──────────────────────────────────────────────────────


def test_hosted_by_you_reaches_the_wire_row() -> None:
    from app.main import _activity_previews_from_ctx

    rows = _activity_previews_from_ctx(
        {"activity_previews": [
            {"activity_id": "e1", "title": "Pottery Open Studio", "hosted_by_you": True},
            {"activity_id": "e2", "title": "Book club"},
        ]}
    )
    assert [r.hosted_by_you for r in rows] == [True, False]


def test_a_session_with_leftover_flags_still_ships_its_cards() -> None:
    """Leftover keys from earlier lanes may not outrank an active browse."""
    from app.ui_intent import UI_INTENT_SHOW_ACTIVITY_PREVIEW, derive_ui_intent

    ctx = {
        "active_intent": "discovery.find_activities",
        "activity_browse_active": True,
        "event_host_active": False,
        "tip_seek_pending": None,
        "tip_ask_offer": {"pending": True},
        "ask_draft": {"title": "Board game nights"},
        "peer_matches": [{"peer_user_id": "p1"}],
        "activity_previews": [{"title": "Trivia night"}],
    }
    assert derive_ui_intent(ctx, activity_count=1) == UI_INTENT_SHOW_ACTIVITY_PREVIEW


# ── 2. far cards in a "near you" list name their area ────────────────────────────────


def _details_for(area: str, zip5: str):
    def _details(row: dict) -> dict:
        return {"title": row["title"], "miles": int(row["distance_meters"] / 1609.34),
                "area_label": area, "zip5": zip5, "block_id": f"zip-{zip5}"}
    return _details


@pytest.mark.parametrize("area,zip5", _AREAS)
@pytest.mark.parametrize("far_m", [90_000.0, 3_900_000.0])  # just past radius, far away
def test_far_rows_in_a_near_list_name_their_area(area: str, zip5: str, far_m: float) -> None:
    rows = [
        {"id": "near", "title": "Board games", "distance_meters": 3_000.0,
         "venue_name": "Library"},
        {"id": "far", "title": "Sunset hike", "distance_meters": far_m,
         "venue_name": "Trailhead"},
        {"id": "unmeasured", "title": "Picnic", "venue_name": "Park"},
        {"id": "no_venue", "title": "Run club", "distance_meters": far_m},
    ]
    with mock.patch("app.discovery_route.far_activity_details",
                    side_effect=_details_for(area, zip5)):
        ab._name_far_areas(rows)
    where = f"{area} ({zip5})"
    assert [r.get("venue_name") for r in rows] == [
        "Library", f"Trailhead · {where}", "Park", where,
    ]


@pytest.mark.parametrize("area,zip5", _AREAS)
def test_browse_turn_labels_a_far_card_under_the_near_header(area: str, zip5: str) -> None:
    ctx: dict[str, Any] = {"activity_browse_active": True,
                           "browse_draft": {"_asked": True}, "phone_verified": True}
    rows = [
        {"id": "near", "title": "Board games", "distance_meters": 3_000.0,
         "venue_name": "Library", "topic_score": 1.0},
        {"id": "far", "title": "Sunset hike", "distance_meters": 3_900_000.0,
         "venue_name": "Trailhead", "topic_score": 1.0},
    ]
    with mock.patch.object(ab, "_fetch_block_events", return_value=rows), \
         mock.patch.object(ab, "_filter_events_by_query",
                           side_effect=lambda ev, q: (list(ev), None)), \
         mock.patch("app.discovery_route.far_activity_details",
                    side_effect=_details_for(area, zip5)), \
         mock.patch("app.event_place.event_community", return_value=None):
        ab.run_activity_browse_turn(
            user_message="anything", session_ctx=ctx, history=[],
            user_jwt="jwt", home_block_id="zip-10001",
        )
    venues = [p["venue_name"] for p in ctx["activity_previews"]]
    assert venues == ["Library", f"Trailhead · {area} ({zip5})"]


# ── 3. Lana's own "Look beyond …" / "Look in …" pill keeps the lane ──────────────────


def _offer_ctx(chip_key: str, chip: str, rendered: bool = True) -> dict[str, Any]:
    return {
        "activity_browse_active": True,
        "browse_draft": {"interest": "board games", "_seek_offer": True, chip_key: chip,
                         "suggestions": ["Yes, listen for me", chip]},
        "_offered_chip_msgs": ["Yes, listen for me", chip] if rendered else [],
    }


# What the classifier said when the tap released the lane: a confident abandon / pivot.
_ABANDON = {"abandon": True, "goal": "other", "linear_intent": "discovery.communities"}


@pytest.mark.parametrize("chip", _COMMUNITY_CHIPS)
def test_tapping_look_beyond_community_does_not_release(chip: str) -> None:
    ctx = _offer_ctx("_community_chip", chip)
    assert ab.activity_browse_should_release(chip, ctx, _ABANDON) is False


@pytest.mark.parametrize("chip", _AREA_CHIPS)
def test_tapping_look_in_area_does_not_release(chip: str) -> None:
    ctx = _offer_ctx("_area_offer_chip", chip)
    assert ab.activity_browse_should_release(chip, ctx, _ABANDON) is False


@pytest.mark.parametrize("chip", _COMMUNITY_CHIPS + _AREA_CHIPS)
def test_a_chip_that_was_never_rendered_still_goes_to_the_classifier(chip: str) -> None:
    # Same words, but no response carried the pill: typed text, so abandon wins.
    key = "_community_chip" if chip in _COMMUNITY_CHIPS else "_area_offer_chip"
    ctx = _offer_ctx(key, chip, rendered=False)
    assert ab.activity_browse_should_release(chip, ctx, _ABANDON) is True


@pytest.mark.parametrize("chip,other", [
    (_COMMUNITY_CHIPS[0], _COMMUNITY_CHIPS[1]),
    (_AREA_CHIPS[1], _AREA_CHIPS[2]),
])
def test_a_rendered_chip_for_a_different_offer_does_not_hold(chip: str, other: str) -> None:
    # The pill was rendered, but the draft now offers a different one: classifier decides.
    key = "_community_chip" if chip in _COMMUNITY_CHIPS else "_area_offer_chip"
    ctx = _offer_ctx(key, other)
    ctx["_offered_chip_msgs"].append(chip)
    assert ab.activity_browse_should_release(chip, ctx, _ABANDON) is True


@pytest.mark.parametrize("chip", _COMMUNITY_CHIPS)
def test_host_a_meet_pill_still_leaves_browse(chip: str) -> None:
    ctx = _offer_ctx("_community_chip", chip)
    ctx["browse_draft"]["suggestions"].append("Host a meet")
    ctx["_offered_chip_msgs"].append("Host a meet")
    host = {"goal": "host", "linear_intent": "event.host", "confidence": 0.95}
    assert ab.activity_browse_should_release("Host a meet", ctx, host) is True


# ── 4. the caller's own far meet is a real match ────────────────────────────────────


@pytest.mark.parametrize("title,interest,area,zip5", [
    ("Chess Club Blitz Night", "chess", "Duluth", "55802"),
    ("Knitting Circle Meetup", "knitting", "Asheville", "28801"),
    ("Gravel Ride", "cycling", "Bend", "97701"),
])
def test_far_offer_keeps_the_callers_own_meet_and_marks_it(
    title: str, interest: str, area: str, zip5: str
) -> None:
    own = {"id": "own", "title": title, "host_id": "me",
           "distance_meters": 45_000.0, "venue_name": "Hall", "topic_score": 1.0}
    draft: dict = {}
    beyond = mock.Mock(return_value=[dict(own)])
    with mock.patch("app.discovery_route.activities_beyond_radius", beyond), \
         mock.patch.object(ab, "_filter_events_by_query",
                           side_effect=lambda rows, q: (list(rows), interest)), \
         mock.patch("app.discovery_route.far_activity_details",
                    return_value={"title": title, "miles": 28, "area_label": area,
                                  "zip5": zip5, "block_id": f"zip-{zip5}"}), \
         mock.patch("app.auth.jwt_user_id", return_value="me"):
        facts, chip, cards = ab._far_offer("jwt", "zip-10001", draft, interest=interest)
    assert "exclude_host_id" not in beyond.call_args.kwargs
    assert [c["id"] for c in cards] == ["own"]
    assert cards[0]["hosted_by_you"] is True
    assert "they host this one themselves" in " ".join(facts)


# ── 5. An empty search keeps its TOPIC, not the filter's when-only label ─────────────
#
# The filter's short label can be only the when ("this week"). Storing it as the topic
# lost the subject, so a "Look beyond" tap re-ran on the date and offered every far meet
# that week as a match (e2e 2026-10-08).

_TOPIC_LABELS = [
    ("salsa dancing", "this week"),
    ("chess", "this weekend"),
    ("pottery class", "Saturday"),
]


@pytest.mark.parametrize("topic,label", _TOPIC_LABELS)
def test_empty_search_keeps_the_topic_over_a_when_label(topic: str, label: str) -> None:
    ctx: dict[str, Any] = {"activity_browse_active": True,
                           "browse_draft": {"_asked": True}, "phone_verified": True}
    rows = [{"id": "x", "title": "Board games", "distance_meters": 3_000.0,
             "venue_name": "Library", "topic_score": 1.0}]
    far_calls: list[str] = []

    def _far(_jwt: Any, _block: Any, _draft: Any, *, interest: str) -> tuple:
        far_calls.append(interest)
        return [], "", []

    with mock.patch.object(ab, "_topic_from_slots", return_value=topic), \
         mock.patch.object(ab, "_fetch_block_events", return_value=rows), \
         mock.patch.object(ab, "_filter_events_by_query", return_value=([], label)), \
         mock.patch.object(ab, "_far_offer", side_effect=_far), \
         mock.patch("app.event_place.event_community", return_value=None):
        ab.run_activity_browse_turn(
            user_message=f"any {topic} {label}?", session_ctx=ctx, history=[],
            user_jwt="jwt", home_block_id="zip-10001", slots={"activity_topic": topic},
        )
    assert ctx["browse_draft"]["interest"] == topic
    assert far_calls == [topic]


def test_echo_topic_falls_back_to_the_label_only_for_a_long_interest() -> None:
    long_ask = "are there any fun things for my six year old to do"
    assert ab._echo_topic(long_ask, "kids activities") == "kids activities"
    assert ab._echo_topic("board games", "tonight") == "board games"
