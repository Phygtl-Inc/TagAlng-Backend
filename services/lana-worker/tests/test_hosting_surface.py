"""C-4-EVENT-P3 hosting draft card for unified chat."""

from __future__ import annotations

import unittest

from app.hosting_surface import attach_hosting_to_signal_saved, build_hosting_draft
from app.local_signals import stamp_signal_saved_ctx
from app.ui_actions import derive_ui_actions, hosting_open_actions
from app.ui_intent import UI_INTENT_SIGNAL_SAVED


class TestHostingSurface(unittest.TestCase):
    def test_build_hosting_draft_parses_when_where(self) -> None:
        draft = build_hosting_draft(
            detail_text="Brazilian coffee Saturday morning at Foxtail",
            when_hint="Saturday morning",
            block_name="Lake Nona",
            matches_created=3,
        )
        self.assertIn("Brazilian coffee", draft["title"])
        self.assertEqual(draft["when_label"], "Saturday morning")
        self.assertIn("Foxtail", str(draft["where_label"]))
        self.assertIn("3 closest", draft["outreach_copy"])

    def test_stamp_host_meet_attaches_hosting(self) -> None:
        ctx: dict = {}
        stamp_signal_saved_ctx(
            ctx,
            {
                "signal_id": "s1",
                "intent": "host_meet",
                "detail_text": "weekend walking meetup — Saturday morning",
                "matches_created": 2,
            },
            active_intent="sharing.host",
            when_hint="Saturday morning",
        )
        saved = ctx["signal_saved"]
        self.assertEqual(saved["intent"], "host_meet")
        hosting = saved.get("hosting")
        self.assertIsInstance(hosting, dict)
        self.assertEqual(hosting.get("status_label"), "Ready to open it up")
        self.assertIn("Saturday morning", str(hosting.get("when_label")))

    def test_hosting_open_actions(self) -> None:
        actions = hosting_open_actions(matches_nearby=4)
        self.assertEqual(actions[0]["id"], "hosting_open")
        self.assertEqual(actions[0]["message"], "open the meet up")
        self.assertEqual(actions[1]["id"], "hosting_send")

    def test_derive_hosting_ctas_for_host_meet(self) -> None:
        ctx = {
            "signal_saved": {
                "intent": "host_meet",
                "detail_text": "Brazilian coffee",
                "matches_created": 1,
                "hosting": {"status_label": "Ready to open it up"},
            }
        }
        actions = derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED)
        self.assertEqual(actions[0]["label"], "Open the meet up")

    def test_swap_signal_keeps_listen_actions(self) -> None:
        from app.ui_actions import signal_listen_actions

        ctx = {"signal_saved": {"intent": "swap_seek", "detail_text": "rain boots"}}
        actions = derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED)
        self.assertEqual(actions, signal_listen_actions())
