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

    def test_none_answers_never_become_chips(self) -> None:
        # "Anything to bring?" → "nothing" is an empty list, not a literal chip — at the
        # parse layer (LLM draft), and via is_none_bring_item at the FE-POST/publish layers.
        from app.lana_ui import is_none_bring_item

        parsed = parse_event_draft(
            {"bring_items": ["nothing", "None", "no need!", "N/A", "Stroller"]}
        )
        self.assertEqual(parsed["bring_items"], ["Stroller"])
        for none_word in ("nothing", "Nothing.", "NONE", "no need", "nada", "n/a"):
            self.assertTrue(is_none_bring_item(none_word), none_word)
        # Real items survive — including ones that merely contain a none-word.
        for item in ("Stroller", "Nothing-brand cooler", "Snacks"):
            self.assertFalse(is_none_bring_item(item), item)

    def test_publish_fields_drop_none_answers(self) -> None:
        from unittest.mock import patch

        from app.event_publish import build_create_event_fields

        draft = EventDraft(
            title="Birthday Party with Kids",
            bring_items=["nothing", "Balloons"],
        )
        with patch(
            "app.event_publish.resolve_event_location",
            return_value=(None, None, "blk-1"),
        ), patch("app.event_publish._valid_purpose_ids", return_value=set()):
            fields = build_create_event_fields("u-1", draft)
        self.assertEqual(fields.get("bring_items"), ["Balloons"])


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
    last-write-wins — a correction ("don't call it that — call it X") replaces the old value.
    The brain's contract (null for anything the latest message didn't state) is what keeps an
    unrelated turn from clobbering a value the host already gave."""

    def _apply(self, brain, ed):
        settings: dict = {}
        turn_ctx: dict = {}
        session_ctx: dict = {}
        _apply_host_brain(brain, ed, turn_ctx, session_ctx, settings)
        return turn_ctx, session_ctx, settings

    def test_applies_capacity_place_and_prefs(self) -> None:
        ed: dict = {}
        turn_ctx, _, settings = self._apply(
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

    def test_corrections_overwrite_title_and_place(self) -> None:
        # "don't call it that — call it Pasta Night, and actually let's do the community
        # center" must not be silently dropped just because the fields were already filled.
        ed = {"title": "Pizza Night", "venue_name": "Riverside Park"}
        self._apply({"title": "Pasta Night", "place": "Community Center", "reply": "x"}, ed)
        self.assertEqual(ed["title"], "Pasta Night")
        self.assertEqual(ed["venue_name"], "Community Center")

    def test_place_change_drops_stale_pin(self) -> None:
        # Moving the meet invalidates the old venue's pin AND the event_venue stash —
        # otherwise next turn's re-stamp resurrects the old spot / publish pins the
        # wrong coordinates.
        ed = {
            "venue_name": "Foxtail Coffee",
            "place_id": "pid-1",
            "venue_lat": 1.0,
            "venue_lng": 2.0,
            "venue_address": "1 Old St",
        }
        turn_ctx, session_ctx, _ = self._apply({"place": "the park", "reply": "x"}, ed)
        self.assertEqual(ed["venue_name"], "the park")
        for k in ("place_id", "venue_lat", "venue_lng", "venue_address"):
            self.assertNotIn(k, ed)
        self.assertIsNone(turn_ctx["event_venue"])
        self.assertIsNone(session_ctx["event_venue"])

    def test_same_place_echo_keeps_pin(self) -> None:
        # The brain re-stating the current place (an echo, not a change) must not wipe
        # the precise pin a Search pick / auto-resolve already stamped.
        ed = {"venue_name": "Foxtail Coffee", "place_id": "pid-1", "venue_lat": 1.0}
        self._apply({"place": "foxtail coffee", "reply": "x"}, ed)
        self.assertEqual(ed["venue_name"], "Foxtail Coffee")
        self.assertEqual(ed["place_id"], "pid-1")
        self.assertEqual(ed["venue_lat"], 1.0)

    def test_ignores_generic_title(self) -> None:
        ed: dict = {}
        self._apply({"title": "a meetup", "reply": "x"}, ed)
        self.assertNotIn("title", ed)

    def test_generic_title_never_overwrites_real_name(self) -> None:
        ed = {"title": "Book Club"}
        self._apply({"title": "meetup", "reply": "x"}, ed)
        self.assertEqual(ed["title"], "Book Club")

    def test_redo_place_reopens_where_step(self) -> None:
        # "I want a different spot" (no new value) — clear the venue + pin + step flags so
        # the flow re-collects the place instead of holding a card with a hole in it.
        ed = {
            "title": "Big Bros Meet",
            "venue_name": "KFC",
            "place_id": "pid-1",
            "venue_lat": 1.0,
            "venue_lng": 2.0,
            "venue_address": "1 Old St",
        }
        turn_ctx, session_ctx, _ = self._apply({"redo": ["place"], "reply": "x"}, ed)
        for k in ("venue_name", "place_id", "venue_lat", "venue_lng", "venue_address"):
            self.assertNotIn(k, ed)
        self.assertIs(turn_ctx["event_place_asked"], False)
        self.assertIsNone(turn_ctx["event_venue"])
        self.assertIsNone(turn_ctx["event_venue_tried"])
        self.assertIsNone(session_ctx["event_venue"])
        self.assertEqual(ed["title"], "Big Bros Meet")  # untouched slots survive

    def test_redo_when_clears_dates(self) -> None:
        ed = {"starts_at": "2026-07-25T21:00:00", "ends_at": "2026-07-25T22:30:00"}
        turn_ctx, _, _ = self._apply({"redo": ["when"], "reply": "x"}, ed)
        self.assertNotIn("starts_at", ed)
        self.assertNotIn("ends_at", ed)
        self.assertIsNone(turn_ctx["event_when_date"])
        self.assertIsNone(turn_ctx["event_when_time"])

    def test_redo_title_clears_name(self) -> None:
        ed = {"title": "Big Bros Meet"}
        self._apply({"redo": ["title"], "reply": "x"}, ed)
        self.assertNotIn("title", ed)

    def test_redo_with_inline_value_keeps_value(self) -> None:
        # Belt-and-braces: if the brain emits both redo and the new value, the value wins —
        # redo clears first, the extraction lands after.
        ed = {"title": "Pizza Night"}
        self._apply({"redo": ["title"], "title": "Pasta Night", "reply": "x"}, ed)
        self.assertEqual(ed["title"], "Pasta Night")

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
