import unittest
from unittest.mock import patch

from app.claims_persist import ClaimExtractResult
from app.layer1_handlers import persist_identity_from_message


class TestPersistIdentityFromMessage(unittest.TestCase):
    def test_verify_gate_without_user(self) -> None:
        res = persist_identity_from_message(None, "I'm Pakistani", linear_intent="identity.add_claim")
        self.assertTrue(res.verify_gate)
        self.assertEqual(res.saved, 0)

    @patch("app.layer1_handlers.try_upsert_claims_from_message")
    def test_persists_claims_kids_and_nickname(self, mock_upsert) -> None:
        mock_upsert.return_value = ClaimExtractResult(
            saved=3, nickname="Drake", kids_count=2
        )
        res = persist_identity_from_message(
            "user-1",
            "I'm a pakistani triathlon athlete, 2 kids",
            linear_intent="identity.add_claim",
        )
        self.assertFalse(res.verify_gate)
        self.assertEqual(res.saved, 3)
        self.assertEqual(res.kids_count, 2)
        self.assertEqual(res.nickname, "Drake")
        self.assertIsNone(res.conflict)

    @patch("app.layer1_handlers.dismiss_claims_from_edit_message", return_value=1)
    @patch("app.layer1_handlers.try_upsert_claims_from_message")
    def test_edit_intent_counts_dismissals(self, mock_upsert, _mock_dismiss) -> None:
        mock_upsert.return_value = ClaimExtractResult(saved=1)
        res = persist_identity_from_message(
            "user-1", "remove italian heritage", linear_intent="identity.edit_claim"
        )
        self.assertEqual(res.dismissed, 1)
        self.assertEqual(res.total, 2)

    @patch("app.layer1_handlers.try_upsert_claims_from_message")
    def test_heritage_conflict_surfaces_prompt(self, mock_upsert) -> None:
        mock_upsert.return_value = ClaimExtractResult(
            saved=0,
            heritage_conflict={
                "from_label": "Brazilian Heritage",
                "label": "Sicilian Heritage",
                "claim": {
                    "concept": "sicilian_heritage",
                    "label": "Sicilian Heritage",
                    "confidence": 0.9,
                    "bucket": "heritage",
                },
            },
        )
        res = persist_identity_from_message(
            "user-1", "actually I'm Sicilian", linear_intent="identity.add_claim"
        )
        self.assertIsNotNone(res.conflict)
        self.assertIsInstance(res.conflict_prompt, str)
        self.assertIn("Brazilian", res.conflict_prompt)


class TestIdentityConversationalReply(unittest.TestCase):
    @patch("app.discovery_route.persist_profile_patch")
    @patch("app.discovery_route.lana_profile_turn")
    @patch("app.discovery_route.format_profile_intake_context", return_value="CTX")
    @patch("app.discovery_route.load_event_draft_context", return_value={})
    def test_reply_persists_ai_captured_name(
        self, _load, _fmt, mock_turn, mock_patch
    ) -> None:
        from app.discovery_route import _identity_conversational_reply

        # Engine read a bare "Drake" as the name (in context) and proposes a follow-up.
        mock_turn.return_value = (
            "Nice to meet you, Drake! Road or trail?",
            "continue",
            {"profile_patch": {"nickname": "Drake"}, "topics_covered": ["activity"]},
            {"highlights": []},
        )
        ctx: dict = {}
        reply = _identity_conversational_reply(
            user_id="user-1",
            msg="Drake",
            history=[{"role": "assistant", "content": "what should neighbors call you?"}],
            session_ctx={},
            ctx=ctx,
        )
        self.assertIn("Drake", reply)
        mock_patch.assert_called_once_with("user-1", {"nickname": "Drake"})
        self.assertEqual(ctx["profile_turn_status"], "continue")

    @patch("app.discovery_route.persist_profile_patch")
    @patch(
        "app.discovery_route.lana_profile_turn",
        side_effect=RuntimeError("vertex down"),
    )
    @patch("app.discovery_route.format_profile_intake_context", return_value="CTX")
    @patch("app.discovery_route.load_event_draft_context", return_value={})
    def test_falls_back_when_engine_fails(self, _load, _fmt, _turn, mock_patch) -> None:
        from app.discovery_route import _identity_conversational_reply

        reply = _identity_conversational_reply(
            user_id="user-1",
            msg="I'm Pakistani",
            history=[],
            session_ctx={},
            ctx={},
        )
        self.assertTrue(reply)
        mock_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
