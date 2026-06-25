import unittest
from unittest.mock import patch

from app.look_meet import (
    look_meet_should_release,
    look_meet_user_moved_on,
    reset_look_meet_state,
    run_look_meet_turn,
    save_pending_meet_seek,
)


class TestLookMeetPivot(unittest.TestCase):
    def test_ready_new_request_releases(self) -> None:
        """On the ready card, a substantive new request must not replay the same card."""
        ctx = {"look_meet_active": True, "look_ready": True}
        self.assertTrue(
            look_meet_user_moved_on("what activities do we have this weekend?", ctx)
        )
        self.assertTrue(look_meet_user_moved_on("ok but any brazilian event?", ctx))

    def test_ready_confirm_and_edit_stay_in_flow(self) -> None:
        ctx = {"look_meet_active": True, "look_ready": True}
        self.assertFalse(look_meet_user_moved_on("Start listening for me", ctx))
        self.assertFalse(look_meet_user_moved_on("fix:kind", ctx))

    def test_cancel_handled_in_flow_not_a_pivot(self) -> None:
        ctx = {"look_meet_active": True, "look_ready": True}
        # Cancel is a graceful in-flow exit, not a reroute.
        self.assertFalse(look_meet_user_moved_on("never mind", ctx))

    def test_mid_capture_answers_stay_in_flow(self) -> None:
        ctx = {"look_meet_active": True}  # not ready yet — collecting fields
        for answer in ("anywhere", "Lively", "Open · all moms", "anything like fifa screening?"):
            self.assertFalse(look_meet_user_moved_on(answer, ctx), answer)

    def test_explicit_pivot_releases_any_time(self) -> None:
        ctx = {"look_meet_active": True}  # mid-capture
        for pivot in (
            "find me italian moms",
            "show me people on my block",
            "log out",
            "host an event",
        ):
            self.assertTrue(look_meet_user_moved_on(pivot, ctx), pivot)

    def test_reset_clears_state(self) -> None:
        ctx = {
            "look_meet_active": True,
            "look_ready": True,
            "look_draft": {"kind": "fifa screening"},
            "look_turns": 5,
            "look_pending_ask": "affinity",
        }
        reset_look_meet_state(ctx)
        self.assertFalse(ctx.get("look_meet_active"))
        self.assertIsNone(ctx.get("look_draft"))
        self.assertIsNone(ctx.get("look_ready"))
        self.assertEqual(ctx.get("look_turns"), 0)


class TestLookMeetAiRelease(unittest.TestCase):
    """The mid-capture release is AI-driven (abandon + intent lane), not just regex."""

    def test_semantic_abandon_releases(self) -> None:
        ctx = {"look_meet_active": True, "look_draft": {"kind": "fifa meet"}}
        # No cancel keyword — only the classifier's abandon flag catches this.
        self.assertTrue(
            look_meet_should_release(
                "eh, maybe some other time", ctx, {"abandon": True, "confidence": 0.7}
            )
        )

    def test_cross_lane_pivot_releases_via_ai(self) -> None:
        ctx = {"look_meet_active": True, "look_draft": {"kind": "fifa meet"}}
        # No pivot keyword in the text — the AI lane read is what releases it.
        self.assertTrue(
            look_meet_should_release(
                "actually connect me with locals like me",
                ctx,
                {"goal": "peers", "confidence": 0.85},
            )
        )

    def test_changed_meet_after_kind_releases(self) -> None:
        ctx = {"look_meet_active": True, "look_draft": {"kind": "fifa meet"}}
        self.assertTrue(
            look_meet_should_release(
                "i'd rather meet some dads for poker",
                ctx,
                {"goal": "activities", "confidence": 0.8},
            )
        )

    def test_kind_answer_at_p1_does_not_release(self) -> None:
        # No kind captured yet → an activity-shaped reply IS the answer to "what kind?".
        ctx = {"look_meet_active": True, "look_draft": {}}
        self.assertFalse(
            look_meet_should_release(
                "something like a fifa screening",
                ctx,
                {"goal": "activities", "confidence": 0.85},
            )
        )

    def test_plain_answer_stays_in_flow(self) -> None:
        ctx = {"look_meet_active": True, "look_draft": {"kind": "fifa meet"}}
        # A benign day answer classifies as continue — must not be read as a pivot.
        self.assertFalse(
            look_meet_should_release("saturday", ctx, {"goal": "continue", "confidence": 0.9})
        )

    def test_no_slots_falls_back_to_regex(self) -> None:
        ctx = {"look_meet_active": True, "look_ready": True}
        self.assertTrue(look_meet_should_release("any brazilian event?", ctx, None))
        self.assertFalse(look_meet_should_release("Start listening for me", ctx, None))


