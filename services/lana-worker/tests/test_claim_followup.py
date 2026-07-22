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


class _Res:
    """Minimal IdentityPersistResult stand-in."""

    def __init__(self, saved=1, primary_label=None, primary_bucket=None) -> None:
        self.saved = saved
        self.primary_label = primary_label
        self.primary_bucket = primary_bucket


class TestClaimConciergeReply(unittest.TestCase):
    @patch("app.rapport_reply.rapport_concierge_reply")
    def test_offer_arms_rapport_continuation(self, mock_concierge) -> None:
        from app.discovery_route import _claim_concierge_reply

        action = {
            "kind": "find_activities",
            "label": "Search badminton activities",
            "topic": "badminton",
            "send": "show me badminton activities on my block",
        }
        mock_concierge.return_value = {
            "reply": "Badminton — love it! Want me to look for badminton meets nearby?",
            "options": [],
            "action": action,
            "language_offer": [],
        }
        ctx: dict = {}
        reply = _claim_concierge_reply(
            user_id="user-1",
            msg="i like badminton",
            res=_Res(saved=1, primary_label="Badminton", primary_bucket="interests"),
            known_labels=[],
            session_ctx={},
            ctx=ctx,
        )
        self.assertIn("Badminton", reply)
        # The chip renders from rapport_reply and the accept dispatches next turn.
        self.assertEqual(ctx["rapport_reply"], {"options": [], "action": action})
        self.assertTrue(ctx["rapport_offer_pending"])
        self.assertEqual(ctx["rapport_pending_action"], action)
        self.assertTrue(ctx["rapport_active"])

    @patch("app.rapport_reply.rapport_concierge_reply")
    def test_repeat_claim_flagged_already_known(self, mock_concierge) -> None:
        from app.discovery_route import _claim_concierge_reply

        mock_concierge.return_value = {
            "reply": "I remember — badminton's your thing!",
            "options": [],
            "action": None,
            "language_offer": [],
        }
        ctx: dict = {}
        _claim_concierge_reply(
            user_id="user-1",
            msg="i like badminton",
            res=_Res(saved=1, primary_label="Badminton", primary_bucket="interests"),
            known_labels=["Badminton", "Two Kids"],
            session_ctx={},
            ctx=ctx,
        )
        self.assertTrue(mock_concierge.call_args.kwargs["already_known"])
        # A warm close clears any stale rapport capture keys (None survives the merge).
        self.assertIsNone(ctx["rapport_active"])
        self.assertIsNone(ctx["rapport_reply"])

    @patch(
        "app.rapport_reply.rapport_concierge_reply",
        side_effect=RuntimeError("llm down"),
    )
    def test_falls_back_when_concierge_fails(self, _mock) -> None:
        from app.discovery_route import _claim_concierge_reply

        reply = _claim_concierge_reply(
            user_id="user-1",
            msg="I'm Pakistani",
            res=_Res(saved=1, primary_label="Pakistani Heritage"),
            known_labels=[],
            session_ctx={},
            ctx={},
        )
        self.assertTrue(reply)


class TestRapportCaptureContext(unittest.TestCase):
    def test_pending_question_reaches_classifier(self) -> None:
        # The classifier must SEE the question Lana asked, so a bare noun-phrase answer
        # ("local cricket grounds" to "where do you like to play?") reads as an ANSWER,
        # not a confident tip_seek that releases into a real search + posted block ask.
        from app.discovery_slots import _active_capture_context

        ctx = {
            "rapport_active": True,
            "rapport_followup_question": "Do you have a favorite spot nearby where you like to play?",
        }
        note = _active_capture_context(ctx)
        self.assertIn("favorite spot nearby", note)
        self.assertIn("NOUN PHRASE", note)

    def test_no_question_still_covers_bare_answers(self) -> None:
        from app.discovery_slots import _active_capture_context

        note = _active_capture_context({"rapport_active": True})
        self.assertNotIn("pending question", note)
        self.assertIn("NOUN PHRASE", note)


if __name__ == "__main__":
    unittest.main()
