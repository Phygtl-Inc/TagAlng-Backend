import unittest

from app.lana_ui import (
    event_draft_blockers,
    finalize_event_draft,
    merge_event_drafts,
    parse_event_turn_ui,
)


class TestEventDraft(unittest.TestCase):
    def test_blockers_detects_missing_fields(self) -> None:
        draft = {"title": "Brunch", "starts_at": None, "venue_name": "Park"}
        self.assertEqual(event_draft_blockers(draft), ["starts_at"])

    def test_finalize_sets_missing(self) -> None:
        draft = finalize_event_draft({"title": "Coffee", "venue_name": "Commons"})
        self.assertIn("starts_at", draft["missing"])

    def test_merge_preserves_previous_fields(self) -> None:
        prev = {"title": "Brunch", "starts_at": "2026-06-08T10:00:00Z", "venue_name": None}
        new = {"venue_name": "Lake Nona Commons"}
        merged = merge_event_drafts(prev, new)
        self.assertEqual(merged["title"], "Brunch")
        self.assertEqual(merged["venue_name"], "Lake Nona Commons")

    def test_merge_keeps_community_the_meet_is_for(self) -> None:
        # Hosting from a community's "Create event" stamps circle_place_id on the draft;
        # the extractor's redraw must not drop it, or the setup card re-asks "is this for
        # a community?" with None pre-selected.
        prev = {"title": "Lift session", "circle_place_id": "c0ffee00-0000-0000-0000-000000000001"}
        merged = merge_event_drafts(prev, {"venue_name": "The Man Cave Warehouse"})
        self.assertEqual(merged["circle_place_id"], "c0ffee00-0000-0000-0000-000000000001")

    def test_clear_fields_resets_slot(self) -> None:
        # Host rejects the name they gave; clearing it without a replacement blanks it
        # so the flow re-asks "what to call it?" instead of looping on the next slot.
        prev = {"title": "Spooky Movie Gathering", "starts_at": "2026-06-27T18:00:00", "venue_name": None}
        merged = merge_event_drafts(prev, {}, clear_fields=["title"])
        self.assertIsNone(merged["title"])
        self.assertEqual(merged["starts_at"], "2026-06-27T18:00:00")

    def test_clear_fields_yields_to_same_turn_value(self) -> None:
        # A rename that supplies the new name wins over the reset (no blank flash).
        prev = {"title": "Spooky Movie Gathering"}
        merged = merge_event_drafts(prev, {"title": "Game Night"}, clear_fields=["title"])
        self.assertEqual(merged["title"], "Game Night")

    def test_ui_strips_none_highlights(self) -> None:
        ui = parse_event_turn_ui(
            {
                "ui": {
                    "bucket": "activity",
                    "focus_phrase": "None",
                    "highlights": [{"text": "None", "bucket": "activity"}],
                }
            }
        )
        self.assertIsNone(ui["focus_phrase"])
        self.assertEqual(ui["highlights"], [])


if __name__ == "__main__":
    unittest.main()
