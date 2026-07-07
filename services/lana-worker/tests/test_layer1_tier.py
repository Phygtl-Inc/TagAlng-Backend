import unittest
from unittest.mock import patch

from app.intro_proposal import wants_neighbor_intro
from app.layer1_tier import (
    is_standalone_affirmation,
    is_standalone_negation,
    parse_nudge_response,
    wants_respond_intro,
)


class TestStandaloneNegation(unittest.TestCase):
    def test_bare_negations(self) -> None:
        for m in ("no", "Nope.", "that's all", "no more", "I'm good", "nothing else"):
            self.assertTrue(is_standalone_negation(m), m)

    def test_not_negations(self) -> None:
        for m in ("no, decline the intro from Ada", "I'm Pakistani", "Daniel"):
            self.assertFalse(is_standalone_negation(m), m)

    def test_affirmation_and_negation_disjoint(self) -> None:
        self.assertTrue(is_standalone_affirmation("yes"))
        self.assertFalse(is_standalone_negation("yes"))


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

    def test_resolve_nudge_action_prefers_ai(self) -> None:
        from app.layer1_tier import resolve_nudge_action

        # AI reads an ambiguous reply the regex would have mis-accepted.
        with patch(
            "app.intro_response_ai.interpret_nudge_response", return_value="decline"
        ):
            self.assertEqual(resolve_nudge_action("ok but who is it first"), "decline")
        # New "introduce me to X" is never an accept/decline of a pending intro.
        with patch(
            "app.intro_response_ai.interpret_nudge_response", return_value="accept"
        ) as m:
            self.assertEqual(resolve_nudge_action("introduce me to Kashaf"), "unknown")
            m.assert_not_called()

    def test_resolve_nudge_action_falls_back_to_regex(self) -> None:
        from app.layer1_tier import resolve_nudge_action

        with patch("app.intro_response_ai.interpret_nudge_response", return_value=None):
            self.assertEqual(resolve_nudge_action("not now"), "decline")
            self.assertEqual(resolve_nudge_action("yes introduce us"), "accept")


class TestRespondNudgeRouting(unittest.TestCase):
    @patch("app.discovery_route.handle_respond_nudge")
    def test_respond_runs_before_propose(self, mock_handle: object) -> None:
        # A real intro waiting in session engages the handler; the AI-read accept
        # is routed as tier.respond_nudge.
        from app.discovery_route import _try_respond_nudge_turn

        mock_handle.return_value = ("Done — connected.", None, "accept")
        result = _try_respond_nudge_turn(
            msg="yes introduce us",
            session_ctx={"pending_intro_respond": {"intro_id": "i1", "nickname": "Ada"}},
            user_jwt="jwt",
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNotNone(result)
        reply, _ctx, _routing, peers = result
        self.assertIn("connected", reply)
        self.assertEqual(peers, [])
        mock_handle.assert_called_once()

    @patch("app.discovery_route.handle_respond_nudge")
    def test_no_pending_intro_never_engages(self, mock_handle: object) -> None:
        # Nothing waiting in session → fall through without touching the handler,
        # so no message can reach the "I don't see a pending intro" dead-end.
        from app.discovery_route import _try_respond_nudge_turn

        result = _try_respond_nudge_turn(
            msg="no thanks",
            session_ctx={},
            user_jwt="jwt",
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNone(result)
        mock_handle.assert_not_called()

    @patch("app.discovery_route.handle_respond_nudge")
    def test_unrelated_question_with_pending_falls_through(self, mock_handle: object) -> None:
        # An intro is waiting, but the reply is a question — the AI reads it as
        # unclear ("prompt"), so we fall through to normal routing and leave the
        # pending intro in session rather than nagging.
        from app.discovery_route import _try_respond_nudge_turn

        mock_handle.return_value = ("Ada sent an intro — accept, not now, or block?", None, "prompt")
        result = _try_respond_nudge_turn(
            msg="which languages can I speak?",
            session_ctx={"pending_intro_respond": {"intro_id": "i1", "nickname": "Ada"}},
            user_jwt="jwt",
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNone(result)


class TestDismissIntroPass(unittest.TestCase):
    def test_dismiss_offer_not_now(self) -> None:
        from app.discovery_route import _try_dismiss_intro_pass_turn

        result = _try_dismiss_intro_pass_turn(
            msg="not now",
            session_ctx={
                "pending_intro_offer": {
                    "candidate_user_id": "u1",
                    "candidate_nickname": "Natasha",
                },
            },
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNotNone(result)
        reply, ctx, _routing, peers = result
        self.assertIn("No problem", reply)
        self.assertEqual(peers, [])
        self.assertIsNone(ctx.get("pending_intro_offer"))

    def test_dismiss_duplicate_not_now(self) -> None:
        from app.discovery_route import _try_dismiss_intro_pass_turn

        result = _try_dismiss_intro_pass_turn(
            msg="not now",
            session_ctx={
                "recent_intro_duplicate": {
                    "candidate_user_id": "u1",
                    "candidate_nickname": "Natasha",
                },
            },
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNotNone(result)

    def test_dismiss_skipped_when_received_intro_pending(self) -> None:
        from app.discovery_route import _try_dismiss_intro_pass_turn

        result = _try_dismiss_intro_pass_turn(
            msg="not now",
            session_ctx={
                "pending_intro_respond": {"intro_id": "i1", "nickname": "Ada"},
                "pending_intro_offer": {"candidate_user_id": "u1"},
            },
            phone_verified=True,
            phase="preview",
        )
        self.assertIsNone(result)


class TestAiSlotsBlockProposeIntro(unittest.TestCase):
    def test_find_in_block_blocks_stale_intro(self) -> None:
        from app.discovery_route import _ai_slots_block_propose_intro

        self.assertTrue(
            _ai_slots_block_propose_intro(
                "what is happening on my block",
                {
                    "linear_intent": "discovery.find_in_block",
                    "goal": "peers",
                    "confidence": 0.9,
                },
            )
        )

    def test_propose_intro_still_allowed(self) -> None:
        from app.discovery_route import _ai_slots_block_propose_intro

        self.assertFalse(
            _ai_slots_block_propose_intro(
                "introduce me to Natasha",
                {
                    "goal": "propose_intro",
                    "linear_intent": "social.propose_intro",
                    "confidence": 0.9,
                },
            )
        )
