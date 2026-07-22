"""Self-heal for claims saved without embeddings (write-time embed is best-effort)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

# Break the pre-existing claims_persist → vertex_extract → orchestrator import
# cycle when this file runs standalone (the full suite primes it via other tests).
import app.orchestrator.pipeline  # noqa: F401

import app.claims_persist as cp


def _null_embedding_rows(rows: list[dict]) -> MagicMock:
    sb = MagicMock()
    sel = sb.table.return_value.select.return_value.in_.return_value
    sel = sel.is_.return_value.is_.return_value.limit.return_value
    sel.execute.return_value = MagicMock(data=rows)
    return sb


class TestBackfillClaimEmbeddings(unittest.TestCase):
    def test_noop_without_user_ids(self) -> None:
        self.assertEqual(cp.backfill_claim_embeddings(user_ids=[]), 0)

    @patch("app.claims_persist.vertex_embed", return_value=[0.1, 0.2])
    @patch("app.claims_persist.service_client")
    def test_embeds_and_updates_null_rows(self, mock_client, mock_embed) -> None:
        sb = _null_embedding_rows(
            [
                {"id": "c1", "concept": "long_married", "label": "Married 10 years",
                 "source_quote": None, "bucket": "stage"},
                {"id": "c2", "concept": "italian_heritage", "label": "Italian Heritage",
                 "source_quote": "i'm italian", "bucket": "heritage"},
            ]
        )
        mock_client.return_value = sb
        self.assertEqual(cp.backfill_claim_embeddings(user_ids=["u1"]), 2)
        self.assertEqual(mock_embed.call_count, 2)
        sb.table.return_value.update.assert_called_with({"embedding": [0.1, 0.2]})

    @patch("app.claims_persist.vertex_embed", side_effect=RuntimeError("vertex down"))
    @patch("app.claims_persist.service_client")
    def test_embed_failure_skips_row(self, mock_client, _mock_embed) -> None:
        mock_client.return_value = _null_embedding_rows(
            [{"id": "c1", "concept": "x", "label": "X", "source_quote": None, "bucket": None}]
        )
        self.assertEqual(cp.backfill_claim_embeddings(user_ids=["u1"]), 0)

    @patch("app.claims_persist.backfill_claim_embeddings", return_value=1)
    def test_kick_respects_cooldown(self, mock_backfill) -> None:
        cp._BACKFILL_COOLDOWN.clear()
        with patch("app.claims_persist.service_client") as mock_client:
            users = mock_client.return_value.table.return_value.select.return_value
            users.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
            cp.kick_claim_embedding_backfill(user_id="u1", block_id="b1")
            cp.kick_claim_embedding_backfill(user_id="u1", block_id="b1")
        import time

        deadline = time.time() + 2
        while mock_backfill.call_count < 1 and time.time() < deadline:
            time.sleep(0.02)
        # Second kick inside the cooldown window must not spawn another run.
        self.assertEqual(mock_backfill.call_count, 1)


if __name__ == "__main__":
    unittest.main()
