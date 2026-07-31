"""Hosting bubble CTAs — open vs send-to-mom."""

from __future__ import annotations

import unittest
from unittest import mock

from app.hosting_cta import (
    handle_hosting_open_turn,
    is_hosting_open_cta,
    is_hosting_send_mom_cta,
    session_has_hosting_offer,
)
from app.ui_actions import derive_ui_actions
from app.ui_intent import UI_INTENT_SIGNAL_SAVED


class TestHostingCta(unittest.TestCase):
    def test_cta_message_detection(self) -> None:
        self.assertTrue(is_hosting_open_cta("open the meet up"))
        self.assertTrue(is_hosting_open_cta("open the meetup"))
        self.assertTrue(is_hosting_send_mom_cta("send to a mom"))

    def test_session_has_hosting_offer(self) -> None:
        self.assertTrue(
            session_has_hosting_offer(
                {"signal_saved": {"intent": "host_meet", "detail_text": "coffee"}},
            )
        )

    def test_open_hides_repeat_ctas(self) -> None:
        ctx = {
            "signal_saved": {
                "intent": "host_meet",
                "detail_text": "brazilian coffee",
                "hosting_opened": True,
            }
        }
        self.assertEqual(derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED), [])

    def test_open_marks_hosting_opened(self) -> None:
        session = {
            "signal_saved": {
                "intent": "host_meet",
                "detail_text": "brazilian coffee this weekend",
                "signal_id": "s1",
            },
        }
        with mock.patch(
            "app.hosting_cta.refresh_my_signal_matches",
            return_value=0,
        ), mock.patch(
            "app.hosting_cta.fetch_my_block_log",
            return_value=[],
        ):
            reply, ctx, _routing, peers = handle_hosting_open_turn(
                session_ctx=session,
                user_jwt="jwt",
                phone_verified=True,
                phase="preview",
            )
        self.assertIn("open to neighbors nearby", reply.lower())
        self.assertTrue(ctx["signal_saved"]["hosting_opened"])
        self.assertEqual(ctx["signal_saved"]["hosting"]["status_label"], "Open to neighbors nearby")
        self.assertEqual(peers, [])
