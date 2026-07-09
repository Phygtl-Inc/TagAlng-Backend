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


class TestBrowseWhenText(unittest.TestCase):
    """Chat text, preview-card label, and the LLM filter's date lines must all say the
    same LOCAL date for the same event (QA 2026-07-08: one event, three datetimes)."""

    # Stored UTC instant = Mon Jul 13, 8:30 PM America/New_York.
    QA_EVENT = {
        "id": "ev-1",
        "title": "Playdate at the park",
        "venue_name": "New York",
        "starts_at": "2026-07-14T00:30:00+00:00",
    }

    def setUp(self) -> None:
        patcher = patch.dict("os.environ", {"EVENT_DEFAULT_TZ": "America/New_York"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chat_text_uses_local_date_not_utc(self) -> None:
        from app.activity_browse import _format_browse_message

        msg = _format_browse_message([self.QA_EVENT], None, phone_verified=True)
        self.assertIn("(Mon Jul 13, 8:30 PM)", msg)
        self.assertNotIn("Jul 14", msg)  # the leaked UTC date QA saw

    def test_chat_text_date_equals_card_label_date(self) -> None:
        from app.activity_browse import _format_browse_message
        from app.discovery_route import activity_previews_from_events

        preview = activity_previews_from_events([self.QA_EVENT])[0]
        self.assertEqual(preview["starts_label"], "Mon, Jul 13 · 8:30 PM")
        # Raw ISO stays for clients — payload contract unchanged.
        self.assertEqual(preview["starts_at"], "2026-07-14T00:30:00+00:00")
        msg = _format_browse_message([self.QA_EVENT], None, phone_verified=True)
        self.assertIn("Mon Jul 13", msg)  # same local date as the card

    def test_llm_filter_sees_local_date(self) -> None:
        from app.activity_browse import _event_when_parts

        # The date line the filter LLM matches "on July 13" against must be local too.
        self.assertEqual(_event_when_parts(self.QA_EVENT["starts_at"]), "2026-07-13 Mon")

    def test_core_block_events_carry_when_text_not_raw_utc(self) -> None:
        from app.orchestrator.memory import _event_for_llm

        out = _event_for_llm(dict(self.QA_EVENT))
        self.assertNotIn("starts_at", out)
        self.assertEqual(out["when_text"], "Mon Jul 13, 8:30 PM")


if __name__ == "__main__":
    unittest.main()
