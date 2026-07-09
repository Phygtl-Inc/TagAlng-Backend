import unittest
from unittest.mock import patch

from app.activity_browse import (
    activity_browse_should_release,
    reset_activity_browse_state,
    run_activity_browse_turn,
)
from app.discovery_route import (
    _browse_or_seek_decision,
    _resolve_browse_or_meet_answer,
    handle_discovery_turn,
)


class TestBrowseSeekDecision(unittest.TestCase):
    def test_browse(self) -> None:
        self.assertEqual(
            _browse_or_seek_decision({"goal": "activities", "confidence": 0.9}, "what's happening"),
            "browse",
        )

    def test_clear_meet_seek_not_diverted(self) -> None:
        # A confident meet_seek is left to the existing signal-capture flow (None here).
        self.assertIsNone(
            _browse_or_seek_decision(
                {"goal": "save_signal", "signal_intent": "meet_seek",
                 "linear_intent": "looking.meet", "confidence": 0.9},
                "find me a tennis partner",
            )
        )

    def test_clarify_when_model_torn(self) -> None:
        self.assertEqual(
            _browse_or_seek_decision(
                {"goal": "activities", "confidence": 0.9, "clarify": "browse_or_meet"},
                "something fun this weekend",
            ),
            "clarify",
        )

    def test_clarify_when_low_confidence(self) -> None:
        self.assertEqual(
            _browse_or_seek_decision({"goal": "activities", "confidence": 0.4}, "something fun"),
            "clarify",
        )

    def test_none_for_other_intent(self) -> None:
        self.assertIsNone(_browse_or_seek_decision({"goal": "peers", "confidence": 0.9}, "find moms"))


