import unittest
from unittest.mock import patch

from app.intro_list import (
    INTENT_LIST_INTROS,
    fetch_my_intros,
    format_intros_list_reply,
    infer_intro_direction,
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

    @patch("app.intro_list.call_rpc")
    def test_fetch_my_intros(self, mock_rpc) -> None:
        mock_rpc.return_value = [{"id": "i1", "nickname": "Sam", "direction": "sent"}]
        rows = fetch_my_intros("jwt", direction="sent")
        self.assertEqual(len(rows), 1)
        mock_rpc.assert_called_once_with("jwt", "get_my_intros", {"p_direction": "sent"})


if __name__ == "__main__":
    unittest.main()
