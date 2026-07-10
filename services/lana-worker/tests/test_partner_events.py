"""Partner-sourced events — attribution rendering + import idempotency (pure level)."""

from __future__ import annotations

import unittest

from app.activity_browse import _format_browse_message
from app.discovery_route import activity_previews_from_events, format_activities_message
from app.partner_events import (
    attribution_label,
    merge_partner_events,
    normalize_partner_items,
    with_attribution,
)

_PARTNER_EVENT = {
    "id": "e1",
    "title": "Toddler Storytime",
    "starts_at": "2026-07-15T14:00:00+00:00",
    "venue_name": "Lake Nona Library",
    "source": "partner",
    "source_name": "Lake Nona Library",
}
_MEMBER_EVENT = {
    "id": "e2",
    "title": "Friday park playdate",
    "starts_at": "2026-07-17T13:00:00+00:00",
    "venue_name": "Laureate Park playground",
    "source": "member",
    "source_name": None,
}


class TestAttribution(unittest.TestCase):
    def test_attribution_label(self) -> None:
        self.assertEqual(attribution_label(_PARTNER_EVENT), "via Lake Nona Library")
        self.assertIsNone(attribution_label(_MEMBER_EVENT))
        self.assertIsNone(attribution_label({"source": "partner", "source_name": ""}))
        self.assertIsNone(attribution_label(None))

    def test_with_attribution_appends_only_for_partner(self) -> None:
        self.assertEqual(
            with_attribution("• Toddler Storytime", _PARTNER_EVENT),
            "• Toddler Storytime · via Lake Nona Library",
        )
        self.assertEqual(with_attribution("• Playdate", _MEMBER_EVENT), "• Playdate")

    def test_activity_previews_carry_attribution(self) -> None:
        previews = activity_previews_from_events([_PARTNER_EVENT, _MEMBER_EVENT])
        self.assertEqual(previews[0]["attribution"], "via Lake Nona Library")
        self.assertEqual(previews[0]["source"], "partner")
        self.assertIsNone(previews[1]["attribution"])

    def test_browse_message_renders_attribution(self) -> None:
        msg = _format_browse_message(
            [_PARTNER_EVENT, _MEMBER_EVENT], None, phone_verified=True
        )
        self.assertIn("Toddler Storytime at Lake Nona Library", msg)
        self.assertIn("· via Lake Nona Library", msg)
        # Member events stay untouched.
        playdate_line = next(l for l in msg.splitlines() if "playdate" in l.lower())
        self.assertNotIn("via", playdate_line)

    def test_discovery_activities_message_renders_attribution(self) -> None:
        msg = format_activities_message([_PARTNER_EVENT], "Lake Nona", phone_verified=True)
        self.assertIn("· via Lake Nona Library", msg)


class TestImportIdempotency(unittest.TestCase):
    def _items(self) -> list[dict]:
        return [
            {
                "title": "Toddler Storytime",
                "starts_at": "2026-07-15T10:00:00",
                "source_name": "Lake Nona Library",
                "cohort_tags": "parents|family",
            },
            {
                "title": "Family Swim",
                "starts_at": "2026-07-16T09:00:00",
                "source_name": "Lake Nona YMCA",
                "venue_name": "YMCA Aquatic Center",
            },
        ]

    def test_normalize_stamps_partner_source_and_defaults(self) -> None:
        rows, problems = normalize_partner_items(self._items(), default_block_id="blk1")
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["source"] == "partner" for r in rows))
        self.assertEqual(rows[0]["cohort_tags"], ["parents", "family"])
        self.assertEqual(rows[0]["venue_name"], "Lake Nona Library")  # defaults to source
        self.assertEqual(rows[1]["venue_name"], "YMCA Aquatic Center")
        self.assertEqual(rows[0]["block_id"], "blk1")
        # Naive wall-clock got anchored to the event tz and stored as UTC.
        self.assertTrue(rows[0]["starts_at"].endswith("+00:00"))

    def test_normalize_reports_bad_rows_and_dedupes(self) -> None:
        items = self._items()
        items.append(dict(items[0]))  # duplicate line in the file
        items.append({"title": "No when", "source_name": "X"})  # missing starts_at
        rows, problems = normalize_partner_items(items)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(problems), 1)

    def test_merge_is_idempotent(self) -> None:
        rows, _ = normalize_partner_items(self._items())
        inserts, updates = merge_partner_events([], rows)
        self.assertEqual(len(inserts), 2)
        self.assertEqual(updates, [])
        # Re-import of the same file against what the first run wrote → no-op.
        existing = [{**r, "id": f"id{i}"} for i, r in enumerate(inserts)]
        inserts2, updates2 = merge_partner_events(existing, rows)
        self.assertEqual(inserts2, [])
        self.assertEqual(updates2, [])

    def test_merge_patches_changed_fields_only(self) -> None:
        rows, _ = normalize_partner_items(self._items())
        existing = [{**r, "id": f"id{i}"} for i, r in enumerate(rows)]
        changed = [dict(r) for r in rows]
        changed[0]["description"] = "Now with songs and bubbles."
        inserts, updates = merge_partner_events(existing, changed)
        self.assertEqual(inserts, [])
        self.assertEqual(
            updates, [{"description": "Now with songs and bubbles.", "id": "id0"}]
        )

    def test_new_occurrence_is_an_insert_not_an_update(self) -> None:
        rows, _ = normalize_partner_items(self._items())
        existing = [{**rows[0], "id": "id0"}]
        next_week = dict(self._items()[0])
        next_week["starts_at"] = "2026-07-22T10:00:00"
        new_rows, _ = normalize_partner_items([next_week])
        inserts, updates = merge_partner_events(existing, new_rows)
        self.assertEqual(len(inserts), 1)
        self.assertEqual(updates, [])


if __name__ == "__main__":
    unittest.main()
