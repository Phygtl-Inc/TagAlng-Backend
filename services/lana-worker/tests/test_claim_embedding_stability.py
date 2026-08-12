"""A claim's stored vector must not move when the claim didn't.

Re-mentioning a fact used to replace the stored embedding: `_embed_claim` ran on the
pre-merge extraction and was written unconditionally, and the embed text includes
source_quote. So Tim saying "I play tennis" a second time moved his vector — and with it
every match score computed against him. Pouya saw the same pair read 72% / FIT on Aug 10
and 63% / WEAK on Aug 12 with no profile edit on either side.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service")

from app.claims_persist import upsert_claims  # noqa: E402
from app.models import ExtractedClaim  # noqa: E402

_STORED = {
    "id": "claim-1",
    "confidence": 1.0,
    "synonyms": ["tennis", "racket_sports"],
    "details": [],
    "label": "Plays tennis",
    "source_quote": "you think I should go play tennis?",
    "bucket": "activity",
}


def _claim(**over) -> ExtractedClaim:
    base = dict(
        concept="tennis_player",
        label="Plays tennis",
        tone=None,
        confidence=1.0,
        disclosure="public",
        synonyms=["tennis"],
        details=[],
        source_quote="you think I should go play tennis?",
        bucket="activity",
        transient=False,
    )
    base.update(over)
    return ExtractedClaim(**base)


def _sb(stored: dict):
    sb, table = MagicMock(), MagicMock()
    sb.table.return_value = table
    chain = MagicMock()
    table.select.return_value = chain
    chain.eq.return_value = chain
    chain.is_.return_value = chain
    chain.limit.return_value = chain
    chain.execute.return_value = MagicMock(data=[stored])
    table.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
    return sb, table


def _updated_row(table) -> dict:
    return table.update.call_args[0][0]


class TestClaimEmbeddingStability(unittest.TestCase):
    def test_restating_the_same_fact_leaves_the_vector_alone(self):
        sb, table = _sb(dict(_STORED))
        with patch("app.claims_persist.service_client", return_value=sb), patch(
            "app.claims_persist._embed_claim", return_value=[0.5] * 768
        ) as embed, patch("app.claims_persist.reconcile_heritage_claims"), patch.dict(
            os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "0"}, clear=False
        ):
            upsert_claims("user-1", [_claim()])
        self.assertNotIn(
            "embedding",
            _updated_row(table),
            "a restated claim rewrote its vector — match scores will drift for free",
        )
        embed.assert_not_called()

    def test_new_detail_does_re_embed(self):
        # Enrichment genuinely changes what the claim says, so the vector should follow.
        sb, table = _sb(dict(_STORED))
        with patch("app.claims_persist.service_client", return_value=sb), patch(
            "app.claims_persist._embed_claim", return_value=[0.5] * 768
        ) as embed, patch("app.claims_persist.reconcile_heritage_claims"), patch.dict(
            os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "0"}, clear=False
        ):
            upsert_claims("user-1", [_claim(details=["Plays at PJCC on Tuesdays"])])
        self.assertIn("embedding", _updated_row(table))
        embed.assert_called_once()

    def test_the_vector_describes_the_merged_claim_not_the_bare_extraction(self):
        sb, table = _sb(dict(_STORED))
        with patch("app.claims_persist.service_client", return_value=sb), patch(
            "app.claims_persist._embed_claim", return_value=[0.5] * 768
        ) as embed, patch("app.claims_persist.reconcile_heritage_claims"), patch.dict(
            os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "0"}, clear=False
        ):
            upsert_claims("user-1", [_claim(details=["Competes at state level"])])
        embedded = embed.call_args[0][0]
        # _merge_into_existing unions synonyms; the bare extraction carried only "tennis".
        self.assertIn("racket_sports", embedded.synonyms)


if __name__ == "__main__":
    unittest.main()
