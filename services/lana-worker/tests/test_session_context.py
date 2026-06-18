import unittest

from app.db import merge_session_context
from app.signal_capture import should_abandon_signal_draft


class TestMergeSessionContext(unittest.TestCase):
    def test_clears_signal_draft_when_none(self) -> None:
        merged = merge_session_context(
            {"signal_draft": {"detail": "rain boots", "phase": "signal_confirm_missing"}},
            {"signal_saved": {"detail_text": "rain boots (3T)"}, "signal_draft": None},
        )
        self.assertNotIn("signal_draft", merged)
        self.assertIn("signal_saved", merged)

    def test_drops_stale_block_log_when_turn_omits_it(self) -> None:
        merged = merge_session_context(
            {
                "block_log_entries": [{"entry_id": "e1"}],
                "active_intent": "discovery.block_log",
            },
            {"active_intent": "looking.meet", "signal_saved": {"detail_text": "walking buddy"}},
        )
        self.assertNotIn("block_log_entries", merged)
        self.assertIn("signal_saved", merged)

    def test_keeps_signal_draft_when_updated(self) -> None:
        draft = {"detail": "boots", "phase": "signal_confirm_missing"}
        merged = merge_session_context({"signal_draft": {"detail": "old"}}, {"signal_draft": draft})
        self.assertEqual(merged["signal_draft"], draft)

    def test_clears_stale_intro_respond_when_listing_inbox(self) -> None:
        from app.intro_list import stamp_pending_intros_ctx

        turn_ctx: dict = {}
        stamp_pending_intros_ctx(
            turn_ctx,
            [
                {
                    "id": "i1",
                    "nickname": "Ada",
                    "direction": "sent",
                    "status": "proposed",
                },
            ],
        )
        merged = merge_session_context(
            {
                "pending_intro_respond": {
                    "intro_id": "stale",
                    "nickname": "Kashaf",
                },
                "active_intent": "tier.respond_nudge",
            },
            turn_ctx,
        )
        self.assertNotIn("pending_intro_respond", merged)
        self.assertEqual(merged["active_intent"], "social.list_intros")
        self.assertEqual(merged["pending_intros"][0]["nickname"], "Ada")

    def test_clears_pending_intro_offer_when_none(self) -> None:
        merged = merge_session_context(
            {
                "pending_intro_offer": {
                    "candidate_user_id": "u1",
                    "candidate_nickname": "Natasha",
                },
                "intro_offer_shown": True,
            },
            {
                "recent_intro_duplicate": {
                    "candidate_user_id": "u1",
                    "candidate_nickname": "Natasha",
                },
                "pending_intro_offer": None,
                "intro_offer_shown": None,
            },
        )
        self.assertNotIn("pending_intro_offer", merged)
        self.assertNotIn("intro_offer_shown", merged)
        self.assertIn("recent_intro_duplicate", merged)


class TestSignalDraftAbandon(unittest.TestCase):
    def test_abandon_on_bicycle_typo_while_confirming_size(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "stage",
            "detail": "looking for rain boots",
            "linear_intent": "looking.swap",
            "intent": "swap_seek",
        }
        self.assertTrue(
            should_abandon_signal_draft("i wanna buy a bycycle", draft, slots={})
        )

    def test_abandon_on_for_my_kid_not_a_size(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "stage",
            "detail": "buy a bicycle",
            "linear_intent": "looking.swap",
            "intent": "swap_seek",
        }
        self.assertTrue(should_abandon_signal_draft("for my kid", draft, slots={}))

    def test_abandon_on_bicycle_while_confirming_size(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "stage",
            "detail": "looking for rain boots",
            "linear_intent": "looking.swap",
            "intent": "swap_seek",
        }
        self.assertTrue(
            should_abandon_signal_draft("I wanna swap my kid bicycle", draft, slots={})
        )

    def test_abandon_on_pizza_question(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "stage",
            "detail": "looking for rain boots",
            "linear_intent": "looking.swap",
            "intent": "swap_seek",
        }
        self.assertTrue(
            should_abandon_signal_draft("do you know a good pizza shop?", draft, slots={})
        )

    def test_abandon_when_ai_extracts_new_detail(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "stage",
            "detail": "i wanna swap my rain boots",
            "linear_intent": "looking.swap",
            "intent": "swap_seek",
        }
        slots = {
            "linear_intent": "looking.swap",
            "signal_detail": "I'm looking for a kids bicycle",
            "confidence": 0.9,
        }
        self.assertTrue(
            should_abandon_signal_draft(
                "I'm looking for a kids bicycle", draft, slots=slots
            )
        )

    def test_keep_short_size_answer(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "stage",
            "detail": "looking for rain boots",
            "linear_intent": "looking.swap",
            "intent": "swap_seek",
        }
        self.assertFalse(should_abandon_signal_draft("3T", draft, slots={}))

    def test_keep_category_answer_despite_ai_reclassify(self) -> None:
        draft = {
            "phase": "signal_confirm_missing",
            "confirm_field": "category",
            "detail": "good restaurant near me",
            "linear_intent": "looking.tip",
            "intent": "tip_seek",
        }
        slots = {
            "linear_intent": "sharing.tip",
            "signal_intent": "tip_share",
            "confidence": 0.9,
        }
        self.assertFalse(
            should_abandon_signal_draft("food", draft, slots=slots),
        )


if __name__ == "__main__":
    unittest.main()
