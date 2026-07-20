"""Tests for identity claim linking path in claims_persist.py."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, call, patch

from app.claims_persist import (
    _claim_row,
    _concept_min_sim,
    _concept_top_k,
    _identity_concept_link_enabled,
    resolve_cross_concept_match,
    upsert_claims,
)
from app.models import ExtractedClaim


def _claim(concept, label, bucket="interest", confidence=0.9, tone=None, source_quote=None, transient=False):
    return ExtractedClaim(
        concept=concept,
        label=label,
        bucket=bucket,
        confidence=confidence,
        tone=tone,
        source_quote=source_quote,
        transient=transient,
    )


# ---------------------------------------------------------------------------
# Flag env parsing (new var name IDENTITY_CONCEPT_LINK_ENABLED)
# ---------------------------------------------------------------------------

class TestFlagParsing(unittest.TestCase):
    def test_flag_env_parsing(self):
        true_values = ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON",
                       " 1 ", " true ", "  yes  ", "  on  "]
        false_values = ["", "0", "false", "FALSE", "no", "NO", "off", "OFF",
                        "garbage", "2", "maybe"]

        for val in true_values:
            with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": val}, clear=False):
                self.assertTrue(_identity_concept_link_enabled(), repr(val))

        for val in false_values:
            with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": val}, clear=False):
                self.assertFalse(_identity_concept_link_enabled(), repr(val))

        # Unset
        env = {k: v for k, v in os.environ.items() if k != "IDENTITY_CONCEPT_LINK_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(_identity_concept_link_enabled())


# ---------------------------------------------------------------------------
# _concept_top_k and _concept_min_sim
# ---------------------------------------------------------------------------

class TestConceptTopK(unittest.TestCase):
    def test_concept_top_k_default_and_override(self):
        env = {k: v for k, v in os.environ.items() if k != "LANA_CONCEPT_TOP_K"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_concept_top_k(), 5)

        with patch.dict(os.environ, {"LANA_CONCEPT_TOP_K": "7"}, clear=False):
            self.assertEqual(_concept_top_k(), 7)

        with patch.dict(os.environ, {"LANA_CONCEPT_TOP_K": "garbage"}, clear=False):
            self.assertEqual(_concept_top_k(), 5)

        with patch.dict(os.environ, {"LANA_CONCEPT_TOP_K": "0"}, clear=False):
            self.assertEqual(_concept_top_k(), 1)


class TestConceptMinSim(unittest.TestCase):
    def test_concept_min_sim_default_and_override(self):
        env = {k: v for k, v in os.environ.items() if k != "LANA_CONCEPT_MIN_SIM"}
        with patch.dict(os.environ, env, clear=True):
            self.assertAlmostEqual(_concept_min_sim(), 0.75)

        with patch.dict(os.environ, {"LANA_CONCEPT_MIN_SIM": "0.9"}, clear=False):
            self.assertAlmostEqual(_concept_min_sim(), 0.9)

        with patch.dict(os.environ, {"LANA_CONCEPT_MIN_SIM": "garbage"}, clear=False):
            self.assertAlmostEqual(_concept_min_sim(), 0.75)

        with patch.dict(os.environ, {"LANA_CONCEPT_MIN_SIM": "1.5"}, clear=False):
            self.assertAlmostEqual(_concept_min_sim(), 1.0)

        with patch.dict(os.environ, {"LANA_CONCEPT_MIN_SIM": "-0.1"}, clear=False):
            self.assertAlmostEqual(_concept_min_sim(), 0.0)


# ---------------------------------------------------------------------------
# resolve_cross_concept_match tests (unchanged logic)
# ---------------------------------------------------------------------------

class TestResolveCrossConceptMatch(unittest.TestCase):
    def test_resolve_cross_concept_match_empty_candidates(self):
        incoming = _claim("runner", "Runner", bucket="activity")
        result = resolve_cross_concept_match(incoming=incoming, candidates=[])
        self.assertIsNone(result)

    def test_resolve_cross_concept_match_heritage_shortcut(self):
        """Heritage shortcut: same root -> returns candidate without LLM call."""
        incoming = _claim("brazilian", "Brazilian", bucket="heritage")
        candidate = {
            "id": "cid-1",
            "concept": "brazilian_heritage",
            "label": "Brazilian Heritage",
            "bucket": "heritage",
        }

        with patch("app.orchestrator.llm.llm_json") as mock_llm_json, \
             patch("app.orchestrator.llm.llm_configured", return_value=True):
            result = resolve_cross_concept_match(incoming=incoming, candidates=[candidate])

        self.assertEqual(result, candidate)
        mock_llm_json.assert_not_called()

    def test_resolve_cross_concept_match_llm_says_same(self):
        """Non-heritage bucket: LLM says same -> returns first candidate."""
        incoming = _claim("runner", "Runner", bucket="activity")
        candidate = {"id": "cid-2", "concept": "runs_regularly", "label": "Runs Regularly", "bucket": "activity"}

        with patch("app.orchestrator.llm.llm_json", return_value={"decision": "same"}), \
             patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.router_model", return_value="gemini-flash"):
            result = resolve_cross_concept_match(incoming=incoming, candidates=[candidate])

        self.assertEqual(result, candidate)

    def test_resolve_cross_concept_match_llm_says_different(self):
        """Non-heritage bucket: LLM says different for all -> returns None."""
        incoming = _claim("vegan", "Vegan", bucket="interest")
        candidates = [
            {"id": "cid-3", "concept": "vegetarian", "label": "Vegetarian", "bucket": "interest"},
            {"id": "cid-4", "concept": "plant_based", "label": "Plant Based", "bucket": "interest"},
        ]

        with patch("app.orchestrator.llm.llm_json", return_value={"decision": "different"}), \
             patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.router_model", return_value="gemini-flash"):
            result = resolve_cross_concept_match(incoming=incoming, candidates=candidates)

        self.assertIsNone(result)

    def test_resolve_cross_concept_match_llm_unavailable(self):
        """LLM not configured -> returns None (create-new path)."""
        incoming = _claim("runner", "Runner", bucket="activity")
        candidate = {"id": "cid-5", "concept": "runs_regularly", "label": "Runs Regularly", "bucket": "activity"}

        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            result = resolve_cross_concept_match(incoming=incoming, candidates=[candidate])

        self.assertIsNone(result)

    def test_resolve_cross_concept_match_llm_raises(self):
        """LLM raises exception -> returns None (never blind-merge)."""
        incoming = _claim("runner", "Runner", bucket="activity")
        candidate = {"id": "cid-6", "concept": "runs_regularly", "label": "Runs Regularly", "bucket": "activity"}

        with patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("LLM timeout")), \
             patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.router_model", return_value="gemini-flash"):
            result = resolve_cross_concept_match(incoming=incoming, candidates=[candidate])

        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _claim_row shape test (adapted to new signature)
# ---------------------------------------------------------------------------

class TestClaimRowShape(unittest.TestCase):
    def test_claim_row_legacy_shape_unchanged(self):
        emb = [0.2] * 768
        c = _claim("italian_heritage", "Italian Heritage", bucket="heritage",
                   source_quote="I am Italian")
        row = _claim_row("user-1", c, emb)

        expected_keys = {
            "user_id", "concept", "label", "tone", "confidence",
            "disclosure", "synonyms", "source_quote", "bucket",
            "transient", "embedding",
        }
        self.assertEqual(set(row.keys()), expected_keys)
        self.assertEqual(row["concept"], "italian_heritage")
        self.assertEqual(row["bucket"], "heritage")
        self.assertEqual(row["embedding"], emb)

    def test_claim_row_no_embedding(self):
        c = _claim("runner", "Runner", bucket="activity")
        row = _claim_row("user-1", c, None)
        self.assertNotIn("embedding", row)


# ---------------------------------------------------------------------------
# New link-path tests
# ---------------------------------------------------------------------------

def _make_sb(select_data=None):
    """Build a fully chained MagicMock Supabase client."""
    sb = MagicMock()
    table_mock = MagicMock()
    sb.table.return_value = table_mock

    # Chain for select queries
    select_chain = MagicMock()
    table_mock.select.return_value = select_chain
    select_chain.eq.return_value = select_chain
    select_chain.is_.return_value = select_chain
    select_chain.limit.return_value = select_chain
    select_chain.execute.return_value = MagicMock(data=select_data if select_data is not None else [])

    # Chain for insert
    insert_chain = MagicMock()
    table_mock.insert.return_value = insert_chain
    insert_chain.execute.return_value = MagicMock(data=[{"id": "new-claim-id"}])

    # Chain for update
    update_chain = MagicMock()
    table_mock.update.return_value = update_chain
    update_chain.eq.return_value = update_chain
    update_chain.execute.return_value = MagicMock(data=[])

    # Chain for upsert (claim_concept_links)
    upsert_chain = MagicMock()
    table_mock.upsert.return_value = upsert_chain
    upsert_chain.execute.return_value = MagicMock(data=[])

    return sb, table_mock


class TestUpsertClaimsLinkFlag(unittest.TestCase):
    def test_flag_off_no_link_table_access(self):
        """Flag OFF: user_identity_claims written; _resolve_concept_id not called; no claim_concept_links access."""
        sb, table_mock = _make_sb(select_data=[])

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "0"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id") as mock_resolve, \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c = _claim("runner", "Runner", bucket="activity")
            result = upsert_claims("user-1", [c])

        self.assertEqual(result, 1)
        mock_resolve.assert_not_called()
        # upsert should never have been called on any table
        table_mock.upsert.assert_not_called()

    def test_flag_on_insert_branch_links_claim(self):
        """Flag ON, insert branch: legacy insert happens AND claim_concept_links upsert called
        with claim_id from insert response and on_conflict='claim_id'."""
        sb, table_mock = _make_sb(select_data=[])
        # Insert returns a claim id
        table_mock.insert.return_value.execute.return_value = MagicMock(data=[{"id": "inserted-claim-id"}])

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid-1"), \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c = _claim("runner", "Runner", bucket="activity")
            result = upsert_claims("user-1", [c])

        self.assertEqual(result, 1)
        # Legacy insert happened
        table_mock.insert.assert_called_once()
        # Link upsert happened with correct args and on_conflict
        table_mock.upsert.assert_called_once_with(
            {"claim_id": "inserted-claim-id", "concept_id": "concept-uuid-1"},
            on_conflict="claim_id",
        )

    def test_flag_on_update_branch_links_claim(self):
        """Flag ON, update branch: link upserted with the existing claim id."""
        sb, table_mock = _make_sb(select_data=[{"id": "existing-claim-id"}])

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid-2"), \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c = _claim("runner", "Runner", bucket="activity")
            result = upsert_claims("user-1", [c])

        self.assertEqual(result, 1)
        # Update was called, not insert
        table_mock.update.assert_called()
        table_mock.insert.assert_not_called()
        # Link upsert happened with existing id
        table_mock.upsert.assert_called_once_with(
            {"claim_id": "existing-claim-id", "concept_id": "concept-uuid-2"},
            on_conflict="claim_id",
        )

    def test_flag_on_resolve_none_skips_link_but_counts_save(self):
        """Flag ON, _resolve_concept_id returns None: legacy write still counted, no link write."""
        sb, table_mock = _make_sb(select_data=[])
        table_mock.insert.return_value.execute.return_value = MagicMock(data=[{"id": "claim-xyz"}])

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value=None), \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c = _claim("runner", "Runner", bucket="activity")
            result = upsert_claims("user-1", [c])

        self.assertEqual(result, 1)
        # Legacy write still happened
        table_mock.insert.assert_called_once()
        # No link upsert
        table_mock.upsert.assert_not_called()

    def test_flag_on_link_raises_exception_swallowed(self):
        """Flag ON, link table upsert raises: exception swallowed, upsert_claims returns normal count."""
        sb, table_mock = _make_sb(select_data=[])
        table_mock.insert.return_value.execute.return_value = MagicMock(data=[{"id": "claim-abc"}])
        # Make the upsert chain raise
        table_mock.upsert.return_value.execute.side_effect = RuntimeError("DB error")

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid-3"), \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c = _claim("runner", "Runner", bucket="activity")
            result = upsert_claims("user-1", [c])

        # Exception was swallowed; count is unaffected
        self.assertEqual(result, 1)

    def test_embed_claim_called_once_per_claim_flag_on(self):
        """_embed_claim called exactly once per claim during upsert_claims with flag ON."""
        sb, table_mock = _make_sb(select_data=[])
        table_mock.insert.return_value.execute.return_value = MagicMock(data=[{"id": "claim-1"}])

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768) as mock_embed, \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid-4"), \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c = _claim("runner", "Runner", bucket="activity")
            upsert_claims("user-1", [c])

        self.assertEqual(mock_embed.call_count, 1)

    def test_reselect_exception_swallowed_batch_continues(self):
        """Insert-branch re-select raising must not abort the batch: saved still
        counts, link skipped for that claim, subsequent claims fully processed."""
        sb, table_mock = _make_sb()
        select_chain = table_mock.select.return_value
        insert_chain = table_mock.insert.return_value
        select_chain.execute.side_effect = [
            MagicMock(data=[]),                      # claim 1 existence check -> insert branch
            RuntimeError("transient network error"), # claim 1 fallback re-select raises
            MagicMock(data=[]),                      # claim 2 existence check -> insert branch
        ]
        insert_chain.execute.side_effect = [
            MagicMock(data=[]),                      # claim 1 insert: no data -> fallback fires
            MagicMock(data=[{"id": "claim-2-id"}]),  # claim 2 insert: id present, no fallback
        ]

        with patch.dict(os.environ, {"IDENTITY_CONCEPT_LINK_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._link_claim_to_concept") as mock_link, \
             patch("app.claims_persist.reconcile_heritage_claims"):
            c1 = _claim("runner", "Runner", bucket="activity")
            c2 = _claim("climber", "Climber", bucket="activity")
            result = upsert_claims("user-1", [c1, c2])

        self.assertEqual(result, 2)
        mock_link.assert_called_once()
        self.assertEqual(mock_link.call_args[0][1], "claim-2-id")
        self.assertEqual(table_mock.insert.call_count, 2)


if __name__ == "__main__":
    unittest.main()
