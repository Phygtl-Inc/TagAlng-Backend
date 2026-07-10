"""Goal stack: a side-quest (upfront name capture) must never eat the active goal.

QA (2026-07-08): tapping Lana's own "Meet playground-loving neighbors" chip started the
name side-quest; after "Jess" the goal was gone, restating it hit the stale name-ask
("No rush — a first name is all I need") one turn after Lana greeted her by name, and a
third restatement got "I'll skip that for now" — three drops of the same ask.
"""

import unittest
from unittest.mock import patch

from app.db import merge_session_context
from app.discovery_route import (
    PHASE_NEED_DISPLAY_NAME,
    _resume_interrupted_goal,
    _try_upfront_display_name_turn,
)
from app.goal_stack import (
    GOAL_STACK_KEY,
    goal_kind_from_slots,
    pending_goal_from_turn,
    pop_pending_goal,
    push_pending_goal,
    resume_ack,
)

_PEER_SLOTS = {
    "goal": "peers",
    "linear_intent": "discovery.find_by_attrs",
    "confidence": 0.9,
    "signal_detail": "playground-loving neighbors",
}


class TestGoalStackHelpers(unittest.TestCase):
    def test_push_pop_lifo_and_none_stamp_on_empty(self) -> None:
        ctx: dict = {}
        push_pending_goal(ctx, {"kind": "find_neighbors", "message": "first"})
        push_pending_goal(ctx, {"kind": "seek_tip", "message": "second"})
        self.assertEqual(len(ctx[GOAL_STACK_KEY]), 2)
        top = pop_pending_goal(ctx)
        self.assertEqual(top["message"], "second")
        self.assertEqual(len(ctx[GOAL_STACK_KEY]), 1)
        pop_pending_goal(ctx)
        # Empty stack is None-stamped so the session merge deletes it (never popped).
        self.assertIsNone(ctx[GOAL_STACK_KEY])
        merged = merge_session_context({GOAL_STACK_KEY: [{"kind": "x", "message": "m"}]}, ctx)
        self.assertNotIn(GOAL_STACK_KEY, merged)

    def test_push_dedupes_same_message(self) -> None:
        ctx: dict = {}
        push_pending_goal(ctx, {"kind": "find_neighbors", "message": "meet moms"})
        push_pending_goal(ctx, {"kind": "find_neighbors", "message": "meet moms"})
        self.assertEqual(len(ctx[GOAL_STACK_KEY]), 1)

    def test_goal_kind_from_slots(self) -> None:
        self.assertEqual(goal_kind_from_slots(_PEER_SLOTS), "find_neighbors")
        self.assertEqual(
            goal_kind_from_slots({"goal": "activities", "confidence": 0.8}),
            "find_activities",
        )
        self.assertEqual(
            goal_kind_from_slots(
                {"goal": "save_signal", "signal_intent": "meet_seek", "confidence": 0.8}
            ),
            "find_neighbors",
        )
        # Low confidence / chat never becomes a resumable goal.
        self.assertIsNone(goal_kind_from_slots({"goal": "peers", "confidence": 0.2}))
        self.assertIsNone(goal_kind_from_slots({"goal": "chat", "confidence": 0.9}))
        self.assertIsNone(goal_kind_from_slots(None))

    def test_pending_goal_from_slots_carries_topic_and_message(self) -> None:
        goal = pending_goal_from_turn("meet playground-loving neighbors", {}, _PEER_SLOTS)
        self.assertIsNotNone(goal)
        self.assertEqual(goal["kind"], "find_neighbors")
        self.assertEqual(goal["topic"], "playground-loving neighbors")
        self.assertEqual(goal["message"], "meet playground-loving neighbors")
        self.assertEqual(goal["slots"], _PEER_SLOTS)

    def test_tapped_goal_payload_is_authoritative(self) -> None:
        # Structured chip payload wins over (even absent) classifier slots.
        session_ctx = {"tapped_goal": {"kind": "find_neighbors", "topic": "playground time"}}
        goal = pending_goal_from_turn("connect me with moms", session_ctx, None)
        self.assertIsNotNone(goal)
        self.assertEqual(goal["kind"], "find_neighbors")
        self.assertEqual(goal["topic"], "playground time")
        self.assertEqual(goal["source"], "tap")

    def test_plain_chat_is_not_stashed(self) -> None:
        self.assertIsNone(pending_goal_from_turn("hey there", {}, {}))
        self.assertIsNone(pending_goal_from_turn("", {}, _PEER_SLOTS))

    def test_resume_ack_references_topic(self) -> None:
        self.assertIn("playground-loving neighbors", resume_ack("playground-loving neighbors"))


