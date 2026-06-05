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
