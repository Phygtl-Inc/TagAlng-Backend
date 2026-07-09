"""Swap/tip captures queue with an honest promise instead of dead-ending.

QA (2026-07-08): the "3T rain boots" pass-along capture completed beautifully and then
went nowhere (swaps are "Coming soon" in the UI); a dentist tip ask from an unverified
guest got a bare "Verify your email first" wall; the capability copy claimed swaps/tips
were live. These tests pin the honest behavior: captures close with the hold-it promise
and land in queued_contributions, the tip ask acknowledges + queues, and the capability
copy says "opening soon".
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.discovery_route import _try_save_signal_turn, _try_signal_lane_turn
from app.layer1_handlers import HELP_WHAT_CAN_YOU_DO
from app.pass_along import run_pass_along_turn
from app.queued_contributions import (
    QUEUED_KIND_SWAP,
    QUEUED_KIND_TIP,
    kind_for_signal_intent,
    queued_close_line,
    unverified_queue_reply,
)
from app.tip_share import run_tip_share_turn

_BARE_WALL = "Verify your email first — then I can post that to your block."


def _boots_ctx() -> dict:
    """A pass-along session at the save step: capture complete, photo already offered."""
    return {
        "pass_along_active": True,
        "pass_along_turns": 3,
        "pass_along_photo_prompted": True,
        "item_draft": {
            "title": "3T rain boots",
            "intent_type": "free",
            "condition": "lightly used",
            "category": "kids clothing",
        },
        "zip_code": "32827",
    }


class TestBootsCaptureQueues(unittest.TestCase):
    @patch("app.queued_contributions.queue_contribution", return_value=True)
    def test_verified_boots_capture_ends_queued_with_honest_close(self, mock_queue) -> None:
        ctx = _boots_ctx()
        reply = run_pass_along_turn(
            user_message="list it",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="block-1",
            user_id="user-1",
            phone_verified=True,
        )
        # Honest close — the hold-it promise, not a dead-end "listed!" claim.
        self.assertIn("I'll hold your", reply)
        self.assertIn("3T rain boots", reply)
        self.assertIn("swaps open on your block soon", reply)
        self.assertIn("first up", reply)
        self.assertIn("I'll text you", reply)
        # No dead end, no bare verify wall, no false "listed on your block".
        self.assertNotIn("Verify your email first", reply)
        self.assertNotIn("listed on your block", reply)
        # The contribution actually landed in the queue, with a notify promise.
        mock_queue.assert_called_once()
        kwargs = mock_queue.call_args.kwargs
        self.assertEqual(kwargs["kind"], QUEUED_KIND_SWAP)
        self.assertEqual(kwargs["user_id"], "user-1")
        self.assertEqual(kwargs["block_id"], "block-1")
        self.assertTrue(kwargs["notify"])
        self.assertEqual(kwargs["payload"]["title"], "3T rain boots")
        self.assertEqual(kwargs["payload"]["intent"], "swap_offer")
        # Flow closed cleanly; the card shows the queued state.
        self.assertFalse(ctx["pass_along_active"])
        self.assertTrue(ctx["item_queued_now"])
        self.assertTrue(ctx["item_draft"]["queued"])

    @patch("app.queued_contributions.queue_contribution", return_value=True)
    def test_anonymous_boots_capture_queues_without_notify(self, mock_queue) -> None:
        ctx = _boots_ctx()
        reply = run_pass_along_turn(
            user_message="list it",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id=None,
            user_id="anon-1",
            phone_verified=False,
        )
        self.assertIn("I'll hold your", reply)
        self.assertIn("swaps open on your block soon", reply)
        # No text-you promise without a verified contact — verify is an invitation,
        # never a wall.
        self.assertNotIn("I'll text you.", reply)
        self.assertIn("Verify your email and I'll text you", reply)
        self.assertFalse(mock_queue.call_args.kwargs["notify"])


class TestTipCaptureQueues(unittest.TestCase):
    @patch("app.queued_contributions.queue_contribution", return_value=True)
    def test_tip_pass_along_cta_queues_with_honest_close(self, mock_queue) -> None:
        ctx = {
            "tip_share_active": True,
            "tip_ready": True,
            "tip_turns": 3,
            "tip_draft": {
                "name": "Dr. Sarah",
                "category": "pediatric dentist",
                "trait": "gentle with toddlers",
            },
        }
        reply = run_tip_share_turn(
            user_message="pass the tip along",
            session_ctx=ctx,
            history=[],
            user_jwt="jwt",
            home_block_id="block-1",
            user_id="user-1",
            phone_verified=True,
        )
        self.assertIn("I'll hold your", reply)
        self.assertIn("Dr. Sarah", reply)
        self.assertIn("tips open on your block soon", reply)
        self.assertIn("I'll text you", reply)
        self.assertNotIn("Verify your email first", reply)
        mock_queue.assert_called_once()
        kwargs = mock_queue.call_args.kwargs
        self.assertEqual(kwargs["kind"], QUEUED_KIND_TIP)
        self.assertTrue(kwargs["notify"])
        self.assertEqual(kwargs["payload"]["intent"], "tip_share")
        self.assertTrue(ctx["tip_queued_now"])
        self.assertTrue(ctx["tip_draft"]["queued"])


class TestDentistTipNeverBareWall(unittest.TestCase):
    @patch("app.queued_contributions.queue_contribution", return_value=True)
    def test_unverified_dentist_tip_queues_instead_of_wall(self, mock_queue) -> None:
        result = _try_save_signal_turn(
            msg="anyone know a good pediatric dentist?",
            slots={
                "goal": "save_signal",
                "confidence": 0.9,
                "signal_intent": "tip_seek",
                "signal_detail": "pediatric dentist",
                "signal_category": "health",
                "linear_intent": "looking.tip",
            },
            session_ctx={"preview_block_id": "block-a", "zip": "32827"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            phase="preview",
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, _ctx, routing, _peers = result
        # Acknowledge + almost-here + queued — never the bare verify wall.
        self.assertNotEqual(reply, _BARE_WALL)
        self.assertIn("pediatric dentist", reply)
        self.assertIn("almost here on your block", reply)
        self.assertIn("first in line", reply)
        self.assertEqual(routing.get("tool_to_call"), "save_signal_queued_unverified")
        mock_queue.assert_called_once()
        kwargs = mock_queue.call_args.kwargs
        self.assertEqual(kwargs["kind"], QUEUED_KIND_TIP)
        self.assertEqual(kwargs["block_id"], "block-a")
        self.assertFalse(kwargs["notify"])

    @patch("app.queued_contributions.queue_contribution", return_value=True)
    def test_signal_lane_wall_also_queues_tip_family(self, mock_queue) -> None:
        result = _try_signal_lane_turn(
            msg="I know a great dentist for kids",
            slots={
                "goal": "save_signal",
                "confidence": 0.9,
                "signal_intent": "tip_share",
                "signal_detail": "great dentist for kids",
                "linear_intent": "sharing.tip",
            },
            session_ctx={"preview_block_id": "block-a"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            phase="preview",
            user_id="user-1",
        )
        self.assertIsNotNone(result)
        reply, _ctx, routing, _peers = result
        self.assertNotEqual(reply, _BARE_WALL)
        self.assertIn("almost here on your block", reply)
        self.assertEqual(routing.get("tool_to_call"), "save_signal_queued_unverified")
        mock_queue.assert_called_once()
        self.assertEqual(mock_queue.call_args.kwargs["kind"], QUEUED_KIND_TIP)

    def test_live_meet_family_keeps_existing_gate(self) -> None:
        # unverified_queue_reply only owns the not-yet-live swap/tip families —
        # meets are live, so their gate is untouched.
        self.assertIsNone(
            unverified_queue_reply(
                signal_intent="meet_seek",
                detail="moms walking group",
                user_id="user-1",
                block_id="block-a",
            )
        )
        self.assertIsNone(kind_for_signal_intent("host_meet"))
        self.assertEqual(kind_for_signal_intent("swap_seek"), QUEUED_KIND_SWAP)
        self.assertEqual(kind_for_signal_intent("tip_seek"), QUEUED_KIND_TIP)


class TestCapabilityCopyHonest(unittest.TestCase):
    def test_capability_copy_no_longer_claims_live_swaps(self) -> None:
        self.assertNotIn("swap or borrow items", HELP_WHAT_CAN_YOU_DO)
        self.assertNotIn("share local tips", HELP_WHAT_CAN_YOU_DO)
        self.assertIn("opening soon on your block", HELP_WHAT_CAN_YOU_DO)

    def test_queued_close_line_copy(self) -> None:
        line = queued_close_line(
            kind=QUEUED_KIND_SWAP, summary="3T rain boots · free", notify=True
        )
        self.assertEqual(
            line,
            "I'll hold your **3T rain boots · free** listing — swaps open on your "
            "block soon and yours will be first up. I'll text you.",
        )


if __name__ == "__main__":
    unittest.main()