class TestNameSideQuestKeepsGoal(unittest.TestCase):
    """The upfront-name side-quest stashes the interrupted goal and resumes it."""

    @patch("app.discovery_route.user_needs_display_name", return_value=True)
    def test_interrupt_stashes_goal_and_survives_merge(self, _needs) -> None:
        result = _try_upfront_display_name_turn(
            msg="meet playground-loving neighbors",
            session_ctx={"routing_phase": "listening"},
            user_id="user-1",
            phase="listening",
            is_anonymous=False,
            slots=_PEER_SLOTS,
        )
        self.assertIsNotNone(result)
        reply, out_ctx, _routing, _peers = result
        self.assertIn("call you", reply.lower())
        stack = out_ctx[GOAL_STACK_KEY]
        self.assertEqual(len(stack), 1)
        self.assertEqual(stack[0]["topic"], "playground-loving neighbors")
        self.assertEqual(stack[0]["message"], "meet playground-loving neighbors")
        # Goal persists across the session round-trip (list value, plain merge).
        merged = merge_session_context({}, out_ctx)
        self.assertEqual(merged[GOAL_STACK_KEY], stack)

    @patch("app.discovery_route.persist_profile_patch")
    @patch("app.discovery_route.user_needs_display_name", return_value=True)
    def test_name_capture_pops_goal_for_resume(self, _needs, _persist) -> None:
        goal = {
            "kind": "find_neighbors",
            "topic": "playground-loving neighbors",
            "message": "meet playground-loving neighbors",
            "slots": _PEER_SLOTS,
        }
        result = _try_upfront_display_name_turn(
            msg="Jess",
            session_ctx={
                "routing_phase": PHASE_NEED_DISPLAY_NAME,
                "awaiting_upfront_name": True,
                GOAL_STACK_KEY: [goal],
            },
            user_id="user-1",
            phase=PHASE_NEED_DISPLAY_NAME,
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        reply, out_ctx, _routing, _peers = result
        self.assertIn("Jess", reply)
        # No dead-end "how can I help you today?" — the goal is queued for resume.
        self.assertNotIn("how can I help", reply.lower())
        self.assertEqual(out_ctx["_resume_goal"], goal)
        self.assertIsNone(out_ctx[GOAL_STACK_KEY])
        self.assertTrue(out_ctx["display_name_saved"])

    @patch("app.discovery_route.user_needs_display_name", return_value=True)
    def test_name_give_up_still_resumes_goal(self, _needs) -> None:
        goal = {"kind": "find_neighbors", "topic": "running moms", "message": "find running moms"}
        result = _try_upfront_display_name_turn(
            msg="just show me the neighbors already",
            session_ctx={
                "routing_phase": PHASE_NEED_DISPLAY_NAME,
                "awaiting_upfront_name": True,
                "upfront_name_attempts": 1,
                GOAL_STACK_KEY: [goal],
            },
            user_id="user-1",
            phase=PHASE_NEED_DISPLAY_NAME,
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        reply, out_ctx, _routing, _peers = result
        self.assertIn("skip that", reply.lower())
        self.assertEqual(out_ctx["_resume_goal"], goal)
        self.assertTrue(out_ctx["display_name_saved"])


class TestNameNeverReAsked(unittest.TestCase):
    """Once the name is captured, the capture-eligibility check reads the SAME persisted
    state the save wrote (display_name_saved / users.nickname) — never a second store."""

    def test_stale_awaiting_flag_does_not_reask_when_name_saved(self) -> None:
        # The QA bug: the save turn wrote display_name_saved=True, but the merge kept the
        # old awaiting_upfront_name=True alive — the next turn re-asked the name. Now the
        # awaiting branch defers to user_needs_display_name (which reads display_name_saved
        # without any DB hit) and releases.
        session_ctx = {
            "routing_phase": PHASE_NEED_DISPLAY_NAME,
            "awaiting_upfront_name": True,
            "display_name_saved": True,
        }
        result = _try_upfront_display_name_turn(
            msg="meet playground-loving neighbors",
            session_ctx=session_ctx,
            user_id="user-1",
            phase=PHASE_NEED_DISPLAY_NAME,
            is_anonymous=False,
        )
        # Falls through to normal routing — the restated goal is handled, not re-asked.
        self.assertIsNone(result)
        # The stale flags are None-stamped so the session merge deletes them for good.
        self.assertIsNone(session_ctx["awaiting_upfront_name"])
        merged = merge_session_context(
            {"awaiting_upfront_name": True, "upfront_name_attempts": 1}, session_ctx
        )
        self.assertNotIn("awaiting_upfront_name", merged)
        self.assertNotIn("upfront_name_attempts", merged)

    @patch("app.discovery_route.user_needs_display_name", return_value=False)
    def test_fresh_quest_never_starts_once_name_known(self, _needs) -> None:
        self.assertIsNone(
            _try_upfront_display_name_turn(
                msg="hey",
                session_ctx={"routing_phase": "listening"},
                user_id="user-1",
                phase="listening",
                is_anonymous=False,
            )
        )


class TestResumeInterruptedGoal(unittest.TestCase):
    def _resume(self, mock_handle):
        goal = {
            "kind": "find_neighbors",
            "topic": "playground-loving neighbors",
            "message": "meet playground-loving neighbors",
            "slots": _PEER_SLOTS,
        }
        ctx = {"display_name_saved": True, "nickname": "Jess", "last_routing": {"outcome": "R"}}
        return _resume_interrupted_goal(
            goal,
            lead_reply="Love it — great to meet you, Jess!",
            ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
            history=[],
            user_id="user-1",
            timer=None,
        )

    @patch("app.discovery_route.handle_discovery_turn")
    def test_resume_routes_goal_and_ack_references_topic(self, mock_handle) -> None:
        mock_handle.return_value = (
            "Here are 5 neighbors who love playground time.",
            {"routing_phase": "preview"},
            {"outcome": "R"},
            [{"nickname": "Ada"}],
        )
        reply, out_ctx, _routing, peers = self._resume(mock_handle)
        # Ack keeps the greeting AND references the stored topic, then the goal's real reply.
        self.assertIn("Jess", reply)
        self.assertIn("playground-loving neighbors", reply)
        self.assertIn("Here are 5 neighbors", reply)
        self.assertEqual(peers[0]["nickname"], "Ada")
        # The goal's handler got the STORED message, with its classified slots re-primed
        # into the cache so the intent can't be lost to re-parsing.
        args, kwargs = mock_handle.call_args
        self.assertEqual(args[0], "meet playground-loving neighbors")
        resumed_ctx = kwargs["session_ctx"]
        self.assertEqual(resumed_ctx["_discovery_slots"], _PEER_SLOTS)
        self.assertEqual(resumed_ctx["_discovery_slots_for"], "meet playground-loving neighbors")
        self.assertTrue(resumed_ctx["_resuming_goal"])
        self.assertNotIn("_resuming_goal", out_ctx)

    @patch("app.discovery_route.handle_discovery_turn", return_value=None)
    def test_resume_fallback_still_names_topic(self, mock_handle) -> None:
        reply, _ctx, _routing, peers = self._resume(mock_handle)
        self.assertIn("playground-loving neighbors", reply)
        self.assertEqual(peers, [])


class TestChipGoalPayload(unittest.TestCase):
    """Suggestion buttons carry a structured goal (kind + topic) next to the display text."""

    def test_rapport_chip_carries_goal(self) -> None:
        from app.ui_actions import _rapport_action_chip

        chip = _rapport_action_chip(
            {
                "kind": "find_neighbors",
                "label": "Meet playground-loving neighbors",
                "topic": "playground time",
                "send": "connect me with moms into playground time",
            }
        )
        self.assertIsNotNone(chip)
        self.assertEqual(chip["message"], "connect me with moms into playground time")
        self.assertEqual(chip["goal"], {"kind": "find_neighbors", "topic": "playground time"})

    def test_rapport_chip_without_kind_stays_plain(self) -> None:
        from app.ui_actions import _rapport_action_chip

        chip = _rapport_action_chip({"label": "Tell me more", "send": "tell me more"})
        self.assertIsNotNone(chip)
        self.assertNotIn("goal", chip)

    def test_models_round_trip_backwards_compatible(self) -> None:
        from app.models import SendMessageRequest, UiActionRow

        # Old FE: plain text only — still valid.
        legacy = SendMessageRequest(message="find me neighbors")
        self.assertIsNone(legacy.goal)
        row = UiActionRow(id="a", label="L", message="m")
        self.assertIsNone(row.goal)
        # New FE: echoes the chip's structured goal back.
        tapped = SendMessageRequest.model_validate(
            {
                "message": "connect me with moms into playground time",
                "goal": {"kind": "find_neighbors", "topic": "playground time"},
            }
        )
        self.assertEqual(tapped.goal.kind, "find_neighbors")
        self.assertEqual(tapped.goal.topic, "playground time")
        row2 = UiActionRow.model_validate(
            {
                "id": "rapport_action",
                "label": "Meet playground-loving neighbors",
                "message": "connect me with moms into playground time",
                "goal": {"kind": "find_neighbors", "topic": "playground time"},
            }
        )
        self.assertEqual(row2.goal.kind, "find_neighbors")


if __name__ == "__main__":
    unittest.main()
