import unittest
from unittest.mock import patch

from app.intro_proposal import (
    INTENT_PROPOSE_INTRO,
    accepts_intro_offer,
    build_match_reason,
    format_intro_offer_turn,
    pick_peer_for_intro,
    requested_peer_name,
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
        self.assertTrue(wants_neighbor_intro("introduce me to Kashaf"))
        self.assertFalse(wants_neighbor_intro("yes"))
        self.assertFalse(wants_neighbor_intro("yes introduce us"))
        self.assertFalse(wants_neighbor_intro("not now"))

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

    def test_format_intro_offer_turn_single_match(self) -> None:
        text = format_intro_offer_turn(
            {"nickname": "Kashaf", "matching_peer_label": "Lives on my block"},
            "You both fit lives on my block — you mentioned morning walks.",
        )
        self.assertIn("I think I found a fit — Kashaf", text)
        self.assertNotIn("I found 5 neighbors", text)
        self.assertIn("Want me to introduce you two?", text)

    def test_format_intro_offer_turn_does_not_double_echo_label(self) -> None:
        # Regression: the label was appended as its own sentence AND embedded in the
        # reason, producing "Married 10 years. You both fit married 10 years ...".
        peer = {"nickname": "Loka", "matching_peer_label": "Married 10 years"}
        reason = build_match_reason(identity_snippet="married 10 years", peer=peer)
        text = format_intro_offer_turn(peer, reason)
        self.assertEqual(text.lower().count("married 10 years"), 1)

    def test_build_match_reason_uses_first_clause_only(self) -> None:
        reason = build_match_reason(
            identity_snippet="morning runs; married; two kids",
            peer={"matching_peer_label": "Morning runners"},
        )
        self.assertIn("morning runs", reason.lower())
        self.assertNotIn("two kids", reason.lower())

    def test_build_match_reason_never_says_matched_you_with(self) -> None:
        # Lingo rule 4 — persisted to intros.match_reason, which the reply guard
        # never scans. The old fallback shipped "Lana matched you with Near you."
        for peer in (
            {"matching_peer_label": "Near you"},
            {"matching_peer_label": ""},
            {},
        ):
            for snippet in (None, "", "unrelated snippet the peer does not share"):
                reason = build_match_reason(identity_snippet=snippet, peer=peer)
                self.assertNotIn("matched you with", reason.lower(), reason)
                self.assertTrue(reason.strip(), "reason must never be empty")

    def test_build_match_reason_generic_label_not_echoed_as_trait(self) -> None:
        # "Near you" names proximity, not a shared trait — "You both fit near you."
        # is as garbled as the old string.
        reason = build_match_reason(
            identity_snippet="", peer={"matching_peer_label": "Near you"}
        )
        self.assertNotIn("near you.", reason.lower().replace("— ", ""))
        self.assertIn("click", reason.lower())

    def test_build_match_reason_scrubs_banned_claim_words(self) -> None:
        # Labels echo the users' own claim words, which can carry the banned lexicon.
        reason = build_match_reason(
            identity_snippet="", peer={"matching_peer_label": "Mom of two"}
        )
        self.assertNotIn("mom", reason.lower())

    def test_pick_peer_without_consent_or_name_returns_none(self) -> None:
        # Regression: a bare "Dana" (answering a name ask, misclassified as
        # propose_intro) auto-picked peers[0] and sent a real intro. With no
        # pending offer, no index, no matching name, and no intro phrasing in the
        # message, the picker must refuse so the caller clarifies.
        peers = [
            {"peer_user_id": "u1", "nickname": "Maria", "matching_peer_label": "Near you"},
            {"peer_user_id": "u2", "nickname": "Sofia", "matching_peer_label": "Near you"},
        ]
        self.assertIsNone(pick_peer_for_intro(peers, msg="Dana"))
        # Explicit consent still falls back to the top card.
        self.assertIsNotNone(pick_peer_for_intro(peers, msg="yes"))
        self.assertIsNotNone(pick_peer_for_intro(peers, msg="Can you introduce us?"))

    def test_requested_peer_name(self) -> None:
        self.assertEqual(requested_peer_name("introduce me to Kashaf"), "kashaf")
        self.assertIsNone(requested_peer_name("introduce me to neighbor 1"))

    def test_pick_peer_by_slot_name_not_regex(self) -> None:
        peers = [
            {"peer_user_id": "u1", "nickname": "Ada", "matching_peer_label": "Mom"},
            {"peer_user_id": "u2", "nickname": "Kashaf", "matching_peer_label": "Parent"},
        ]
        picked = pick_peer_for_intro(
            peers,
            msg="send an intro to kashaf",
            peer_name="kashaf",
        )
        self.assertEqual(picked["peer_user_id"], "u2")

    def test_pick_peer_by_name_not_first_default(self) -> None:
        peers = [
            {"peer_user_id": "u1", "nickname": "Natasha", "matching_peer_label": "Mom"},
            {"peer_user_id": "u2", "nickname": "Kashaf", "matching_peer_label": "Parent"},
        ]
        picked = pick_peer_for_intro(peers, msg="introduce me to kashaf")
        self.assertEqual(picked["peer_user_id"], "u2")
        missing = pick_peer_for_intro(peers, msg="introduce me to sofia")
        self.assertIsNone(missing)

    def test_pick_peer_for_intro_neighbor_index(self) -> None:
        peers = [
            {"peer_user_id": "u1", "matching_peer_label": "Pakistani Heritage"},
            {"peer_user_id": "u2", "matching_peer_label": "New Mom"},
        ]
        picked = pick_peer_for_intro(peers, msg="neighbor 1")
        self.assertEqual(picked["peer_user_id"], "u1")
        picked2 = pick_peer_for_intro(peers, msg="first one")
        self.assertEqual(picked2["peer_user_id"], "u1")

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

    def test_pending_does_not_override_name_in_message(self) -> None:
        peers = [
            {"peer_user_id": "u1", "nickname": "Ada", "matching_peer_label": "Mom"},
            {"peer_user_id": "u2", "nickname": "Kashaf", "matching_peer_label": "Parent"},
        ]
        pending = {
            "candidate_user_id": "u1",
            "candidate_nickname": "Ada",
            "matching_peer_label": "Mom",
        }
        picked = pick_peer_for_intro(
            peers,
            msg="send an intro to kashaf",
            pending=pending,
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["peer_user_id"], "u2")

    def test_pending_does_not_override_explicit_other_name(self) -> None:
        peers = [
            {"peer_user_id": "u1", "nickname": "Natasha", "matching_peer_label": "Mom"},
            {"peer_user_id": "u2", "nickname": "Kashaf", "matching_peer_label": "Brazilian"},
        ]
        pending = {
            "candidate_user_id": "u1",
            "candidate_nickname": "Natasha",
            "matching_peer_label": "Mom",
        }
        picked = pick_peer_for_intro(peers, msg="introduce me to kashaf", pending=pending)
        self.assertIsNotNone(picked)
        assert picked is not None
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
        # None is the delete-on-merge sentinel (see db.py _INTRO_STATE_NULL_DELETES):
        # the stale offer is dropped from the persisted session on merge.
        self.assertIsNone(ctx.get("pending_intro_offer"))
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
    def test_try_propose_intro_slots_peer_name_no_regex_phrase(self, mock_propose) -> None:
        mock_propose.return_value = {
            "intro_id": "intro-3",
            "candidate_user_id": "u2",
            "match_reason": "American moms match.",
            "status": "proposed",
        }
        peers = [
            {"peer_user_id": "u1", "nickname": "Ada"},
            {"peer_user_id": "u2", "nickname": "Kashaf"},
        ]
        result = try_propose_intro_from_preview(
            msg="send an intro to kashaf",
            session_ctx={},
            user_jwt="jwt",
            peers=peers,
            identity_snippet="american moms",
            force=True,
            peer_name="kashaf",
        )
        self.assertIsNotNone(result)
        reply, intro = result  # type: ignore[misc]
        self.assertIn("kashaf", reply.lower())
        self.assertEqual(intro["intro_id"], "intro-3")
        mock_propose.assert_called_once()
        self.assertEqual(mock_propose.call_args.kwargs["candidate_user_id"], "u2")

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
