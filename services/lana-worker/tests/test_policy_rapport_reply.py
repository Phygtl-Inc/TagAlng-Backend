"""_policy_rapport_reply — the unified policy (decide_turn) owns the REPLY to a
rapport-thread answer; the mini-model concierge is fallback-only (QA 2026-07-29:
it bypassed the bridge policy and hallucinated a "language learning" close)."""

import unittest
from unittest.mock import patch

from app.lana_unified_pipeline import _policy_rapport_reply
from app.policy.decide import NextAction
from app.turn_timing import TurnTimer


def _run(session_ctx: dict, **overrides):
    kwargs = dict(
        user_id="u1",
        session_id="s1",
        session_ctx=session_ctx,
        history=[{"role": "assistant", "content": "What languages do you speak?"}],
        user_message="English is my language of choice",
        timer=TurnTimer(),
    )
    kwargs.update(overrides)
    return _policy_rapport_reply(**kwargs)


class TestPolicyRapportReply(unittest.TestCase):
    @patch("app.policy.decide.audit_decision")
    @patch("app.policy.decide.decide_turn")
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_policy_reply_owns_turn_and_clears_rapport(self, _mode, mock_decide, mock_audit):
        mock_decide.return_value = NextAction(
            kind="bridge_offer",
            utterance="Nice — noted! Want to see what's happening near you?",
            chips=[{"label": "See events", "send": "show me events near me"}],
        )
        ctx = {"rapport_active": True, "rapport_followup_question": "What languages do you speak?"}
        result = _run(ctx)
        self.assertIsNotNone(result)
        reply, status, out_ctx, ui, _draft = result
        self.assertIn("Want to see", reply)
        self.assertEqual(status, "continue")
        # capture cleared with None (never popped) so the session merge can't resurrect it
        self.assertIsNone(out_ctx["rapport_active"])
        self.assertIsNone(out_ctx["rapport_followup_question"])
        self.assertEqual(out_ctx["policy_chip_msgs"], ["show me events near me"])
        self.assertEqual(out_ctx["last_routing"]["outcome"], "decide_turn")
        # policy replies must NOT opt out of the final-mile localizer — the
        # trust-based skip shipped mixed-language replies (QA 2026-07-30)
        self.assertFalse(out_ctx.get("_reply_localized"))
        mock_audit.assert_called_once()

    @patch("app.policy.decide.audit_decision")
    @patch("app.policy.decide.decide_turn")
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_answered_ask_seeds_streak_and_reply_clears_it(self, _mode, mock_decide, _audit):
        # The rapport question being answered counts as ask #1 (seeded before the
        # policy runs, visible in its payload); a non-ask decision then clears it.
        # The ctx dict is mutated in place after the call, so capture the seeded
        # value at call time.
        seen: dict = {}

        def _capture(**kwargs):
            seen["streak"] = kwargs["session_ctx"].get("policy_ask_streak")
            return NextAction(kind="reply", utterance="No worries!")

        mock_decide.side_effect = _capture
        result = _run({})
        self.assertIsNotNone(result)
        self.assertEqual(seen["streak"], 1)
        self.assertIsNone(result[2]["policy_ask_streak"])

    @patch("app.policy.decide.audit_decision")
    @patch("app.policy.decide.decide_turn")
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_stacked_ask_extends_streak(self, _mode, mock_decide, _audit):
        mock_decide.return_value = NextAction(
            kind="ground_place", utterance="Which spot do you go to?"
        )
        result = _run({})
        self.assertIsNotNone(result)
        # seeded to 1 (the answered rapport ask) + the policy asked again → 2
        self.assertEqual(result[2]["policy_ask_streak"], 2)

    @patch("app.policy.decide.audit_decision")
    @patch("app.policy.decide.decide_turn")
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_tile_question_reaches_policy(self, _mode, mock_decide, _audit):
        # The tile question lives on the home screen, not in chat history —
        # QA 2026-07-29: "why are u asking this" after the language tile got an
        # explanation of the NAME ask because the policy never saw the question.
        mock_decide.return_value = NextAction(kind="reply", utterance="Fair question!")
        result = _run(
            {},
            user_message="why are u asking this",
            rapport_question="What languages do you speak?",
        )
        self.assertIsNotNone(result)
        self.assertEqual(
            mock_decide.call_args.kwargs["answering_question"],
            "What languages do you speak?",
        )

    @patch("app.policy.decide.decide_turn")
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_guest_turn_still_reaches_policy(self, _mode, mock_decide):
        # No phone_verified gate here on purpose — one voice for guests too.
        mock_decide.return_value = NextAction(kind="reply", utterance="Love that!")
        with patch("app.policy.decide.audit_decision"):
            result = _run({"phone_verified": False})
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "Love that!")

    @patch("app.policy.decide.decide_turn")
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_handoff_falls_back_to_concierge(self, _mode, mock_decide):
        mock_decide.return_value = NextAction(kind="handoff", utterance="")
        self.assertIsNone(_run({}))

    @patch("app.policy.decide.decide_turn", return_value=None)
    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_no_decision_falls_back_to_concierge(self, _mode, _decide):
        self.assertIsNone(_run({}))

    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="off")
    def test_policy_off_falls_back_to_concierge(self, _mode):
        self.assertIsNone(_run({}))

    @patch("app.lana_unified_pipeline.decide_turn_mode", return_value="on")
    def test_host_flow_and_pending_confirmation_stay_legacy(self, _mode):
        self.assertIsNone(_run({"event_host_active": True}))
        self.assertIsNone(_run({"pending_confirmation": {"kind": "cancel_event"}}))


if __name__ == "__main__":
    unittest.main()
