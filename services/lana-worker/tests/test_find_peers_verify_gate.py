import unittest
from unittest.mock import patch

from app.orchestrator.tools import execute_tool


class TestFindPeersVerifyGate(unittest.TestCase):
    @patch("app.discovery_route.fetch_preview_peers_on_block")
    def test_unverified_preview_sets_await_signup_phone(self, mock_peers) -> None:
        mock_peers.return_value = [{"matching_peer_label": "shared interests", "preview": True}]
        result = execute_tool(
            tool_name="find_peers",
            tool_args={},
            user_id="user-1",
            user_jwt="jwt",
            session_id="sess-1",
            block_id="block-1",
            purpose="lana",
            session_ctx={"routing_phase": "preview", "phone_verified": False},
            source_module="orchestrator",
        )
        self.assertEqual(result["routing_phase"], "await_signup_phone")
        self.assertTrue(result["requires_phone_verification"])

    @patch("app.discovery_route.fetch_preview_peers_on_block")
    def test_first_preview_without_gate_stays_preview(self, mock_peers) -> None:
        mock_peers.return_value = [{"matching_peer_label": "shared interests", "preview": True}]
        result = execute_tool(
            tool_name="find_peers",
            tool_args={},
            user_id="user-1",
            user_jwt="jwt",
            session_id="sess-1",
            block_id="block-1",
            purpose="lana",
            session_ctx={"routing_phase": "need_identity", "phone_verified": False},
            source_module="orchestrator",
        )
        self.assertEqual(result["routing_phase"], "preview")
        self.assertFalse(result["requires_phone_verification"])
