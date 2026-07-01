"""Batched host-setup flow: temporal-skip gate, CTA matchers, setup defaults, the
review→setup→confirm ui_intent mapping, and bring_items round-tripping through the draft
helpers + model. Pure-function coverage for the new contract (no LLM / DB needed)."""

import unittest

from app.lana_ui import merge_event_drafts, parse_event_draft
from app.lana_unified_pipeline import (
    _has_temporal_tokens,
    _is_host_confirm,
    _is_host_drop,
    _is_host_tweak,
    _seed_setup_defaults,
)
from app.models import EventDraft
from app.ui_intent import derive_ui_intent


class TestTemporalSkipGate(unittest.TestCase):
    def test_real_dates_trigger_resolver(self) -> None:
        for msg in ("Saturday morning", "next friday at 9pm", "the 28th", "tomorrow", "10am"):
            self.assertTrue(_has_temporal_tokens(msg), msg)

    def test_chip_taps_skip_resolver(self) -> None:
        # The common setup/CTA chip taps carry no date — skip the LLM when-resolver.
        for msg in ("Anyone can join", "Looks good", "My place", "Brazilian moms only"):
            self.assertFalse(_has_temporal_tokens(msg), msg)


class TestHostCtaMatchers(unittest.TestCase):
    def test_confirm(self) -> None:
        self.assertTrue(_is_host_confirm("Looks good · next"))
        self.assertTrue(_is_host_confirm("looks good"))
        self.assertFalse(_is_host_confirm("Let me tweak"))

    def test_drop(self) -> None:
        self.assertTrue(_is_host_drop("Drop the meet up"))
        self.assertTrue(_is_host_drop("drop it on the block"))
        self.assertFalse(_is_host_drop("Looks good"))

    def test_tweak(self) -> None:
        self.assertTrue(_is_host_tweak("Let me tweak"))
        self.assertFalse(_is_host_tweak("Looks good"))


class TestSetupDefaults(unittest.TestCase):
    def test_seeds_only_unset_fields(self) -> None:
        ed: dict = {}
        _seed_setup_defaults(ed)
        self.assertEqual(ed["max_attendees"], 8)
        self.assertIs(ed["auto_approve"], False)  # require-approval ON by default
        self.assertIs(ed["allow_attendee_share"], True)
        self.assertEqual(ed["bring_items"], [])

    def test_does_not_clobber_existing(self) -> None:
        ed = {"max_attendees": 20, "auto_approve": True, "bring_items": ["Stroller"]}
        _seed_setup_defaults(ed)
        self.assertEqual(ed["max_attendees"], 20)
        self.assertIs(ed["auto_approve"], True)
        self.assertEqual(ed["bring_items"], ["Stroller"])


class TestHostStageUiIntent(unittest.TestCase):
    def test_stage_mapping(self) -> None:
        self.assertEqual(
            derive_ui_intent({"event_host_active": True, "host_stage": "review"}),
            "event_review",
        )
        self.assertEqual(
            derive_ui_intent({"event_host_active": True, "host_stage": "setup"}),
            "event_setup",
        )
        self.assertEqual(
            derive_ui_intent({"event_host_active": True, "host_stage": "confirm"}),
            "event_confirm",
        )

    def test_published_and_collect(self) -> None:
        self.assertEqual(derive_ui_intent({"event_published_now": True}), "event_created")
        self.assertEqual(
            derive_ui_intent({"event_host_active": True}), "collect_event_detail"
        )


class TestBringItems(unittest.TestCase):
    def test_model_carries_bring_items(self) -> None:
        d = EventDraft(title="x", bring_items=["Stroller", "Coffee mug"])
        self.assertEqual(d.bring_items, ["Stroller", "Coffee mug"])

    def test_parse_dedupes_and_caps(self) -> None:
        parsed = parse_event_draft(
            {"bring_items": ["Stroller", "Stroller", " ", "Mug"] + [f"x{i}" for i in range(20)]}
        )
        self.assertEqual(parsed["bring_items"][:3], ["Stroller", "Mug", "x0"])
        self.assertLessEqual(len(parsed["bring_items"]), 12)

    def test_merge_takes_new_bring_items(self) -> None:
        merged = merge_event_drafts({"bring_items": ["Old"]}, {"bring_items": ["New", "Items"]})
        self.assertEqual(merged["bring_items"], ["New", "Items"])


if __name__ == "__main__":
    unittest.main()
