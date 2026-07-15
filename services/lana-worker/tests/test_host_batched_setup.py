"""Batched host-setup flow: temporal-skip gate, CTA matchers, setup defaults, the
review→setup→confirm ui_intent mapping, and bring_items round-tripping through the draft
helpers + model. Pure-function coverage for the new contract (no LLM / DB needed)."""

import unittest

from app.lana_ui import merge_event_drafts, parse_event_draft
from app.lana_unified_pipeline import (
    _apply_host_brain,
    _has_temporal_tokens,
    _host_blockers_needed,
    _host_fallback_nudge,
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

    def test_aside_overrides_setup_card(self) -> None:
        # A conversational aside mid-host shows the text (collect_event_detail), not the
        # setup carousel that would hide Lana's reply — even while host_stage is 'setup'.
        self.assertEqual(
            derive_ui_intent(
                {"event_host_active": True, "host_stage": "setup", "host_aside": True}
            ),
            "collect_event_detail",
        )
        # Without the aside flag, the setup card is shown as normal.
        self.assertEqual(
            derive_ui_intent({"event_host_active": True, "host_stage": "setup"}),
            "event_setup",
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


class TestBlockersNeeded(unittest.TestCase):
    def test_lists_only_missing(self) -> None:
        self.assertEqual(
            _host_blockers_needed("", None, None, False), ["a name", "a date & time", "a place"]
        )
        self.assertEqual(_host_blockers_needed("Book Club", "2026-07-10", "15:00", True), [])
        self.assertEqual(
            _host_blockers_needed("Book Club", None, None, True), ["a date & time"]
        )


class TestFallbackNudge(unittest.TestCase):
    """Deterministic fallback used ONLY when the LLM brain is unavailable — must still move
    the flow forward (never a dead-end) and prompt for exactly what's missing."""

    def test_lists_missing(self) -> None:
        self.assertIn("a name", _host_fallback_nudge(["a name", "a place"]))

    def test_all_done(self) -> None:
        self.assertIn("Looks good", _host_fallback_nudge([]))


class TestApplyHostBrain(unittest.TestCase):
    """The brain OWNS understanding (any phrasing); this applies its extraction to the draft
    monotonically — never clobbering a real value the host already gave."""

    def _apply(self, brain, ed):
        settings: dict = {}
        turn_ctx: dict = {}
        session_ctx: dict = {}
        existing = str(ed.get("title") or "")
        _apply_host_brain(brain, ed, turn_ctx, session_ctx, settings, existing)
        return turn_ctx, settings

    def test_applies_capacity_place_and_prefs(self) -> None:
        ed: dict = {}
        turn_ctx, settings = self._apply(
            {
                "title": "Neighbor Coffee",
                "place": "my house",
                "capacity": 7,
                "auto_approve": False,
                "allow_share": True,
                "reply": "x",
            },
            ed,
        )
        self.assertEqual(ed["title"], "Neighbor Coffee")
        self.assertEqual(ed["venue_name"], "my house")
        self.assertTrue(turn_ctx["event_place_asked"])
        self.assertEqual(ed["max_attendees"], 7)
        self.assertTrue(settings["_cap_set"])
        self.assertIs(ed["auto_approve"], False)
        self.assertIs(ed["allow_attendee_share"], True)

    def test_corrections_replace_title_and_venue(self) -> None:
        # 2026-07-14 #9a: the brain only returns a value the host clearly stated, so a
        # returned title/place is a CORRECTION and must replace the current draft value —
        # the old fill-only-when-empty guard silently dropped "rename it / move it" edits.
        ed = {
            "title": "Book Club", "venue_name": "Foxtail Coffee",
            "venue_address": "123 Oak St", "venue_lat": 28.4, "venue_lng": -81.3,
            "place_id": "abc123",
        }
        self._apply({"title": "Something Else", "place": "the park", "reply": "x"}, ed)
        self.assertEqual(ed["title"], "Something Else")
        self.assertEqual(ed["venue_name"], "the park")
        # Moving the event drops the stale pin so publish re-resolves the new place.
        for stale in ("venue_address", "venue_lat", "venue_lng", "place_id"):
            self.assertIsNone(ed[stale])

    def test_same_values_do_not_reset_pin(self) -> None:
        ed = {"title": "Book Club", "venue_name": "Foxtail Coffee", "place_id": "abc123"}
        self._apply({"title": "Book Club", "place": "Foxtail Coffee", "reply": "x"}, ed)
        self.assertEqual(ed["place_id"], "abc123")

    def test_ignores_generic_title(self) -> None:
        ed: dict = {}
        self._apply({"title": "a meetup", "reply": "x"}, ed)
        self.assertNotIn("title", ed)

    def test_nulls_are_left_alone(self) -> None:
        ed = {"title": "Book Club"}
        self._apply(
            {"title": None, "place": None, "capacity": None, "auto_approve": None,
             "allow_share": None, "reply": "x"},
            ed,
        )
        self.assertEqual(ed, {"title": "Book Club"})


if __name__ == "__main__":
    unittest.main()
