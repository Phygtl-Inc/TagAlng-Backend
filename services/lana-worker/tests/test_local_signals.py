import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.discovery_route import (
    PHASE_NEED_ZIP,
    PHASE_PREVIEW,
    handle_discovery_turn,
)
from app.local_signals import (
    block_log_match_summary,
    fetch_my_block_log,
    filter_block_log_for_signal,
    format_block_log_reply,
    format_signal_saved_reply,
    normalize_signal_intent,
    refresh_my_signal_matches,
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
        self.assertIn("2 match", reply)

    def test_format_signal_saved_reply_lists_entries(self) -> None:
        reply = format_signal_saved_reply(
            {"intent": "swap_seek", "matches_created": 1},
            detail="bicycle for my kid",
            matches_shown=1,
            entries=[{
                "peer_preview_label": "Kashaf",
                "peer_signal_intent": "swap_offer",
                "peer_signal_detail": "kids bicycle",
                "my_signal_intent": "swap_seek",
                "my_signal_detail": "bicycle for my kid",
            }],
        )
        self.assertIn("Kashaf", reply)
        self.assertIn("kids bicycle", reply)

    def test_format_block_log_empty(self) -> None:
        self.assertIn("quiet", format_block_log_reply([]))

    def test_block_log_match_summary_meet_host(self) -> None:
        summary = block_log_match_summary({
            "match_type": "meet_invite_potential",
            "peer_signal_intent": "meet_seek",
            "peer_signal_detail": "weekend stroller walk",
            "my_signal_intent": "host_meet",
            "my_signal_detail": "Saturday coffee at Foxtail",
        })
        self.assertIn("weekend stroller walk", summary)
        self.assertIn("Saturday coffee at Foxtail", summary)

    def test_block_log_match_summary_prefers_peer_offer(self) -> None:
        summary = block_log_match_summary({
            "match_type": "inbound_for_my_seek",
            "peer_signal_intent": "swap_offer",
            "peer_signal_detail": "kids bicycle",
            "my_signal_intent": "swap_seek",
            "my_signal_detail": "bicycle for my kid",
        })
        self.assertIn("offering", summary.lower())
        self.assertIn("kids bicycle", summary)
        self.assertNotIn("You're looking for", summary)

    def test_block_log_match_summary_uses_reason_when_peer_empty(self) -> None:
        summary = block_log_match_summary({
            "match_type": "inbound_for_my_seek",
            "my_signal_intent": "swap_seek",
            "my_signal_detail": "bicycle for my kid",
            "match_reasons": ["kids bicycle matches your ask: bicycle for my kid"],
        })
        self.assertIn("kids bicycle", summary)
        self.assertNotIn("You're looking for", summary)

    def test_format_block_log_with_signal_details(self) -> None:
        reply = format_block_log_reply([
            {
                "match_type": "meet_invite_potential",
                "peer_preview_label": "A neighbor on your block",
                "match_strength": 0.84,
                "peer_signal_intent": "meet_seek",
                "peer_signal_detail": "playgroup for toddlers",
                "my_signal_intent": "host_meet",
                "my_signal_detail": "backyard meetup Sunday",
            }
        ])
        self.assertIn("playgroup for toddlers", reply)
        self.assertIn("backyard meetup Sunday", reply)
        self.assertIn("84%", reply)

    def test_filter_block_log_for_swap_offer(self) -> None:
        rows = [
            {"match_type": "inbound_for_my_offer", "peer_preview_label": "Sam"},
            {"match_type": "meet_invite_potential", "peer_preview_label": "Alex"},
        ]
        filtered = filter_block_log_for_signal(rows, signal_intent="swap_offer")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].get("peer_preview_label"), "Sam")

    def test_filter_block_log_for_saved_detail_only(self) -> None:
        rows = [
            {
                "match_type": "inbound_for_my_seek",
                "my_signal_detail": "buy a bicycle (for my kid)",
                "peer_signal_detail": "kids bicycle",
            },
            {
                "match_type": "inbound_for_my_seek",
                "my_signal_detail": "3t rain boots",
                "peer_signal_detail": "rain boots 3T",
            },
        ]
        filtered = filter_block_log_for_signal(
            rows,
            signal_intent="swap_seek",
            detail_text="3t rain boots",
        )
        self.assertEqual(len(filtered), 1)
        self.assertIn("rain boots", str(filtered[0].get("peer_signal_detail") or ""))

    @patch("app.local_signals.call_rpc")
    def test_fetch_my_block_log_refreshes_before_read(self, mock_rpc) -> None:
        mock_rpc.side_effect = [3, [{"id": "e1", "peer_preview_label": "Sam"}]]
        rows = fetch_my_block_log("jwt")
        self.assertEqual(len(rows), 1)
        self.assertEqual(mock_rpc.call_count, 2)
        mock_rpc.assert_any_call("jwt", "refresh_my_signal_matches", {})
        mock_rpc.assert_any_call("jwt", "get_my_block_log", {})

    @patch("app.local_signals.call_rpc")
    def test_refresh_my_signal_matches_returns_zero_on_missing_rpc(self, mock_rpc) -> None:
        mock_rpc.side_effect = HTTPException(status_code=502, detail="rpc_failed")
        self.assertEqual(refresh_my_signal_matches("jwt"), 0)

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
        self.assertFalse(ctx.get("block_log_entries"))
        mock_save.assert_called_once()

    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.save_local_signal")
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_save_signal_clears_stale_block_log(
        self, mock_slots, mock_save, _mock_ai
    ) -> None:
        mock_slots.return_value = {
            "goal": "save_signal",
            "in_discovery": True,
            "confidence": 0.9,
            "signal_intent": "meet_seek",
            "signal_detail": "walking buddy — weekend",
            "linear_intent": "looking.meet",
        }
        mock_save.return_value = {
            "signal_id": "sig-2",
            "intent": "meet_seek",
            "detail_text": "walking buddy — weekend",
            "matches_created": 0,
        }
        _reply, ctx, _, _ = handle_discovery_turn(
            "walking buddy on weekends",
            session_ctx={
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-a",
                "block_log_entries": [{"entry_id": "stale", "peer_preview_label": "Old"}],
                "active_intent": "discovery.block_log",
            },
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-a",
            is_anonymous=False,
            history=[],
            user_id="user-1",
        )
        self.assertEqual(ctx.get("active_intent"), "looking.meet")
        self.assertIn("signal_saved", ctx)
        self.assertFalse(ctx.get("block_log_entries"))

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
