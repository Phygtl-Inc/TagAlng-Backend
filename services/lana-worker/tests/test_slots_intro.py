import unittest
from unittest.mock import patch

from app.discovery_route import PHASE_PREVIEW, _try_slots_intro_turn
from app.discovery_slots import slots_want_propose_intro


class TestSlotsIntro(unittest.TestCase):
    def test_slots_want_propose_intro(self) -> None:
        self.assertTrue(
            slots_want_propose_intro(
                {
                    "goal": "propose_intro",
                    "linear_intent": "social.propose_intro",
                    "intro_source": "block_log",
                    "intro_list_index": 1,
                    "confidence": 0.9,
                }
            )
        )
        self.assertFalse(
            slots_want_propose_intro({"goal": "peers", "confidence": 0.9})
        )

    @patch("app.discovery_route.propose_neighbor_intro")
    @patch("app.discovery_route.fetch_my_block_log")
    @patch("app.discovery_route.block_log_take_action")
    def test_slots_intro_block_log_index(
        self, mock_action, mock_log, mock_propose
    ) -> None:
        mock_log.return_value = [
            {
                "id": "entry-rain",
                "peer_user_id": "peer-rain",
                "match_type": "inbound_for_my_seek",
                "match_strength": 0.84,
                "peer_signal_detail": "rain coat",
            },
            {
                "id": "entry-bike",
                "peer_user_id": "swap-peer-1",
                "peer_preview_label": "A neighbor on your block",
                "match_type": "inbound_for_my_seek",
                "match_strength": 0.76,
                "peer_signal_detail": "kid bicycle",
                "my_signal_detail": "bicycle for my kid",
            },
        ]
        mock_propose.return_value = {
            "intro_id": "intro-swap-1",
            "candidate_user_id": "swap-peer-1",
        }
        slots = {
            "goal": "propose_intro",
            "linear_intent": "social.propose_intro",
            "intro_source": "block_log",
            "intro_list_index": 1,
            "confidence": 0.92,
        }
        session = {
            "active_intent": "discovery.block_log",
            "block_log_intro_list": [
                {
                    "entry_id": "entry-bike",
                    "peer_user_id": "swap-peer-1",
                    "match_type": "inbound_for_my_seek",
                    "peer_signal_detail": "kid bicycle",
                    "my_signal_detail": "bicycle for my kid",
                },
            ],
        }
        result = _try_slots_intro_turn(
            msg="introduce me to #1",
            slots=slots,
            session_ctx=session,
            ctx_base=dict(session),
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            phase=PHASE_PREVIEW,
            history=[
                {
                    "role": "assistant",
                    "content": "1. A neighbor — bicycle with good condition matches your ask",
                },
            ],
        )
        self.assertIsNotNone(result)
        mock_propose.assert_called_once()
        self.assertEqual(mock_propose.call_args.kwargs["candidate_user_id"], "swap-peer-1")
