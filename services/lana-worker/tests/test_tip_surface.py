"""C-4-RECO-P3 tip draft card for unified chat."""

from __future__ import annotations

import unittest

from app.local_signals import stamp_signal_saved_ctx
from app.tip_surface import attach_tip_to_signal_saved, build_tip_draft
from app.ui_actions import derive_ui_actions, tip_pass_actions
from app.ui_intent import UI_INTENT_SIGNAL_SAVED
from app.layer1_intents import enrich_slots, utterance_indicates_tip_share


class TestTipSurface(unittest.TestCase):
    def test_doctor_utterance_is_tip_share(self) -> None:
        self.assertTrue(utterance_indicates_tip_share("dr smith is a great doctor"))

    def test_enrich_slots_doctor_becomes_tip_share(self) -> None:
        slots = enrich_slots(
            {"goal": "save_signal", "signal_intent": "host_meet", "confidence": 0.9},
            msg="dr smith is a great doctor",
        )
        self.assertEqual(slots.get("signal_intent"), "tip_share")
        self.assertEqual(slots.get("linear_intent"), "sharing.tip")

    def test_build_tip_draft_parses_doctor(self) -> None:
        draft = build_tip_draft(
            detail_text="Dr. Smith · doctor",
            category="health",
        )
        self.assertIn("Dr. Smith", draft["title"])
        self.assertEqual(draft["status_label"], "Ready to pass it along")

    def test_stamp_tip_share_attaches_tip(self) -> None:
        ctx: dict = {}
        stamp_signal_saved_ctx(
            ctx,
            {
                "signal_id": "s1",
                "intent": "tip_share",
                "detail_text": "Dr. Smith · doctor",
                "category": "health",
                "matches_created": 0,
            },
            active_intent="sharing.tip",
            where_hint="Lake Nona",
        )
        saved = ctx["signal_saved"]
        self.assertEqual(saved["intent"], "tip_share")
        tip = saved.get("tip")
        self.assertIsInstance(tip, dict)
        self.assertEqual(tip.get("where_label"), "Lake Nona")
        self.assertNotIn("hosting", saved)

    def test_tip_pass_actions(self) -> None:
        actions = tip_pass_actions()
        self.assertEqual(actions[0]["message"], "pass the tip along")
        self.assertEqual(actions[1]["label"], "Send to a mom")

    def test_derive_tip_ctas(self) -> None:
        ctx = {
            "signal_saved": {
                "intent": "tip_share",
                "detail_text": "Dr. Smith · doctor",
                "tip": {"status_label": "Ready to pass it along"},
            },
            "active_intent": "sharing.tip",
        }
        actions = derive_ui_actions(ctx, UI_INTENT_SIGNAL_SAVED)
        self.assertEqual(actions[0]["label"], "Pass the tip along")
