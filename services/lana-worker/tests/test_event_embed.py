import unittest

from app.event_embed import event_embedding_text
from scripts.backfill_event_embeddings import _is_stale


class TestEventEmbeddingText(unittest.TestCase):
    def test_title_leads_and_tags_precede_prose(self) -> None:
        text = event_embedding_text(
            title="Saturday Morning Run",
            description="Easy 5k loop, all paces welcome. Coffee after.",
            venue_name="Lake Nona Trail",
            cohort_tags=["sports", "runner"],
        )
        self.assertTrue(text.startswith("Saturday Morning Run"))
        self.assertLess(text.index("sports"), text.index("Easy 5k"))

    def test_venue_repeated_in_title_is_not_embedded_twice(self) -> None:
        text = event_embedding_text(
            title="Yoga at Lake Nona Park",
            venue_name="Lake Nona Park",
        )
        self.assertEqual(text.lower().count("lake nona park"), 1)

    def test_longer_phrasing_replaces_the_shorter_one(self) -> None:
        # The shorter part arrives first and must be upgraded, not skipped — otherwise the
        # vector keeps "Book Club" and loses "Tuesday Book Club at the library".
        text = event_embedding_text(
            title="Book Club",
            venue_name="Book Club Room, Main Library",
        )
        self.assertIn("Main Library", text)
        self.assertEqual(text.lower().count("book club room"), 1)

    def test_long_description_cannot_bury_the_title(self) -> None:
        text = event_embedding_text(title="Chess Night", description="x " * 600)
        self.assertTrue(text.startswith("Chess Night"))
        self.assertLessEqual(len(text), 2000)

    def test_empty_meet_yields_empty_text(self) -> None:
        # The backfill skips on this rather than spending a Vertex call on "".
        self.assertEqual(event_embedding_text(title=None, description="   "), "")


class TestStaleDetection(unittest.TestCase):
    def test_edited_after_embedding_is_stale(self) -> None:
        self.assertTrue(
            _is_stale(
                {
                    "updated_at": "2026-09-13T10:00:00+00:00",
                    "embedding_updated_at": "2026-09-13T09:00:00+00:00",
                }
            )
        )

    def test_untouched_since_embedding_is_not_stale(self) -> None:
        self.assertFalse(
            _is_stale(
                {
                    "updated_at": "2026-09-13T09:00:00+00:00",
                    "embedding_updated_at": "2026-09-13T10:00:00+00:00",
                }
            )
        )

    def test_never_embedded_is_left_to_the_null_pass(self) -> None:
        self.assertFalse(
            _is_stale({"updated_at": "2026-09-13T09:00:00+00:00", "embedding_updated_at": None})
        )


if __name__ == "__main__":
    unittest.main()
