import unittest
from unittest.mock import patch

from app.orchestrator.lana_chat_fast_path import (
    lana_chat_routing_from_discovery,
    should_skip_lana_router,
)
from app.orchestrator.pipeline import run_turn


class LanaChatFastPathTests(unittest.TestCase):
    def _chat_slots(self, **overrides) -> dict:
        slots = {
            "goal": "chat",
            "in_discovery": False,
            "confidence": 0.9,
        }
        slots.update(overrides)
        return slots

    def _session(self, msg: str, **overrides) -> dict:
        ctx = {
            "unified_mode": True,
            "routing_phase": "listening",
            "_discovery_slots_for": msg,
            "_discovery_slots": self._chat_slots(),
        }
        ctx.update(overrides)
        return ctx

    def test_skip_when_discovery_said_chat(self) -> None:
        self.assertTrue(
            should_skip_lana_router(
                purpose="lana",
                utterance="are you real?",
                session_ctx=self._session("are you real?"),
            )
        )

    def test_no_skip_when_goal_peers(self) -> None:
        ctx = self._session("find neighbors")
        ctx["_discovery_slots"] = self._chat_slots(goal="peers", in_discovery=True)
        self.assertFalse(
            should_skip_lana_router(
                purpose="lana",
                utterance="find neighbors",
                session_ctx=ctx,
            )
        )

    def test_no_skip_when_hosting_activity(self) -> None:
        self.assertFalse(
            should_skip_lana_router(
                purpose="lana",
                utterance="help me host a coffee meetup",
                session_ctx=self._session("help me host a coffee meetup"),
            )
        )

    def test_no_skip_when_stale_slots(self) -> None:
        self.assertFalse(
            should_skip_lana_router(
                purpose="lana",
                utterance="hello",
                session_ctx=self._session("different message"),
            )
        )

    def test_no_skip_low_confidence(self) -> None:
        ctx = self._session("hi")
        ctx["_discovery_slots"] = self._chat_slots(confidence=0.3)
        self.assertFalse(
            should_skip_lana_router(purpose="lana", utterance="hi", session_ctx=ctx)
        )

    def test_preview_pushback_can_skip(self) -> None:
        ctx = self._session("why moms not dads?", routing_phase="preview")
        ctx["peer_matches"] = [{"matching_peer_label": "Mom of toddlers", "preview": True}]
        self.assertTrue(
            should_skip_lana_router(
                purpose="lana",
                utterance="why moms not dads?",
                session_ctx=ctx,
            )
        )

    def test_routing_stub_is_respond_only(self) -> None:
        routing = lana_chat_routing_from_discovery(self._chat_slots())
        self.assertEqual(routing["outcome"], "R")
        self.assertIsNone(routing["tool_to_call"])
        self.assertEqual(routing["intent_class"], "companionship")

    @patch("app.orchestrator.pipeline.synthesize_turn")
    @patch("app.orchestrator.pipeline.route_turn")
    @patch("app.orchestrator.pipeline.prefetch_turn_memories", return_value=[])
    @patch("app.orchestrator.pipeline.load_user_context")
    def test_run_turn_skips_router_on_chat(
        self, mock_ctx, _mock_prefetch, mock_route, mock_synth
    ) -> None:
        mock_ctx.return_value = {
            "home_block_id": None,
            "existing_claims": [],
            "block_network": {},
            "relationship_tiers": {},
            "event_purpose_ids": [],
        }
        mock_synth.return_value = (
            "Hey — I'm Lana, your block concierge.",
            "continue",
            {},
            {"bucket": None, "focus_phrase": None, "highlights": []},
            None,
        )
        msg = "what is my name?"
        run_turn(
            user_id="user-1",
            session_id="sess-1",
            purpose="lana",
            history=[],
            user_message=msg,
            session_ctx=self._session(msg),
            timer=None,
        )
        mock_route.assert_not_called()
        mock_synth.assert_called_once()
        routing_passed = mock_synth.call_args.kwargs["routing"]
        self.assertEqual(routing_passed["outcome"], "R")
        self.assertIsNone(routing_passed.get("tool_to_call"))


if __name__ == "__main__":
    unittest.main()
