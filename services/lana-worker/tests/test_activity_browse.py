import unittest
from unittest.mock import patch

from app.activity_browse import (
    activity_browse_should_release,
    enter_activity_browse_from_cta,
    looks_like_bare_look_meet_entry,
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


class TestLookMeetCtaEntry(unittest.TestCase):
    """The "A meet or playgroup" CTA (intent_hint=look_meet) must only take the canned
    fast path on a BARE chip tap — free text with real content has to reach the AI
    classifier, never the canned "what kind of thing are you up for?" opener."""

    def test_free_text_with_hint_is_not_captured_by_canned_funnel(self) -> None:
        # QA 2026-07-08: this message got the literal canned opener in production.
        ctx: dict = {}
        entered = enter_activity_browse_from_cta(ctx, "I need a babysitter for tonight")
        self.assertFalse(entered)
        # Session untouched → the pipeline's browse gate never fires and the turn falls
        # through to handle_discovery_turn (layer-1 classification).
        self.assertFalse(ctx.get("activity_browse_active"))
        self.assertFalse(ctx.get("browse_skip_seed"))

    def test_semantic_edge_messages_are_not_bare(self) -> None:
        # The QA edge set: safety worry, emotional relocation, capability question —
        # all must classify, none may take the canned entry.
        for msg in (
            "how do I know the moms on here are real and not creeps?",
            "I'm a stay at home dad, is this app for me too?",
            "We just moved here after my husband's job fell through and honestly "
            "I don't know a single person on this street and it's been really hard",
            "can you help me find someone to watch my kids",
        ):
            self.assertFalse(looks_like_bare_look_meet_entry(msg), msg)

    def test_bare_chip_tap_keeps_canned_behavior(self) -> None:
        ctx: dict = {}
        self.assertTrue(enter_activity_browse_from_cta(ctx, "Family & kids"))
        self.assertTrue(ctx.get("activity_browse_active"))
        self.assertTrue(ctx.get("browse_skip_seed"))
        # The seed turn still asks P1 canned (fast, no classification dependency).
        reply = run_activity_browse_turn(
            user_message="Family & kids",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="b1",
        )
        self.assertIn("what kind of thing are you up for", reply.lower())
        self.assertFalse(ctx.get("browse_skip_seed"))  # consumed — later turns re-decide

    def test_cta_seed_payload_zip_and_labels_are_bare(self) -> None:
        for msg in (
            "",
            "I'm looking for a meet or playgroup",  # the button's generic payload
            "A meet or playgroup",
            "Sports",
            "Outdoors",
            "Stroller walk",
            "32827",
        ):
            self.assertTrue(looks_like_bare_look_meet_entry(msg), repr(msg))


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
