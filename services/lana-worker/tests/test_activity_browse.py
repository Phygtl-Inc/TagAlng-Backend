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

    def test_clear_meet_seek_searches_first(self) -> None:
        # Meet ≡ activity: a confident meet_seek enters the events browse (search-first);
        # the seek is offered only when the search comes up empty.
        self.assertEqual(
            _browse_or_seek_decision(
                {"goal": "save_signal", "signal_intent": "meet_seek",
                 "linear_intent": "looking.meet", "confidence": 0.9},
                "find me a tennis partner",
            ),
            "browse",
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

    def test_service_recommendation_never_enters_browse(self) -> None:
        # A place/service recommendation is a tip_seek — even when the classifier misreads
        # it as the activities space, the browse must decline so routing falls through to
        # the tip path (Google), not an events search answered with unrelated activities.
        self.assertIsNone(
            _browse_or_seek_decision(
                {"goal": "activities", "confidence": 0.9},
                "Recommend babysitting service",
            )
        )


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
    def test_button_entry_asks_interest(self) -> None:
        # The CTA's generic seed is dropped (browse_skip_seed), leaving nothing to mine —
        # only then does P1 ask the interest with chips.
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": None,
            "browse_skip_seed": True,
        }
        reply = run_activity_browse_turn(
            user_message="I'm looking for a meet or playgroup",
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
    def test_nl_entry_searches_immediately(self, _fetch) -> None:
        # A natural-language entry carries the interest — no generic "what kind of thing"
        # re-ask; the message is searched on the entry turn.
        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": None,
            "phone_verified": True,
        }
        reply = run_activity_browse_turn(
            user_message="fifa",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertNotIn("what kind of thing", reply.lower())
        self.assertIn("coming up", reply.lower())
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["FIFA watch party"]
        )
        self.assertEqual((ctx.get("browse_draft") or {}).get("interest"), "fifa")

    @patch("app.activity_browse._fetch_block_events", return_value=[])
    def test_nl_entry_empty_block_offers_seek(self, _fetch) -> None:
        # Nothing on the block → clearly say so and offer to listen (the seek fallback),
        # instead of a generic re-ask or a verify wall.
        ctx: dict = {"activity_browse_active": True, "browse_draft": None}
        reply = run_activity_browse_turn(
            user_message="any fifa activities for my 6 year old?",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("keep an ear out", reply.lower())
        self.assertTrue((ctx.get("browse_draft") or {}).get("_seek_offer"))
        # The raw sentence is not parroted back as the "kind".
        self.assertNotIn("any fifa activities for my 6 year old", reply.lower())

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
        # The events live in the card list, not duplicated in the chat text.
        self.assertNotIn("FIFA watch party", reply)
        self.assertEqual(
            [p["title"] for p in ctx.get("activity_previews") or []], ["FIFA watch party"]
        )

    def test_reset_clears_state(self) -> None:
        ctx = {"activity_browse_active": True, "browse_draft": {"interest": "x"}, "browse_turns": 3}
        reset_activity_browse_state(ctx)
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertIsNone(ctx.get("browse_draft"))


class TestBrowseChips(unittest.TestCase):
    def test_no_chips_on_zip_ask(self) -> None:
        # Asking for a ZIP → no interest-category pills (the answer is a ZIP).
        from app.ui_actions import activity_browse_actions

        ctx = {
            "activity_browse_active": True,
            "browse_draft": {"interest": "fifa", "_need_zip": True, "suggestions": []},
        }
        self.assertEqual(activity_browse_actions(ctx), [])

    def test_p1_ask_sends_interest_chips(self) -> None:
        from app.ui_actions import activity_browse_actions

        ctx = {
            "activity_browse_active": True,
            "browse_draft": {"_asked": True, "suggestions": ["Sports", "Family & kids"]},
        }
        self.assertEqual(
            [a["label"] for a in activity_browse_actions(ctx)],
            ["Sports", "Family & kids"],
        )

    @patch(
        "app.activity_browse._fetch_block_events",
        return_value=[
            {
                "title": "FIFA watch party",
                "starts_at": "2026-06-27T18:00:00",
                "venue_name": "The Pub",
                "cohort_tags": ["sports", "kids_led_activity"],
            }
        ],
    )
    def test_results_chips_come_from_event_tags(self, _fetch) -> None:
        from app.ui_actions import activity_browse_actions

        ctx: dict = {
            "activity_browse_active": True,
            "browse_draft": None,
            "phone_verified": True,
        }
        run_activity_browse_turn(
            user_message="fifa",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        # Machine-style tags are humanized for the pills.
        self.assertEqual(
            [a["label"] for a in activity_browse_actions(ctx)],
            ["Sports", "Kids led activity"],
        )


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


class TestClarifierAnswerCarriesContext(unittest.TestCase):
    """A chip tap answers browse-vs-seek but carries none of the original constraints —
    the chosen lane must be seeded with the utterance that triggered the clarifier
    ('fun with my 4 year old this week'), not the chip label."""

    _ORIGINAL = "looking for something fun to do with my 4 year old this week"
    _CHIPS = ["Show activities nearby", "Find neighbors to do something with"]

    def _ask_clarifier(self, mock_slots) -> dict:
        mock_slots.return_value = {
            "goal": "activities",
            "confidence": 0.9,
            "clarify": "browse_or_meet",
            "clarify_options": list(self._CHIPS),
            "in_discovery": True,
        }
        session_ctx = {"routing_phase": "listening"}
        handle_discovery_turn(
            self._ORIGINAL,
            session_ctx=session_ctx,
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        return session_ctx

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_chip_tap_seeds_browse_with_original_ask(self, mock_slots, _ai) -> None:
        session_ctx = self._ask_clarifier(mock_slots)
        self.assertEqual(session_ctx.get("browse_or_meet_origin"), self._ORIGINAL)
        mock_slots.return_value = {"goal": "activities", "confidence": 0.9, "in_discovery": True}
        with patch(
            "app.discovery_route._start_activity_browse_from_discovery",
            return_value=("ok", {}, {}, []),
        ) as mock_start:
            handle_discovery_turn(
                "Show activities nearby",
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
        self.assertEqual(mock_start.call_args.kwargs.get("msg"), self._ORIGINAL)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_neighbors_chip_seeds_seek_with_original_ask(self, mock_slots, _ai) -> None:
        session_ctx = self._ask_clarifier(mock_slots)
        mock_slots.return_value = {
            "goal": "peers", "signal_intent": "meet_seek", "confidence": 0.9,
            "in_discovery": True,
        }
        with patch(
            "app.discovery_route._start_look_meet_from_discovery",
            return_value=("ok", {}, {}, []),
        ) as mock_start:
            handle_discovery_turn(
                "Find neighbors to do something with",
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
        self.assertEqual(mock_start.call_args.kwargs.get("msg"), self._ORIGINAL)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_free_text_answer_wins_over_stash(self, mock_slots, _ai) -> None:
        session_ctx = self._ask_clarifier(mock_slots)
        mock_slots.return_value = {"goal": "activities", "confidence": 0.9, "in_discovery": True}
        with patch(
            "app.discovery_route._start_activity_browse_from_discovery",
            return_value=("ok", {}, {}, []),
        ) as mock_start:
            handle_discovery_turn(
                "actually just show me sports stuff",
                session_ctx=session_ctx,
                user_jwt="jwt",
                phone_verified=False,
                home_block_id=None,
                is_anonymous=True,
            )
        self.assertEqual(
            mock_start.call_args.kwargs.get("msg"), "actually just show me sports stuff"
        )

    def test_neighbors_chip_resolves_to_seek_without_slots(self) -> None:
        self.assertEqual(
            _resolve_browse_or_meet_answer("Find neighbors to do something with", None),
            "seek",
        )


if __name__ == "__main__":
    unittest.main()
