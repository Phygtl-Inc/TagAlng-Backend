import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.discovery_route import (
    PHASE_NEED_ZIP,
    PHASE_PREVIEW,
    handle_discovery_turn,
)
from app.local_signals import (
    format_block_log_reply,
    format_signal_saved_reply,
    normalize_signal_intent,
    save_local_signal,
)


class TestLocalSignalsHelpers(unittest.TestCase):
    def test_normalize_signal_intent(self) -> None:
        self.assertEqual(normalize_signal_intent("swap_seek"), "swap_seek")
        self.assertIsNone(normalize_signal_intent("invalid"))

    def test_format_signal_saved_reply(self) -> None:
        reply = format_signal_saved_reply(
            {"intent": "swap_seek", "matches_created": 2},
            detail="3T rain boots",
        )
        self.assertIn("rain boots", reply)
        self.assertIn("2 new matches", reply)

    def test_format_block_log_empty(self) -> None:
        self.assertIn("quiet", format_block_log_reply([]))

    @patch("app.local_signals.call_rpc")
    def test_save_local_signal_falls_back_to_legacy_detail_param(self, mock_call_rpc) -> None:
        mock_call_rpc.side_effect = [
            HTTPException(
                status_code=502,
                detail='rpc_failed:{"code":"PGRST202","details":"... p_detail_text ... no matches were"}',
            ),
            {"signal_id": "sig-1", "intent": "swap_seek", "detail_text": "rain boots", "matches_created": 0},
        ]
        result = save_local_signal(
            "jwt",
            intent="swap_seek",
            detail_text="rain boots",
            block_id="block-1",
        )
        self.assertEqual(result.get("signal_id"), "sig-1")
        self.assertEqual(mock_call_rpc.call_count, 2)
        first_payload = mock_call_rpc.call_args_list[0].args[2]
        second_payload = mock_call_rpc.call_args_list[1].args[2]
        self.assertIn("p_detail_text", first_payload)
        self.assertIn("p_detail", second_payload)


class TestDiscoverySignalRouting(unittest.TestCase):
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.save_local_signal")
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_save_signal_fast_path(
        self, mock_slots, mock_save, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "goal": "save_signal",
            "in_discovery": True,
            "confidence": 0.9,
            "signal_intent": "swap_seek",
            "signal_detail": "3T rain boots",
            "signal_category": "clothing",
        }
        mock_save.return_value = {
            "signal_id": "sig-1",
            "intent": "swap_seek",
            "detail_text": "3T rain boots",
            "matches_created": 0,
        }
        reply, ctx, routing, peers = handle_discovery_turn(
            "I'm looking for 3T rain boots",
            session_ctx={"preview_block_id": "block-a", "routing_phase": PHASE_PREVIEW},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-a",
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("rain boots", reply)
        self.assertEqual(ctx.get("active_intent"), "looking.swap")
        self.assertIn("signal_saved", ctx)
        mock_save.assert_called_once()

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.fetch_my_block_log")
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_show_block_log_fast_path(
        self, mock_slots, mock_fetch, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "goal": "show_block_log",
            "in_discovery": True,
            "confidence": 0.85,
        }
        mock_fetch.return_value = [
            {
                "id": "e1",
                "peer_preview_label": "Sam",
                "match_strength": 0.82,
                "match_reasons": ["Same block neighbor"],
            }
        ]
        reply, ctx, routing, peers = handle_discovery_turn(
            "show my block log",
            session_ctx={"routing_phase": PHASE_PREVIEW},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-a",
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("Sam", reply)
        self.assertEqual(ctx.get("active_intent"), "discovery.block_log")
        self.assertIn("block_log_entries", ctx)

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_save_signal_needs_zip(self, mock_slots, _mock_ai) -> None:
        mock_slots.return_value = {
            "goal": "save_signal",
            "in_discovery": True,
            "confidence": 0.9,
            "signal_intent": "tip_seek",
            "signal_detail": "pediatrician",
        }
        reply, ctx, routing, peers = handle_discovery_turn(
            "know a good pediatrician?",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id=None,
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("ZIP", reply)
        self.assertEqual(ctx.get("routing_phase"), PHASE_NEED_ZIP)


if __name__ == "__main__":
    unittest.main()
