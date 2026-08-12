"""Aug-10 retest fixes: the CTA-publish reply must not narrate a back-out (§4.3), and
peer-match prose must never quote a percentage the card doesn't render (§4.5).

Pure functions only — no LLM, no DB.
"""

import unittest

from app.lana_unified_pipeline import _event_published_reply
from app.layer1_handlers import peers_to_match_rows


class TestCtaPublishReply(unittest.TestCase):
    """"Drop the meet up" is a publish button. The upstream composer reads it as backing
    out and writes an abandon line; it must not ride along above the live meet."""

    DRAFT = {"title": "Badminton Tuesdays"}

    def test_cta_publish_drops_the_blind_upstream_line(self):
        abandon = (
            "Got it, I've noted that you'd like to drop the meetup. If you want to set "
            "up something new or chat more about it, just let me know."
        )
        reply = _event_published_reply(abandon, self.DRAFT, cta_driven=True)
        self.assertNotIn("drop the meetup", reply)
        self.assertIn("Badminton Tuesdays", reply)
        self.assertIn("live in your area", reply)

    def test_free_text_publish_keeps_the_brains_warm_ack(self):
        # Typed "publícalo" — the host brain knew it was publishing, so its line stands.
        ack = "Perfect, posting it now."
        reply = _event_published_reply(ack, self.DRAFT, cta_driven=False)
        self.assertIn(ack, reply)
        self.assertIn("live in your area", reply)

    def test_a_trailing_question_is_still_dropped(self):
        reply = _event_published_reply(
            "Where will the jog start?", self.DRAFT, cta_driven=False
        )
        self.assertNotIn("?", reply)


class TestPeerRowsCarryTheCardsBadge(unittest.TestCase):
    """The card renders match_badge, never a number. The prose reads the same field, so
    it can't fall back to quoting similarity_score as a percentage the user can't see."""

    def test_scored_row_gets_the_badge_the_card_shows(self):
        rows = peers_to_match_rows(
            [
                {
                    "peer_user_id": "u1",
                    "nickname": "Tim",
                    "similarity_score": 0.72,
                    "matching_peer_label": "Plays tennis",
                    "has_exact_concept_match": False,
                }
            ],
            phone_verified=True,
        )
        self.assertEqual(len(rows), 1)
        # 0.72 is the "partial" band with nothing provably shared — PARTIAL, not "72%".
        self.assertEqual(rows[0]["match_badge"], "PARTIAL")

    def test_unscored_neighbor_gets_no_badge(self):
        rows = peers_to_match_rows(
            [{"peer_user_id": "u2", "nickname": "Ana", "similarity_score": None}],
            phone_verified=True,
        )
        self.assertIsNone(rows[0]["match_badge"])


if __name__ == "__main__":
    unittest.main()