class TestBrowseRelease(unittest.TestCase):
    def test_refine_to_another_activity_stays(self) -> None:
        # "show me cricket instead" is a re-filter, not a pivot.
        self.assertFalse(
            activity_browse_should_release(
                "show me cricket ones instead",
                {"activity_browse_active": True},
                {"goal": "activities", "confidence": 0.9},
            )
        )

    def test_semantic_abandon_releases(self) -> None:
        self.assertTrue(
            activity_browse_should_release(
                "eh i have mixed feelings about this",
                {"activity_browse_active": True},
                {"abandon": True, "confidence": 0.7},
            )
        )

    def test_cross_lane_pivot_releases(self) -> None:
        self.assertTrue(
            activity_browse_should_release(
                "actually connect me with neighbors like me",
                {"activity_browse_active": True},
                {"goal": "peers", "confidence": 0.8},
            )
        )

    def test_switch_to_meet_releases(self) -> None:
        self.assertTrue(
            activity_browse_should_release(
                "actually i want to meet people for this",
                {"activity_browse_active": True},
                {"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.8},
            )
        )


class TestResolveClarifier(unittest.TestCase):
    def test_resolve_seek(self) -> None:
        self.assertEqual(_resolve_browse_or_meet_answer("set up a meet", None), "seek")

    def test_resolve_browse(self) -> None:
        self.assertEqual(_resolve_browse_or_meet_answer("see what's happening", None), "browse")


class TestRunBrowseTurn(unittest.TestCase):
    def test_asks_interest_first(self) -> None:
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="what's happening this weekend",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("what kind of thing", reply.lower())
        self.assertTrue((ctx.get("browse_draft") or {}).get("_asked"))

    @patch(
        "app.activity_browse._fetch_block_events",
        return_value=[
            {
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "The Pub",
                "cohort_tags": [],
            }
        ],
    )
    def test_shows_events_after_interest(self, _fetch) -> None:
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True},
            "phone_verified": True,
        }
        reply = run_activity_browse_turn(
            user_message="sports",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("coming up", reply.lower())
        self.assertIn("FIFA watch party", reply)
        self.assertTrue(ctx.get("activity_previews"))

    def test_reset_clears_state(self) -> None:
        ctx = {"activity_browse_active": True, "browse_draft": {"interest": "x"}, "browse_turns": 3}
        reset_activity_browse_state(ctx)
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertIsNone(ctx.get("browse_draft"))


# Fixture pool for the refinement tests. Dates are relative to a 2026-07 QA window:
# 2026-07-14 is a Tuesday (weekday), 2026-07-18 a Saturday (weekend).
_KID_TUE_MORNING = {
    "title": "Kids storytime",
    "starts_at": "2026-07-14T09:30:00",
    "venue_name": "The Library",
    "cohort_tags": ["kids_led_activity"],
}
_KID_TUE_EVENING = {
    "title": "Kids pizza night",
    "starts_at": "2026-07-14T18:00:00",
    "venue_name": "The Hall",
    "cohort_tags": ["kids_led_activity"],
}
_ADULT_SAT_MORNING = {
    "title": "FIFA watch party",
    "starts_at": "2026-07-18T09:00:00",
    "venue_name": "The Pub",
    "cohort_tags": [],
}
_KID_SAT_MORNING = {
    "title": "Family park playdate",
    "starts_at": "2026-07-18T10:00:00",
    "venue_name": "Laureate Park",
    "cohort_tags": [],
}
_REFINE_POOL = [_KID_TUE_MORNING, _KID_TUE_EVENING, _ADULT_SAT_MORNING, _KID_SAT_MORNING]


def _shown_ctx() -> dict:
    """Session mid-browse: results already shown for an open ('anything') interest."""
    return {
        "activity_browse_active": True,
        "browse_draft": {"interest": "anything", "_asked": True, "_results_shown": True},
        "phone_verified": False,
    }


class TestRefinementRelease(unittest.TestCase):
    """QA 2026-07-08: refinements after results must never leave the browse lane."""

    def test_refinement_after_results_stays_despite_meet_seek_read(self) -> None:
        # In production this exact shape was routed to save_signal_need_verify (6/20 sims).
        self.assertFalse(
            activity_browse_should_release(
                "Something for my 4 year old, we're free weekday mornings",
                _shown_ctx(),
                {"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.9},
            )
        )

    def test_zip_correction_stays_despite_off_lane_read(self) -> None:
        # "oops I mean 32827" was parsed as an activity name; a 5-digit token in browse
        # is always a ZIP, whatever the classifier says.
        self.assertFalse(
            activity_browse_should_release(
                "oops I mean 32827",
                _shown_ctx(),
                {"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.9},
            )
        )


class TestRefinementFilters(unittest.TestCase):
    @patch("app.activity_browse._fetch_block_events", return_value=list(_REFINE_POOL))
    def test_refinement_filters_by_time_and_age(self, _fetch) -> None:
        ctx = _shown_ctx()
        reply = run_activity_browse_turn(
            user_message="Something for my 4 year old, we're free weekday mornings",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        # Acknowledges the applied filters and shows only what fits.
        self.assertIn("Weekday mornings for a 4-year-old", reply)
        self.assertIn("1 fit", reply)
        self.assertIn("Kids storytime", reply)
        self.assertNotIn("FIFA", reply)
        # The raw user sentence is never echoed back as a category.
        self.assertNotIn("Something for my 4 year old", reply)
        self.assertEqual(len(ctx.get("activity_previews") or []), 1)

    @patch("app.activity_browse._fetch_block_events", return_value=list(_REFINE_POOL))
    def test_refinements_compose_across_turns(self, _fetch) -> None:
        ctx = _shown_ctx()
        first = run_activity_browse_turn(
            user_message="mornings please",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("3 fit", first)
        second = run_activity_browse_turn(
            user_message="just the weekend ones",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        # morning (turn 1) composes with weekend (turn 2).
        self.assertIn("Weekend mornings", second)
        self.assertIn("2 fit", second)
        self.assertIn("FIFA watch party", second)
        self.assertIn("Family park playdate", second)
        self.assertNotIn("storytime", second)

    @patch("app.activity_browse._fetch_block_events", return_value=[_KID_TUE_EVENING])
    def test_empty_refined_result_offers_listen_never_demands_verify(self, _fetch) -> None:
        ctx = _shown_ctx()
        reply = run_activity_browse_turn(
            user_message="weekday mornings for my 4 year old",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        # Honest empty state that OFFERS the seek as a question…
        self.assertIn("Want me to listen", reply)
        self.assertIn("?", reply)
        # …and never auto-routes to the verify gate or demands email.
        self.assertNotIn("verify", reply.lower())
        self.assertNotIn("email", reply.lower())
        self.assertEqual(ctx.get("routing_phase"), "listening")
        self.assertFalse(ctx.get("requires_phone_verification"))
        self.assertTrue((ctx.get("browse_draft") or {}).get("_seek_offer"))
        self.assertEqual(ctx.get("activity_previews"), [])
        # No verbatim echo of the refinement text either.
        self.assertNotIn("weekday mornings for my 4 year old", reply)

    @patch("app.activity_browse._fetch_block_events", return_value=[_ADULT_SAT_MORNING])
    @patch(
        "app.discovery_route.fetch_blocks_for_zip",
        return_value=[{"block_id": "b-new", "label": "Lake Nona"}],
    )
    def test_zip_correction_reroutes_to_new_block(self, _blocks, mock_fetch) -> None:
        ctx = _shown_ctx()
        reply = run_activity_browse_turn(
            user_message="oops I mean 32827",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        # The 5-digit token is a new ZIP, not an activity name.
        self.assertEqual(ctx.get("preview_zip"), "32827")
        self.assertEqual(ctx.get("preview_block_id"), "b-new")
        self.assertEqual(mock_fetch.call_args.args[1], "b-new")
        self.assertNotIn("oops", reply.lower())
        self.assertNotIn("Nothing like", reply)
        self.assertIn("FIFA watch party", reply)


class TestClarifierRouting(unittest.TestCase):
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_ambiguous_asks_clarifier(self, mock_slots, _ai) -> None:
        mock_slots.return_value = {
            "goal": "activities",
            "confidence": 0.9,
            "clarify": "browse_or_meet",
            "in_discovery": True,
        }
        reply, ctx, _, _ = handle_discovery_turn(
            "i want to do something fun this weekend",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertTrue(ctx.get("browse_or_meet_pending"))
        self.assertIn("happening", reply.lower())


if __name__ == "__main__":
    unittest.main()
