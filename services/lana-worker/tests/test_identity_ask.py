"""Identity gate: saved claims answer "who are you"; the ask is AI-authored."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.identity_ask import (
    FALLBACK_ASKS,
    compose_identity_ask,
    identity_from_saved_claims,
)


def _claims_client(rows: list[dict]) -> MagicMock:
    sb = MagicMock()
    chain = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    # Two .order() hops: confidence, then created_at as the stable tie-break — an
    # unordered tie re-ordered the user's own identity line between turns.
    chain = chain.is_.return_value.order.return_value.order.return_value.limit.return_value
    chain.execute.return_value = MagicMock(data=rows)
    return sb


class TestIdentityFromSavedClaims(unittest.TestCase):
    def test_none_without_user_id(self) -> None:
        self.assertIsNone(identity_from_saved_claims(None))

    @patch("app.identity_ask.service_client")
    def test_joins_top_labels(self, mock_client) -> None:
        mock_client.return_value = _claims_client(
            [{"label": "Speaks Urdu and Spanish"}, {"label": "Mother"}, {"label": "Mother"}]
        )
        self.assertEqual(
            identity_from_saved_claims("u1"), "Speaks Urdu and Spanish · Mother"
        )

    @patch("app.identity_ask.service_client")
    def test_none_when_no_claims(self, mock_client) -> None:
        mock_client.return_value = _claims_client([])
        self.assertIsNone(identity_from_saved_claims("u1"))

    @patch("app.identity_ask.service_client", side_effect=RuntimeError("db down"))
    def test_none_on_db_error(self, _mock_client) -> None:
        self.assertIsNone(identity_from_saved_claims("u1"))


class TestComposeIdentityAsk(unittest.TestCase):
    @patch("app.orchestrator.llm.llm_configured", return_value=False)
    def test_fallback_when_llm_unconfigured(self, _cfg) -> None:
        self.assertEqual(
            compose_identity_ask(msg="show me nearby people"), FALLBACK_ASKS["match"]
        )
        self.assertEqual(
            compose_identity_ask(msg="introduce me", purpose="intro"),
            FALLBACK_ASKS["intro"],
        )

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    @patch("app.orchestrator.llm.llm_json")
    def test_uses_ai_ask(self, mock_json, _cfg, _model) -> None:
        mock_json.return_value = {
            "ask": "What brings you out to meet neighbors — kids, hobbies, or heritage?"
        }
        ask = compose_identity_ask(msg="show me nearby people")
        self.assertIn("neighbors", ask)
        self.assertNotEqual(ask, FALLBACK_ASKS["match"])

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    @patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("llm down"))
    def test_fallback_on_llm_error(self, _json, _cfg, _model) -> None:
        self.assertEqual(
            compose_identity_ask(msg="show me nearby people"), FALLBACK_ASKS["match"]
        )

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    @patch("app.orchestrator.llm.llm_json", return_value={"ask": "hi"})
    def test_fallback_on_degenerate_ask(self, _json, _cfg, _model) -> None:
        self.assertEqual(
            compose_identity_ask(msg="show me nearby people"), FALLBACK_ASKS["match"]
        )


class TestFindPeersToolSeedsIdentity(unittest.TestCase):
    # The tool now goes through the shared blended fetch (radius + onion + the
    # community filter), not the bare vector RPC — same list the chat lane gets.
    @patch("app.identity_ask.identity_from_saved_claims", return_value="Mother · Runner")
    @patch("app.discovery_route._fetch_verified_peer_matches", return_value=[])
    def test_signed_in_user_with_claims_is_not_blocked(self, _fetch, _seed) -> None:
        from app.orchestrator.tools import execute_tool

        result = execute_tool(
            tool_name="find_peers",
            tool_args={},
            user_id="user-1",
            user_jwt="jwt",
            session_id="sess-1",
            block_id="block-1",
            purpose="lana",
            session_ctx={"phone_verified": True},
            source_module="orchestrator",
        )
        self.assertNotEqual(result.get("reason"), "need_identity")
        self.assertEqual(result.get("status"), "ok")

    @patch("app.identity_ask.identity_from_saved_claims", return_value=None)
    def test_no_claims_still_blocks(self, _seed) -> None:
        from app.orchestrator.tools import execute_tool

        result = execute_tool(
            tool_name="find_peers",
            tool_args={},
            user_id="user-1",
            user_jwt="jwt",
            session_id="sess-1",
            block_id="block-1",
            purpose="lana",
            session_ctx={"phone_verified": True},
            source_module="orchestrator",
        )
        self.assertEqual(result.get("reason"), "need_identity")


if __name__ == "__main__":
    unittest.main()
