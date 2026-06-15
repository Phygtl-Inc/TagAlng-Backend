import unittest
from unittest.mock import patch

from app.intro_proposal import (
    INTENT_PROPOSE_INTRO,
    accepts_intro_offer,
    build_match_reason,
    pick_peer_for_intro,
    stamp_intro_offer_ctx,
    stamp_intro_proposal_ctx,
    try_propose_intro_from_preview,
    wants_neighbor_intro,
)
from app.ui_intent import (
    UI_INTENT_OFFER_NEIGHBOR_INTRO,
    UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
    derive_ui_intent,
)


class TestIntroProposalHelpers(unittest.TestCase):
    def test_wants_neighbor_intro(self) -> None:
        self.assertTrue(wants_neighbor_intro("Can you introduce us?"))
        self.assertFalse(wants_neighbor_intro("yes"))

    def test_accepts_intro_offer(self) -> None:
        self.assertTrue(accepts_intro_offer("yes"))
        self.assertTrue(accepts_intro_offer("Sure."))
        self.assertFalse(accepts_intro_offer("maybe later"))

    def test_build_match_reason(self) -> None:
        reason = build_match_reason(
            identity_snippet="morning runs",
            peer={"matching_peer_label": "Morning runners"},
        )
        self.assertIn("morning runs", reason.lower())

    def test_pick_peer_for_intro_pending(self) -> None:
        peers = [
            {"peer_user_id": "u1", "matching_peer_label": "Runner"},
            {"peer_user_id": "u2", "matching_peer_label": "Parent"},
        ]
        picked = pick_peer_for_intro(
            peers,
            msg="yes",
            pending={"candidate_user_id": "u2"},
        )
        self.assertEqual(picked["peer_user_id"], "u2")

    def test_stamp_intro_offer_ctx(self) -> None:
        ctx: dict = {}
        stamp_intro_offer_ctx(
            ctx,
            peer={"peer_user_id": "u1", "nickname": "Sam", "matching_peer_label": "Runner"},
            match_reason="You both run mornings.",
        )
        self.assertEqual(ctx["active_intent"], INTENT_PROPOSE_INTRO)
        self.assertEqual(ctx["pending_intro_offer"]["candidate_user_id"], "u1")
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_OFFER_NEIGHBOR_INTRO)

    def test_stamp_intro_proposal_ctx(self) -> None:
        ctx: dict = {"pending_intro_offer": {"candidate_user_id": "u1"}}
        stamp_intro_proposal_ctx(
            ctx,
            intro={
                "intro_id": "intro-1",
                "nudge_id": "nudge-1",
                "candidate_user_id": "u1",
                "match_reason": "Shared morning runs.",
                "status": "proposed",
            },
            peer={"peer_user_id": "u1", "nickname": "Sam", "matching_peer_label": "Runner"},
        )
        self.assertNotIn("pending_intro_offer", ctx)
        self.assertEqual(ctx["intro_proposal"]["intro_id"], "intro-1")
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_PROPOSE_NEIGHBOR_INTRO)

    @patch("app.intro_proposal.propose_neighbor_intro")
    def test_try_propose_intro_force(self, mock_propose) -> None:
        mock_propose.return_value = {
            "intro_id": "intro-1",
            "candidate_user_id": "u1",
            "match_reason": "You both run mornings.",
            "status": "proposed",
        }
        peers = [{"peer_user_id": "u1", "nickname": "Sam", "matching_peer_label": "Runner"}]
        result = try_propose_intro_from_preview(
            msg="ok",
            session_ctx={},
            user_jwt="jwt",
            peers=peers,
            identity_snippet="morning runs",
            force=True,
        )
        self.assertIsNotNone(result)
        reply, intro = result  # type: ignore[misc]
        self.assertIn("introduced", reply.lower())
        self.assertEqual(intro["intro_id"], "intro-1")
        mock_propose.assert_called_once()

    @patch("app.intro_proposal.propose_neighbor_intro")
    def test_try_propose_intro_pending_yes(self, mock_propose) -> None:
        mock_propose.return_value = {
            "intro_id": "intro-2",
            "candidate_user_id": "u1",
            "match_reason": "Shared morning runs.",
            "status": "proposed",
        }
        peers = [{"peer_user_id": "u1", "nickname": "Sam"}]
        result = try_propose_intro_from_preview(
            msg="yes",
            session_ctx={
                "pending_intro_offer": {
                    "candidate_user_id": "u1",
                    "match_reason": "Shared morning runs.",
                }
            },
            user_jwt="jwt",
            peers=peers,
            identity_snippet=None,
        )
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