class TestLookMeetGuestGate(unittest.TestCase):
    def test_guest_gated_to_verify_on_start_listening(self) -> None:
        """A guest tapping Start listening is asked for email and the seek is stashed."""
        ctx = {
            "look_meet_active": True,
            "look_ready": True,
            "look_draft": {"kind": "fifa meet"},
            "phone_verified": False,
        }
        reply = run_look_meet_turn(
            user_message="Start listening for me",
            session_ctx=ctx,
            history=[],
            user_jwt="anon-jwt",
            home_block_id=None,
        )
        self.assertIn("email", reply.lower())
        self.assertTrue(ctx.get("requires_phone_verification"))
        self.assertEqual(ctx.get("routing_phase"), "await_signup_phone")
        self.assertIsInstance(ctx.get("look_seek_pending"), dict)
        self.assertFalse(ctx.get("look_meet_active"))

    @patch("app.look_meet._find_block_events", return_value=[])
    @patch(
        "app.look_meet._save_meet_seek",
        return_value={"signal_id": "s1", "matches_created": 2},
    )
    def test_verified_user_saves_directly(self, mock_save, _ev) -> None:
        ctx = {
            "look_meet_active": True,
            "look_ready": True,
            "look_draft": {"kind": "fifa meet"},
            "phone_verified": True,
        }
        reply = run_look_meet_turn(
            user_message="Start listening for me",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="block-1",
        )
        mock_save.assert_called_once()
        self.assertIn("listening", reply.lower())
        self.assertTrue(ctx.get("look_meet_saved_now"))
        self.assertIsNone(ctx.get("look_seek_pending"))

    @patch(
        "app.look_meet._save_meet_seek",
        return_value={"signal_id": "s2", "matches_created": 0},
    )
    def test_save_pending_seek_after_verify(self, mock_save) -> None:
        ctx = {"look_seek_pending": {"kind": "fifa meet"}}
        reply = save_pending_meet_seek(
            session_ctx=ctx, user_jwt="jwt", block_id="b1", zip_code=None
        )
        self.assertIsNotNone(reply)
        self.assertIn("listening", reply.lower())
        self.assertTrue(ctx.get("look_meet_saved_now"))
        self.assertIsNone(ctx.get("look_seek_pending"))
        mock_save.assert_called_once()

    def test_save_pending_seek_noop_when_nothing_pending(self) -> None:
        self.assertIsNone(
            save_pending_meet_seek(
                session_ctx={}, user_jwt="jwt", block_id=None, zip_code=None
            )
        )

    @patch("app.look_meet._find_block_events", return_value=[])
    def test_p1_does_not_loop_on_vague_input(self, _ev) -> None:
        """Vague input asks 'what kind?' once, then progresses instead of re-asking."""
        ctx: dict = {"look_meet_active": True}
        first = run_look_meet_turn(
            user_message="any fun activity",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        self.assertIn("what kind of meet", first.lower())
        self.assertTrue((ctx.get("look_draft") or {}).get("_p1_asked"))

        second = run_look_meet_turn(
            user_message="something fun honestly",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
        )
        # No identical re-ask — the words are taken as the kind and the flow advances.
        self.assertNotIn("what kind of meet", second.lower())
        self.assertEqual((ctx.get("look_draft") or {}).get("kind"), "something fun honestly")


if __name__ == "__main__":
    unittest.main()
