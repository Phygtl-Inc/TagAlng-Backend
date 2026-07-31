"""Capability-catalog self-heal + shared embedding text.

Regression cover for the prod outage where all 8 capability_index rows had
embedding=NULL, match_latent_capabilities' `embedding is not null` guard dropped every
row, and the empty result set was indistinguishable from a genuine no-match — no error,
no log, no metric, suggestion_queue at 0 for a month.
"""

import unittest
from unittest.mock import MagicMock, patch

from app import latent_extract
from app.capability_embed import capability_embedding_text
from app.latent_extract import ExtractedEntity, _queue_capability_matches


class TestCapabilityEmbeddingText(unittest.TestCase):
    def test_name_description_triggers(self) -> None:
        self.assertEqual(
            capability_embedding_text(
                capability_name="Find local activities",
                description="Find events, classes, or activities happening nearby",
                entity_triggers=["activity", "event"],
            ),
            "Find local activities — Find events, classes, or activities happening nearby "
            "— activity, event",
        )

    def test_missing_parts_are_dropped_not_padded(self) -> None:
        self.assertEqual(
            capability_embedding_text(
                capability_name="Host a meet", description=None, entity_triggers=[]
            ),
            "Host a meet",
        )
        self.assertEqual(
            capability_embedding_text(
                capability_name=None, description=None, entity_triggers=None
            ),
            "",
        )

    def test_blank_triggers_filtered(self) -> None:
        self.assertEqual(
            capability_embedding_text(
                capability_name="X", description="Y", entity_triggers=["a", "  ", ""]
            ),
            "X — Y — a",
        )

    def test_matches_the_backfill_script(self) -> None:
        """Script and worker must build identical text or the vector space splits."""
        from scripts.backfill_capability_embeddings import _embedding_text

        row = {
            "capability_name": "Find a gear/clothes swap",
            "description": "Find someone nearby willing to swap kids gear",
            "entity_triggers": ["gear", "stroller"],
        }
        self.assertEqual(
            _embedding_text(row),
            capability_embedding_text(
                capability_name=row["capability_name"],
                description=row["description"],
                entity_triggers=row["entity_triggers"],
            ),
        )


class TestCatalogSelfHeal(unittest.TestCase):
    def setUp(self) -> None:
        # Cooldown is process-global; reset so tests are order-independent.
        latent_extract._catalog_selfheal_at = 0.0

    def _sb_with_unembedded(self, rows: list[dict]) -> MagicMock:
        sb = MagicMock()
        chain = sb.table.return_value.select.return_value.is_.return_value.eq.return_value
        chain.limit.return_value.execute.return_value.data = rows
        sb.rpc.return_value.execute.return_value.data = []
        return sb

    def test_empty_match_with_unembedded_catalog_heals_and_logs(self) -> None:
        sb = self._sb_with_unembedded(
            [{"capability_id": "looking.tip", "capability_name": "Find a tip",
              "description": "d", "entity_triggers": ["dentist"]}]
        )
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch.object(latent_extract, "_embed_capability_row", return_value=[0.1] * 768), \
             patch("threading.Thread") as thread:
            n = _queue_capability_matches(
                user_id="u1",
                entity=ExtractedEntity(text="dentist", type="service"),
                embedding=[0.2] * 768,
                utterance_excerpt="need a dentist",
            )
        self.assertEqual(n, 0)
        thread.assert_called_once()
        # Run the thread body inline and assert it wrote the vector back.
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch.object(latent_extract, "_embed_capability_row", return_value=[0.1] * 768), \
             self.assertLogs("app.latent_extract", level="ERROR") as logs:
            thread.call_args.kwargs["target"]()
        sb.table.return_value.update.assert_called_once()
        self.assertIn("capability_index_unembedded", "".join(logs.output))

    def test_healthy_catalog_heals_nothing_and_stays_quiet(self) -> None:
        sb = self._sb_with_unembedded([])
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch("threading.Thread") as thread:
            _queue_capability_matches(
                user_id="u1",
                entity=ExtractedEntity(text="dentist", type="service"),
                embedding=[0.2] * 768,
                utterance_excerpt="need a dentist",
            )
            thread.call_args.kwargs["target"]()
        sb.table.return_value.update.assert_not_called()

    def test_cooldown_prevents_per_turn_probing(self) -> None:
        """The probe must not run on every turn — that was the review concern."""
        sb = self._sb_with_unembedded([])
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch("threading.Thread") as thread:
            for _ in range(25):
                _queue_capability_matches(
                    user_id="u1",
                    entity=ExtractedEntity(text="dentist", type="service"),
                    embedding=[0.2] * 768,
                    utterance_excerpt="need a dentist",
                )
        self.assertEqual(thread.call_count, 1)

    def test_selfheal_failure_never_raises_into_the_turn(self) -> None:
        sb = MagicMock()
        sb.rpc.return_value.execute.return_value.data = []
        sb.table.side_effect = RuntimeError("supabase down")
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch("threading.Thread") as thread:
            n = _queue_capability_matches(
                user_id="u1",
                entity=ExtractedEntity(text="dentist", type="service"),
                embedding=[0.2] * 768,
                utterance_excerpt="need a dentist",
            )
            thread.call_args.kwargs["target"]()  # must swallow
        self.assertEqual(n, 0)

    def test_row_that_fails_to_embed_is_skipped_not_written(self) -> None:
        sb = self._sb_with_unembedded(
            [{"capability_id": "looking.tip", "capability_name": "Find a tip",
              "description": "d", "entity_triggers": []}]
        )
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch.object(latent_extract, "_embed_capability_row", return_value=None), \
             patch("threading.Thread") as thread:
            _queue_capability_matches(
                user_id="u1",
                entity=ExtractedEntity(text="dentist", type="service"),
                embedding=[0.2] * 768,
                utterance_excerpt="need a dentist",
            )
            thread.call_args.kwargs["target"]()
        sb.table.return_value.update.assert_not_called()

    def test_nonempty_match_never_probes(self) -> None:
        sb = MagicMock()
        sb.rpc.return_value.execute.return_value.data = [
            {"capability_id": "looking.tip", "similarity": 0.61}
        ]
        with patch.object(latent_extract, "service_client", return_value=sb), \
             patch("threading.Thread") as thread:
            n = _queue_capability_matches(
                user_id="u1",
                entity=ExtractedEntity(text="dentist", type="service"),
                embedding=[0.2] * 768,
                utterance_excerpt="need a dentist",
            )
        self.assertEqual(n, 1)
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
