import unittest
from unittest.mock import patch

from app.discovery_route import (
    PHASE_NEED_IDENTITY,
    PHASE_NEED_ZIP,
    PHASE_PREVIEW,
    extract_identity_snippet,
    extract_zip,
    format_preview_message,
    handle_discovery_turn,
    wants_more_peer_detail,
)
from app.lana_dispatch import lana_unified_opening


class TestDiscoveryHelpers(unittest.TestCase):
    def test_extract_zip(self) -> None:
        self.assertEqual(extract_zip("I'm in 32827"), "32827")

    def test_extract_identity(self) -> None:
        self.assertIn("Latino", extract_identity_snippet("I'm a Latino mom new here") or "")

    def test_wants_more(self) -> None:
        self.assertTrue(wants_more_peer_detail("show me their names"))


class TestDiscoveryRouting(unittest.TestCase):
    def test_find_peers_asks_zip(self) -> None:
        result = handle_discovery_turn(
            "find people like me",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertIn("ZIP", reply)
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_ZIP)
        self.assertEqual(peers, [])

    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_zip_then_identity(self, mock_blocks) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        result = handle_discovery_turn(
            "32827",
            session_ctx={"active_intent": "discovery.find_peers", "routing_phase": PHASE_NEED_ZIP},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], PHASE_NEED_IDENTITY)
        self.assertIn("one thing", reply.lower())

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    @patch("app.discovery_route.fetch_blocks_for_zip")
    def test_preview_redacted(self, mock_blocks, mock_preview) -> None:
        mock_blocks.return_value = [{"block_id": "block-1", "label": "Whisper Park"}]
        mock_preview.return_value = [
            {"matching_peer_label": "Weekend activities", "preview": True},
        ]
        result = handle_discovery_turn(
            "I'm a Latino mom looking for friends",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_IDENTITY,
                "preview_block_id": "block-1",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        _, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertTrue(peers[0].get("preview"))

    def test_more_details_gates_verify(self) -> None:
        result = handle_discovery_turn(
            "show me names",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "latino mom",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("verify", reply.lower())


class TestUnifiedOpening(unittest.TestCase):
    def test_opening(self) -> None:
        opening, status, ctx, _ = lana_unified_opening()
        self.assertEqual(status, "continue")
        self.assertTrue(ctx.get("unified_mode"))
        self.assertIn("concierge", opening.lower())


if __name__ == "__main__":
    unittest.main()
