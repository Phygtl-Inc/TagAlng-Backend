import unittest
from unittest import mock

from app.intro_list import stamp_intro_respond_from_peer, stamp_pending_intros_ctx
from app.ui_actions import derive_ui_actions, intro_respond_actions
from app.ui_intent import (
    UI_INTENT_CHAT,
    UI_INTENT_OFFER_NEIGHBOR_INTRO,
    UI_INTENT_RESPOND_PENDING_INTRO,
    UI_INTENT_SHOW_BLOCK_LOG,
    UI_INTENT_SHOW_PENDING_INTROS,
    UI_INTENT_SIGNAL_SAVED,
    derive_ui_intent,
)


class TestUiActions(unittest.TestCase):
    def test_intro_respond_actions(self) -> None:
        actions = intro_respond_actions(nickname="Kashaf", intro_id="intro-1")
        self.assertEqual(actions[0]["id"], "intro_accept")
        self.assertEqual(actions[0]["message"], "yes introduce us")
        self.assertEqual(actions[0]["intro_id"], "intro-1")
        self.assertEqual(actions[1]["id"], "intro_decline")

    def test_derive_respond_pending_intro(self) -> None:
        ctx = {
            "pending_intro_respond": {
                "intro_id": "i1",
                "nickname": "Kashaf",
            },
        }
        actions = derive_ui_actions(ctx, UI_INTENT_RESPOND_PENDING_INTRO)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["message"], "yes introduce us")

    def test_derive_offer_neighbor_intro(self) -> None:
        ctx = {
            "pending_intro_offer": {
                "candidate_nickname": "Maria",
                "candidate_user_id": "u-maria",
            },
        }
        actions = derive_ui_actions(ctx, UI_INTENT_OFFER_NEIGHBOR_INTRO)
        self.assertEqual(actions[0]["id"], "intro_propose")
        self.assertIn("Maria", actions[0]["label"])
        self.assertEqual(actions[0]["peer_user_id"], "u-maria")

    def test_derive_signal_saved(self) -> None:
        actions = derive_ui_actions({}, UI_INTENT_SIGNAL_SAVED)
        self.assertEqual(actions[0]["id"], "signal_show_block_log")
        self.assertEqual(actions[0]["message"], "show my block log")
        self.assertEqual(actions[1]["id"], "signal_wait")

    def test_derive_signal_saved_host_meet(self) -> None:
        ctx = {
            "signal_saved": {
                "intent": "host_meet",
                "detail_text": "weekend walk",
                "matches_created": 2,
            }
        }
        actions = derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED)
        self.assertEqual(actions[0]["id"], "hosting_open")
        self.assertEqual(actions[0]["label"], "Open the meet up")

    def test_duplicate_intro_sent_shows_inbox_not_nudge(self) -> None:
        ctx = {
            "recent_intro_duplicate": {
                "candidate_user_id": "u1",
                "candidate_nickname": "Natasha",
            },
            "block_log_entries": [
                {"peer_preview_label": "Natasha", "peer_user_id": "u1"},
            ],
            "active_intent": "discovery.block_log",
        }
        actions = derive_ui_actions(ctx, UI_INTENT_SHOW_BLOCK_LOG)
        self.assertEqual(actions[0]["id"], "intro_show_inbox")
        self.assertEqual(actions[0]["label"], "Show my intros")

    def test_signal_saved_shows_block_log_not_stale_intro(self) -> None:
        ctx = {
            "recent_intro_duplicate": {
                "candidate_user_id": "u1",
                "candidate_nickname": "Natasha",
            },
            "signal_saved": {
                "intent": "swap_seek",
                "detail_text": "bicycle for my kid",
            },
            "active_intent": "looking.swap",
        }
        actions = derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED)
        self.assertEqual(actions[0]["id"], "signal_show_block_log")
        self.assertEqual(actions[0]["message"], "show my block log")

    def test_list_intros_beats_stale_respond_state(self) -> None:
        ctx = {
            "active_intent": "social.list_intros",
            "pending_intro_respond": {"intro_id": "stale", "nickname": "Kashaf"},
            "pending_intros": [
                {
                    "intro_id": "i1",
                    "nickname": "Ada",
                    "direction": "sent",
                    "status": "proposed",
                },
            ],
        }
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_SHOW_PENDING_INTROS)
        self.assertEqual(derive_ui_actions(ctx, UI_INTENT_SHOW_PENDING_INTROS), [])

    def test_stamp_pending_intros_attaches_row_actions(self) -> None:
        ctx: dict = {}
        stamp_pending_intros_ctx(
            ctx,
            [
                {
                    "id": "i1",
                    "direction": "received",
                    "nickname": "Kashaf",
                    "status": "proposed",
                },
            ],
        )
        row = ctx["pending_intros"][0]
        self.assertEqual(len(row["actions"]), 2)
        self.assertEqual(derive_ui_actions(ctx, UI_INTENT_SHOW_PENDING_INTROS), [])

    def test_derive_ui_intent_respond_pending(self) -> None:
        intent = derive_ui_intent(
            {"pending_intro_respond": {"intro_id": "i1"}, "active_intent": "tier.respond_nudge"},
            phone_verified=True,
        )
        self.assertEqual(intent, UI_INTENT_RESPOND_PENDING_INTRO)

    def test_derive_ui_intent_after_intro_accepted(self) -> None:
        intent = derive_ui_intent(
            {"active_intent": "tier.respond_nudge"},
            phone_verified=True,
        )
        self.assertEqual(intent, UI_INTENT_CHAT)
        self.assertEqual(derive_ui_actions({}, UI_INTENT_RESPOND_PENDING_INTRO), [])

    def test_respond_beats_stale_offer(self) -> None:
        intent = derive_ui_intent(
            {
                "pending_intro_respond": {"intro_id": "i1", "nickname": "Kashaf"},
                "pending_intro_offer": {"candidate_user_id": "u2"},
            },
            phone_verified=True,
        )
        self.assertEqual(intent, UI_INTENT_RESPOND_PENDING_INTRO)

    def test_signal_saved_beats_stale_intro(self) -> None:
        intent = derive_ui_intent(
            {
                "pending_intro_respond": {"intro_id": "i1", "nickname": "Kashaf"},
                "signal_saved": {"detail_text": "3t rain boots", "intent": "swap_seek"},
                "active_intent": "looking.swap",
            },
            phone_verified=True,
        )
        self.assertEqual(intent, UI_INTENT_SIGNAL_SAVED)

    def test_block_log_beats_stale_intro(self) -> None:
        from app.ui_intent import UI_INTENT_SHOW_BLOCK_LOG

        intent = derive_ui_intent(
            {
                "pending_intro_respond": {"intro_id": "i1", "nickname": "Kashaf"},
                "active_intent": "discovery.block_log",
                "block_log_entries": [{"entry_id": "e1"}],
            },
            phone_verified=True,
        )
        self.assertEqual(intent, UI_INTENT_SHOW_BLOCK_LOG)
        actions = derive_ui_actions(
            {
                "block_log_entries": [
                    {
                        "peer_preview_label": "A neighbor on your block",
                        "peer_user_id": "u-peer",
                    },
                ],
            },
            UI_INTENT_SHOW_BLOCK_LOG,
        )
        self.assertEqual(actions[0]["message"], "introduce me to #1")
        self.assertEqual(actions[0]["peer_user_id"], "u-peer")

    @mock.patch("app.intro_list.fetch_my_intros")
    def test_stamp_intro_respond_from_peer(self, mock_fetch: object) -> None:
        mock_fetch.return_value = [
            {
                "id": "i1",
                "other_user_id": "u2",
                "nickname": "Kashaf",
                "direction": "received",
                "status": "proposed",
            },
        ]
        ctx: dict = {}
        ok = stamp_intro_respond_from_peer(
            ctx,
            user_jwt="jwt",
            peer={"peer_user_id": "u2", "nickname": "Kashaf"},
        )
        self.assertTrue(ok)
        self.assertEqual(ctx["active_intent"], "tier.respond_nudge")
        self.assertNotIn("actions", ctx["pending_intros"][0])
