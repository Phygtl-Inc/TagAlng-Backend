"""Pouya / SJSU browse fixes, 2026-10-07.

1. Own meets reached the app as plain RSVP cards: `hosted_by_you` was stamped by
   _mark_own but never copied onto the wire row.
2. A card beyond the local radius must say where it is — never borrow a "near you" header.
3. Tapping "Look beyond <community>" / "Look in <area>" is an answer to Lana's own
   offer: it must not release the browse and wipe the draft holding the search.
4. After leaving the community, the club runner's own matching meet out past the radius
   is a real far match, not "nothing".
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.activity_browse as ab


# ── 1. hosted_by_you on the wire ──────────────────────────────────────────────────────


def test_hosted_by_you_reaches_the_wire_row() -> None:
    from app.main import _activity_previews_from_ctx

    rows = _activity_previews_from_ctx(
        {"activity_previews": [
            {"activity_id": "e1", "title": "SJSU Alumni Mixer", "hosted_by_you": True},
            {"activity_id": "e2", "title": "Book club"},
        ]}
    )
    assert [r.hosted_by_you for r in rows] == [True, False]


def test_the_prod_session_ships_its_cards() -> None:
    """The tester's session after "Widen the search" (2026-10-06): none of its leftover
    keys may outrank the browse, so the one card it produced is sent."""
    from app.ui_intent import UI_INTENT_SHOW_ACTIVITY_PREVIEW, derive_ui_intent

    ctx = {
        "active_intent": "discovery.find_activities",
        "activity_browse_active": True,
        "event_host_active": False,
        "tip_seek_pending": None,
        "tip_ask_offer": {"pending": True},
        "ask_draft": {"title": "Alumni events"},
        "peer_matches": [{"peer_user_id": "p1"}],
        "activity_previews": [{"title": "Pause Sip & Explore"}],
    }
    assert derive_ui_intent(ctx, activity_count=1) == UI_INTENT_SHOW_ACTIVITY_PREVIEW


# ── 2. far cards in a "near you" list name their area ────────────────────────────────


def _details(row: dict) -> dict:
    return {"title": row["title"], "miles": int(row["distance_meters"] / 1609.34),
            "area_label": {"far": "St. Cloud"}.get(row["id"], "?"), "zip5": "34771",
            "block_id": "zip-34771"}


def test_far_rows_in_a_near_list_name_their_area() -> None:
    rows = [
        {"id": "near", "title": "Board games", "distance_meters": 3_000.0,
         "venue_name": "Library"},
        {"id": "far", "title": "Pause Sip & Explore", "distance_meters": 3_900_000.0,
         "venue_name": "Pausa"},
        {"id": "unmeasured", "title": "Picnic", "venue_name": "Park"},
    ]
    with mock.patch("app.discovery_route.far_activity_details", side_effect=_details):
        ab._name_far_areas(rows)
    assert [r["venue_name"] for r in rows] == [
        "Library", "Pausa · St. Cloud (34771)", "Park",
    ]


def test_browse_turn_labels_a_far_card_under_the_near_header() -> None:
    ctx: dict[str, Any] = {"activity_browse_active": True,
                           "browse_draft": {"_asked": True}, "phone_verified": True}
    rows = [
        {"id": "near", "title": "Board games", "distance_meters": 3_000.0,
         "venue_name": "Library", "topic_score": 1.0},
        {"id": "far", "title": "Pause Sip & Explore", "distance_meters": 3_900_000.0,
         "venue_name": "Pausa", "topic_score": 1.0},
    ]
    with mock.patch.object(ab, "_fetch_block_events", return_value=rows), \
         mock.patch.object(ab, "_filter_events_by_query",
                           side_effect=lambda ev, q: (list(ev), None)), \
         mock.patch("app.discovery_route.far_activity_details", side_effect=_details), \
         mock.patch("app.event_place.event_community", return_value=None):
        ab.run_activity_browse_turn(
            user_message="anything", session_ctx=ctx, history=[],
            user_jwt="jwt", home_block_id="zip-94404",
        )
    venues = [p["venue_name"] for p in ctx["activity_previews"]]
    assert venues == ["Library", "Pausa · St. Cloud (34771)"]


# ── 3. Lana's own "Look beyond …" pill keeps the lane ───────────────────────────────


def _offer_ctx(chip_key: str, chip: str, rendered: bool = True) -> dict[str, Any]:
    return {
        "activity_browse_active": True,
        "browse_draft": {"interest": "alumni", "_seek_offer": True, chip_key: chip,
                         "suggestions": ["Yes, listen for me", chip]},
        "_offered_chip_msgs": ["Yes, listen for me", chip] if rendered else [],
    }


# What the classifier said when the tap released the lane: a confident abandon / pivot.
_ABANDON = {"abandon": True, "goal": "other", "linear_intent": "discovery.communities"}


def test_tapping_look_beyond_community_does_not_release() -> None:
    ctx = _offer_ctx("_community_chip", "Look beyond SJSU")
    assert ab.activity_browse_should_release("Look beyond SJSU", ctx, _ABANDON) is False


def test_tapping_look_in_area_does_not_release() -> None:
    ctx = _offer_ctx("_area_offer_chip", "Look in San Jose (95112)")
    assert (
        ab.activity_browse_should_release("Look in San Jose (95112)", ctx, _ABANDON)
        is False
    )


def test_a_chip_that_was_never_rendered_still_goes_to_the_classifier() -> None:
    # Same words, but no response carried the pill: typed text, so abandon wins.
    ctx = _offer_ctx("_community_chip", "Look beyond SJSU", rendered=False)
    assert ab.activity_browse_should_release("Look beyond SJSU", ctx, _ABANDON) is True


def test_host_a_meet_pill_still_leaves_browse() -> None:
    ctx = _offer_ctx("_community_chip", "Look beyond SJSU")
    ctx["browse_draft"]["suggestions"].append("Host a meet")
    ctx["_offered_chip_msgs"].append("Host a meet")
    host = {"goal": "host", "linear_intent": "event.host", "confidence": 0.95}
    assert ab.activity_browse_should_release("Host a meet", ctx, host) is True


# ── 4. the caller's own far meet is a real match ────────────────────────────────────


def test_far_offer_keeps_the_callers_own_meet_and_marks_it() -> None:
    own = {"id": "alumni", "title": "SJSU Alumni Mixer", "host_id": "me",
           "distance_meters": 45_000.0, "venue_name": "Student Union", "topic_score": 1.0}
    draft: dict = {}
    beyond = mock.Mock(return_value=[dict(own)])
    with mock.patch("app.discovery_route.activities_beyond_radius", beyond), \
         mock.patch.object(ab, "_filter_events_by_query",
                           side_effect=lambda rows, q: (list(rows), "alumni events")), \
         mock.patch("app.discovery_route.far_activity_details",
                    return_value={"title": "SJSU Alumni Mixer", "miles": 28,
                                  "area_label": "San Jose", "zip5": "95112",
                                  "block_id": "zip-95112"}), \
         mock.patch("app.auth.jwt_user_id", return_value="me"):
        facts, chip, cards = ab._far_offer("jwt", "zip-94404", draft, interest="alumni")
    assert "exclude_host_id" not in beyond.call_args.kwargs
    assert [c["id"] for c in cards] == ["alumni"]
    assert cards[0]["hosted_by_you"] is True
    assert "they host this one themselves" in " ".join(facts)
