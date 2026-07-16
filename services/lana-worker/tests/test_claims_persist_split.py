"""Tests for Phase 1 identity split write path in claims_persist.py."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from app.claims_persist import (
    _claim_row_legacy,
    _claim_row_split,
    _concept_min_sim,
    _concept_top_k,
    _identity_split_write_enabled,
    _reconcile_heritage_claims_split,
    _resolve_concept_id,
    _upsert_claims_legacy,
    _upsert_claims_split,
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
# 3. test_flag_env_parsing
# ---------------------------------------------------------------------------

class TestFlagParsing(unittest.TestCase):
    def test_flag_env_parsing(self):
        true_values = ["1", "true", "True", "TRUE", "yes", "YES", "on", "ON",
                       " 1 ", " true ", "  yes  ", "  on  "]
        false_values = ["", "0", "false", "FALSE", "no", "NO", "off", "OFF",
                        "garbage", "2", "maybe"]

        for val in true_values:
            with patch.dict(os.environ, {"IDENTITY_SPLIT_WRITE_ENABLED": val}, clear=False):
                self.assertTrue(_identity_split_write_enabled(), repr(val))

        for val in false_values:
            with patch.dict(os.environ, {"IDENTITY_SPLIT_WRITE_ENABLED": val}, clear=False):
                self.assertFalse(_identity_split_write_enabled(), repr(val))

        # Unset
        env = {k: v for k, v in os.environ.items() if k != "IDENTITY_SPLIT_WRITE_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(_identity_split_write_enabled())


# ---------------------------------------------------------------------------
# 4. test_concept_top_k_default_and_override
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


# ---------------------------------------------------------------------------
# 5. test_concept_min_sim_default_and_override
# ---------------------------------------------------------------------------

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
# 6-10. resolve_cross_concept_match tests
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

        # Patch llm_json at the orchestrator level; it must not be called.
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
# 11-12. claim row shape tests
# ---------------------------------------------------------------------------

class TestClaimRowShapes(unittest.TestCase):
    def test_claim_row_split_shape(self):
        c = _claim("runner", "Runner", bucket="activity", tone="positive", source_quote="I run daily")
        emb = [0.1] * 768
        row = _claim_row_split("user-1", c, "concept-uuid-1", emb)

        expected_keys = {
            "user_id", "concept_id", "label", "tone", "confidence",
            "disclosure", "source_quote", "transient", "utterance_embedding",
        }
        self.assertEqual(set(row.keys()), expected_keys)
        self.assertEqual(row["user_id"], "user-1")
        self.assertEqual(row["concept_id"], "concept-uuid-1")
        self.assertEqual(row["utterance_embedding"], emb)
        # Must NOT contain these legacy keys
        self.assertNotIn("concept", row)
        self.assertNotIn("bucket", row)
        self.assertNotIn("synonyms", row)
        self.assertNotIn("embedding", row)

    def test_claim_row_split_shape_no_embedding(self):
        c = _claim("runner", "Runner", bucket="activity")
        row = _claim_row_split("user-1", c, "concept-uuid-1", None)
        self.assertNotIn("utterance_embedding", row)

    def test_claim_row_legacy_shape_unchanged(self):
        with patch("app.claims_persist._embed_claim", return_value=[0.2] * 768):
            c = _claim("italian_heritage", "Italian Heritage", bucket="heritage",
                       source_quote="I am Italian")
            row = _claim_row_legacy("user-1", c)

        expected_keys = {
            "user_id", "concept", "label", "tone", "confidence",
            "disclosure", "synonyms", "source_quote", "bucket",
            "transient", "embedding",
        }
        self.assertEqual(set(row.keys()), expected_keys)
        self.assertEqual(row["concept"], "italian_heritage")
        self.assertEqual(row["bucket"], "heritage")


# ---------------------------------------------------------------------------
# 13-16. upsert_claims split path tests
# ---------------------------------------------------------------------------

class TestUpsertClaimsSplitPath(unittest.TestCase):
    def _make_sb(self, select_returns_empty=True):
        sb = MagicMock()
        table = MagicMock()
        sb.table.return_value = table
        select_chain = MagicMock()
        table.select.return_value = select_chain
        select_chain.eq.return_value = select_chain
        select_chain.is_.return_value = select_chain
        select_chain.limit.return_value = select_chain
        if select_returns_empty:
            select_chain.execute.return_value = MagicMock(data=[])
        else:
            select_chain.execute.return_value = MagicMock(data=[{"id": "existing-claim-id"}])
        return sb, table, select_chain

    def test_upsert_claims_split_skips_when_concept_id_none(self):
        """When _resolve_concept_id returns None, no write happens."""
        sb, table, _ = self._make_sb()

        with patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value=None), \
             patch("app.claims_persist._reconcile_heritage_claims_split"):
            c = _claim("runner", "Runner", bucket="activity")
            result = _upsert_claims_split("user-1", [c])

        self.assertEqual(result, 0)
        table.insert.assert_not_called()
        table.update.assert_not_called()

    def test_upsert_claims_split_confidence_gate(self):
        """A claim with low confidence is skipped before RPC."""
        sb, table, _ = self._make_sb()

        with patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=None), \
             patch("app.claims_persist._resolve_concept_id") as mock_resolve, \
             patch("app.claims_persist._reconcile_heritage_claims_split"):
            c = _claim("runner", "Runner", bucket="activity", confidence=0.5)
            result = _upsert_claims_split("user-1", [c])

        self.assertEqual(result, 0)
        mock_resolve.assert_not_called()

    def test_upsert_claims_split_insert_vs_update(self):
        """Empty select -> INSERT; non-empty select -> UPDATE."""
        # INSERT path
        sb_ins, table_ins, _ = self._make_sb(select_returns_empty=True)
        with patch("app.claims_persist.service_client", return_value=sb_ins), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid"), \
             patch("app.claims_persist._reconcile_heritage_claims_split"):
            _upsert_claims_split("user-1", [_claim("runner", "Runner", bucket="activity")])
        table_ins.insert.assert_called_once()
        table_ins.update.assert_not_called()

        # UPDATE path
        sb_upd, table_upd, _ = self._make_sb(select_returns_empty=False)
        with patch("app.claims_persist.service_client", return_value=sb_upd), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid"), \
             patch("app.claims_persist._reconcile_heritage_claims_split"):
            _upsert_claims_split("user-1", [_claim("runner", "Runner", bucket="activity")])
        table_upd.update.assert_called()
        table_upd.insert.assert_not_called()

    def test_upsert_claims_split_calls_heritage_reconcile(self):
        """Heritage claims in batch cause _reconcile_heritage_claims_split to be called."""
        sb, table, _ = self._make_sb()
        heritage_claim = _claim("brazilian_heritage", "Brazilian Heritage", bucket="heritage")
        other_claim = _claim("runner", "Runner", bucket="activity")

        with patch("app.claims_persist.service_client", return_value=sb), \
             patch("app.claims_persist._embed_claim", return_value=[0.1] * 768), \
             patch("app.claims_persist._resolve_concept_id", return_value="concept-uuid"), \
             patch("app.claims_persist._reconcile_heritage_claims_split") as mock_reconcile:
            _upsert_claims_split("user-1", [heritage_claim, other_claim])

        mock_reconcile.assert_called_once()
        called_batch = mock_reconcile.call_args[0][1]
        self.assertEqual(len(called_batch), 1)
        self.assertEqual(called_batch[0].bucket, "heritage")


# ---------------------------------------------------------------------------
# 1-2, 17. dispatch tests
# ---------------------------------------------------------------------------

class TestFlagDispatch(unittest.TestCase):
    def test_flag_off_uses_legacy_path(self):
        """With flag OFF, upsert_claims dispatches to _upsert_claims_legacy."""
        with patch.dict(os.environ, {"IDENTITY_SPLIT_WRITE_ENABLED": ""}, clear=False), \
             patch("app.claims_persist._upsert_claims_legacy", return_value=1) as mock_leg, \
             patch("app.claims_persist._upsert_claims_split", return_value=2) as mock_split:
            result = upsert_claims("user-1", [_claim("runner", "Runner", bucket="activity")])

        mock_leg.assert_called_once()
        mock_split.assert_not_called()
        self.assertEqual(result, 1)

    def test_flag_on_uses_split_path(self):
        """With flag ON, upsert_claims dispatches to _upsert_claims_split."""
        with patch.dict(os.environ, {"IDENTITY_SPLIT_WRITE_ENABLED": "1"}, clear=False), \
             patch("app.claims_persist._upsert_claims_legacy", return_value=1) as mock_leg, \
             patch("app.claims_persist._upsert_claims_split", return_value=2) as mock_split:
            result = upsert_claims("user-1", [_claim("runner", "Runner", bucket="activity")])

        mock_split.assert_called_once()
        mock_leg.assert_not_called()
        self.assertEqual(result, 2)

    def test_upsert_claims_dispatch_by_flag(self):
        """Direct env-based dispatch test using patch.dict."""
        c = _claim("runner", "Runner", bucket="activity")

        # Flag OFF -> legacy
        with patch.dict(os.environ, {"IDENTITY_SPLIT_WRITE_ENABLED": "0"}, clear=False), \
             patch("app.claims_persist._upsert_claims_legacy", return_value=3) as mock_leg, \
             patch("app.claims_persist._upsert_claims_split", return_value=4) as mock_split:
            r = upsert_claims("user-1", [c])
        self.assertEqual(r, 3)
        mock_leg.assert_called_once()
        mock_split.assert_not_called()

        # Flag ON -> split
        with patch.dict(os.environ, {"IDENTITY_SPLIT_WRITE_ENABLED": "true"}, clear=False), \
             patch("app.claims_persist._upsert_claims_legacy", return_value=3) as mock_leg2, \
             patch("app.claims_persist._upsert_claims_split", return_value=4) as mock_split2:
            r2 = upsert_claims("user-1", [c])
        self.assertEqual(r2, 4)
        mock_split2.assert_called_once()
        mock_leg2.assert_not_called()


if __name__ == "__main__":
    unittest.main()
