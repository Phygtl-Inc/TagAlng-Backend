import unittest
from unittest.mock import patch

from app.discovery_route import (
    PHASE_NEED_IDENTITY,
    PHASE_NEED_ZIP,
    PHASE_PREVIEW,
    extract_zip,
    format_preview_message,
    handle_discovery_turn,
    invalid_zip_hint,
    wants_discovery_turn,
    wants_more_peer_detail,
    wants_verify_help,
)
from app.lana_dispatch import lana_unified_opening


class TestDiscoveryHelpers(unittest.TestCase):
    def test_extract_zip(self) -> None:
        self.assertEqual(extract_zip("I'm in 32827"), "32827")
        self.assertIsNone(extract_zip("3116"))
        self.assertIsNone(extract_zip("0000123"))

    def test_invalid_zip_hint(self) -> None:
        self.assertIn("4 digits", invalid_zip_hint("3116") or "")
        self.assertIn("5-digit", invalid_zip_hint("0000123") or "")
        self.assertIsNone(invalid_zip_hint("hello"))

    def test_wants_more(self) -> None:
        self.assertTrue(wants_more_peer_detail("show me their names"))

    def test_wants_verify_help(self) -> None:
        self.assertTrue(wants_verify_help("How can I verify"))
        self.assertTrue(wants_verify_help("verify my phone please"))


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
    def test_short_zip_gives_hint(self, _mock_blocks) -> None:
        result = handle_discovery_turn(
            "3116",
            session_ctx={"routing_phase": PHASE_NEED_ZIP, "active_intent": "discovery.find_peers"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, _, _, _ = result
        self.assertIn("5-digit", reply)

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

    def test_verify_question_gates_phone(self) -> None:
        result = handle_discovery_turn(
            "How can I verify",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("number", reply.lower())

    def test_need_zip_meta_question_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "are you real?",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_NEED_ZIP,
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)
        self.assertFalse(
            wants_discovery_turn(
                "are you real?",
                {"routing_phase": PHASE_NEED_ZIP, "active_intent": "discovery.find_peers"},
            )
        )

    def test_listening_insult_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "are you dumb?",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)

    def test_preview_confusion_passes_to_orchestrator(self) -> None:
        result = handle_discovery_turn(
            "What are you saying",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNone(result)
        self.assertFalse(
            wants_discovery_turn(
                "What are you saying",
                {"routing_phase": PHASE_PREVIEW, "preview_block_id": "block-1"},
            )
        )

    @patch("app.discovery_route.fetch_preview_events_on_block")
    def test_rsvp_in_preview_gates_verify(self, mock_events) -> None:
        mock_events.return_value = [{"title": "Sunday brunch", "starts_at": "2026-06-15"}]
        result = handle_discovery_turn(
            "I want to take part in Sunday brunch",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, _ = result
        self.assertEqual(ctx["routing_phase"], "await_signup_phone")
        self.assertIn("Sunday brunch", reply)

    @patch("app.discovery_route.fetch_preview_events_on_block")
    def test_activities_in_preview_not_peer_loop(self, mock_events) -> None:
        mock_events.return_value = [{"title": "Park walk", "venue_name": "Central"}]
        result = handle_discovery_turn(
            "I want to find activities",
            session_ctx={
                "active_intent": "discovery.find_peers",
                "routing_phase": PHASE_PREVIEW,
                "preview_block_id": "block-1",
                "identity_snippet": "brazilian",
            },
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, ctx, _, peers = result
        self.assertEqual(ctx["routing_phase"], PHASE_PREVIEW)
        self.assertIn("Park walk", reply)
        self.assertEqual(peers, [])


class TestUnifiedOpening(unittest.TestCase):
    def test_opening(self) -> None:
        opening, status, ctx, _ = lana_unified_opening()
        self.assertEqual(status, "continue")
        self.assertTrue(ctx.get("unified_mode"))
        self.assertIn("concierge", opening.lower())


if __name__ == "__main__":
    unittest.main()
