import unittest
from unittest.mock import patch

from app.intro_list import (
    INTENT_LIST_INTROS,
    attach_pending_intros_after_propose,
    fetch_my_intros,
    format_duplicate_intro_reply,
    format_intros_list_reply,
    infer_intro_direction,
    intro_row_from_proposal,
    normalize_intro_row,
    stamp_pending_intros_ctx,
)
from app.ui_intent import UI_INTENT_SHOW_PENDING_INTROS, derive_ui_intent


class TestIntroList(unittest.TestCase):
    def test_normalize_intro_row(self) -> None:
        row = normalize_intro_row(
            {
                "id": "intro-1",
                "other_user_id": "u2",
                "nickname": "Sam",
                "avatar_url": "https://x/y.jpg",
                "created_at": "2026-06-01T12:00:00Z",
                "expires_at": "2026-06-04T12:00:00Z",
                "status": "proposed",
                "match_reason": "You both run mornings.",
                "shared_dimensions": ["running"],
                "direction": "sent",
            }
        )
        self.assertEqual(row["intro_id"], "intro-1")
        self.assertEqual(row["direction"], "sent")

    def test_format_empty(self) -> None:
        reply = format_intros_list_reply([])
        self.assertIn("don't have any pending intros", reply.lower())

    def test_format_with_rows(self) -> None:
        reply = format_intros_list_reply(
            [
                {
                    "nickname": "Sam",
                    "direction": "sent",
                    "match_reason": "Morning runners.",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            ]
        )
        self.assertIn("Sam", reply)
        self.assertIn("you sent", reply)

    def test_infer_direction(self) -> None:
        self.assertEqual(infer_intro_direction("show intros I sent"), "sent")
        self.assertEqual(infer_intro_direction("intros waiting on me"), "received")
        self.assertEqual(infer_intro_direction("show my pending intros"), "all")
        self.assertEqual(infer_intro_direction("show my intros"), "all")
        self.assertEqual(
            infer_intro_direction("show my intros", {"intro_direction": "sent"}),
            "all",
        )
        self.assertEqual(infer_intro_direction("what did you send to natasha"), "sent")

    @patch("app.intro_list.fetch_my_intros")
    def test_format_duplicate_intro_reply_received(self, mock_fetch) -> None:
        mock_fetch.return_value = [
            {
                "other_user_id": "peer-1",
                "direction": "received",
                "match_reason": "Pakistani Heritage",
            }
        ]
        reply = format_duplicate_intro_reply(
            peer={"peer_user_id": "peer-1", "nickname": "Kashaf"},
            user_jwt="jwt",
        )
        self.assertIn("Kashaf already introduced you", reply)
        self.assertIn("Pakistani Heritage", reply)

    @patch("app.intro_list.fetch_my_intros")
    def test_format_duplicate_intro_reply_sent(self, mock_fetch) -> None:
        mock_fetch.return_value = [
            {
                "other_user_id": "peer-1",
                "direction": "sent",
                "match_reason": "Mom",
            }
        ]
        reply = format_duplicate_intro_reply(
            peer={"peer_user_id": "peer-1", "nickname": "Natasha"},
            user_jwt="jwt",
        )
        self.assertIn("You already sent an intro to Natasha", reply)

    def test_stamp_pending_intros_ctx(self) -> None:
        ctx: dict = {}
        stamp_pending_intros_ctx(
            ctx,
            [
                {
                    "id": "i1",
                    "other_user_id": "u1",
                    "nickname": "Sam",
                    "status": "proposed",
                    "direction": "received",
                }
            ],
        )
        self.assertEqual(ctx["active_intent"], INTENT_LIST_INTROS)
        self.assertEqual(len(ctx["pending_intros"]), 1)
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_SHOW_PENDING_INTROS)

    def test_intro_row_from_proposal(self) -> None:
        row = intro_row_from_proposal(
            {"intro_id": "i1", "candidate_user_id": "u2", "match_reason": "Both moms"},
            {"nickname": "Ada", "matching_peer_label": "Block Resident"},
        )
        self.assertEqual(row["intro_id"], "i1")
        self.assertEqual(row["direction"], "sent")
        self.assertEqual(row["nickname"], "Ada")

    def test_attach_pending_intros_after_propose_fallback(self) -> None:
        ctx: dict = {"active_intent": "social.propose_intro"}
        attach_pending_intros_after_propose(
            ctx,
            user_jwt="jwt",
            intro={"intro_id": "i1", "candidate_user_id": "u2", "match_reason": "Swap match"},
            peer={"nickname": "Ada"},
        )
        self.assertEqual(len(ctx["pending_intros"]), 1)
        self.assertEqual(ctx["pending_intros"][0]["nickname"], "Ada")

    @patch("app.intro_list.call_rpc")
    def test_fetch_my_intros(self, mock_rpc) -> None:
        mock_rpc.return_value = [{"id": "i1", "nickname": "Sam", "direction": "sent"}]
        rows = fetch_my_intros("jwt", direction="sent")
        self.assertEqual(len(rows), 1)
        mock_rpc.assert_called_once_with("jwt", "get_my_intros", {"p_direction": "sent"})


if __name__ == "__main__":
    unittest.main()
