import unittest
from unittest.mock import patch

from app.intro_proposal import wants_neighbor_intro
from app.layer1_tier import parse_nudge_response, wants_respond_intro


class TestLayer1Tier(unittest.TestCase):
    def test_wants_respond_intro(self) -> None:
        self.assertTrue(wants_respond_intro("yes introduce us"))
        self.assertTrue(wants_respond_intro("Yes introduce us."))
        self.assertTrue(wants_respond_intro("accept"))
        self.assertTrue(wants_respond_intro("not now"))
        self.assertFalse(wants_respond_intro("introduce me to Kashaf"))
        self.assertFalse(wants_respond_intro("connect me to Ada"))

    def test_parse_nudge_response_ignores_propose_phrases(self) -> None:
        self.assertEqual(parse_nudge_response("introduce me to Kashaf"), "unknown")
        self.assertEqual(parse_nudge_response("yes introduce us"), "accept")

    def test_parse_nudge_response_ignores_geographic_block(self) -> None:
        self.assertEqual(parse_nudge_response("can you find kashaf in my block?"), "unknown")
        self.assertEqual(parse_nudge_response("find sofia from my block"), "unknown")
        self.assertEqual(parse_nudge_response("block them please"), "block")

    def test_wants_respond_intro_ignores_find_on_block(self) -> None:
        self.assertFalse(wants_respond_intro("can you find kashaf in my block?"))
        self.assertFalse(wants_respond_intro("find sofia from my block"))

    def test_yes_introduce_us_not_neighbor_intro(self) -> None:
        self.assertFalse(wants_neighbor_intro("yes introduce us"))


class TestRespondNudgeRouting(unittest.TestCase):
    @patch("app.discovery_route.handle_respond_nudge")
    @patch("app.discovery_route.wants_respond_intro", return_value=True)
    def test_respond_runs_before_propose(self, _wants: object, mock_handle: object) -> None:
        from app.discovery_route import _try_respond_nudge_turn

        mock_handle.return_value = ("Done — connected.", None, "accept")
        result = _try_respond_nudge_turn(
            msg="yes introduce us",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNotNone(result)
        reply, _ctx, _routing, peers = result
        self.assertIn("connected", reply)
        self.assertEqual(peers, [])
        mock_handle.assert_called_once()
