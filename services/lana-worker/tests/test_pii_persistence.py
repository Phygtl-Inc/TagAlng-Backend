"""Child PII must be provably clean at every durable write of user-derived text.

Functional coverage of the audited persistence call sites (QA finding 2026-07-08):
each write path is exercised with the QA sentence and the row/payload that would hit
the database is asserted clean — no child name, no school name, stage band instead of
an exact age. A source-audit test backstops the routing so a refactor that drops the
redaction call fails loudly.
"""

import inspect
import unittest
from unittest.mock import MagicMock, patch

# Pre-existing import cycle: app.claims_persist → app.vertex_extract →
# app.orchestrator.json_util → app.orchestrator.__init__ → … → app.vertex_extract.
# Initializing the orchestrator package first breaks it (mirrors full-suite order).
import app.orchestrator  # noqa: F401  (import-order shim, see above)

QA_SENTENCE = "my daughter Emma is 4, she goes to Sunshine Preschool on Narcoossee Rd"


def _assert_clean(testcase: unittest.TestCase, blob: str) -> None:
    testcase.assertNotIn("Emma", blob)
    testcase.assertNotIn("Sunshine", blob)
    testcase.assertIn("child_stage:prek", blob)


class TestClaimsPersistClean(unittest.TestCase):
    def test_qa_sentence_never_persists_in_claim_fields(self) -> None:
        from app.claims_persist import clean_claims_for_persist
        from app.models import ExtractedClaim

        claims = clean_claims_for_persist(
            [
                ExtractedClaim(
                    concept="parent_young_child",
                    label="Mom of Emma (4) at Sunshine Preschool",
                    confidence=0.9,
                    source_quote=QA_SENTENCE,
                    synonyms=["Emma's mom"],
                    bucket="stage",
                )
            ]
        )
        self.assertEqual(len(claims), 1)
        c = claims[0]
        blob = " ".join([c.label, c.source_quote or "", *c.synonyms])
        self.assertNotIn("Emma", blob)
        self.assertNotIn("Sunshine", blob)
        self.assertIn("child_stage:prek", c.source_quote or "")


class TestLatentSignalsClean(unittest.TestCase):
    @patch("app.latent_extract.service_client")
    @patch("app.latent_extract._embed_entity", return_value=[0.0] * 768)
    @patch("app.latent_extract.extract_entities_from_message")
    def test_latent_signal_row_and_suggestion_context_clean(
        self, mock_extract, _mock_embed, mock_sc
    ) -> None:
        from app.latent_extract import ExtractedEntity, run_latent_intent

        # Simulate the extractor leaking exactly what the prompt forbids.
        mock_extract.return_value = [
            ExtractedEntity(
                text="Sunshine Preschool",
                type="place",
                subject="child",
                confidence=0.9,
                attributes={"child_age": 4, "child_name": "Emma"},
            )
        ]
        sb = MagicMock()
        sb.rpc.return_value.execute.return_value.data = [
            {"capability_id": "cap-1", "similarity": 0.7}
        ]
        mock_sc.return_value = sb

        result = run_latent_intent("u1", "s1", None, None, QA_SENTENCE)
        self.assertEqual(result["signals"], 1)

        inserted_rows = [
            call.args[0]
            for call in sb.table.return_value.insert.call_args_list
        ]
        self.assertTrue(inserted_rows)
        for row in inserted_rows:
            blob = repr(row)
            self.assertNotIn("Emma", blob)
            self.assertNotIn("Sunshine Preschool", blob)
        # latent_signals row: excerpt banded, attributes reduced to the stage band.
        signal_row = inserted_rows[0]
        self.assertIn("child_stage:prek", signal_row["utterance_excerpt"])
        self.assertEqual(signal_row["attributes"].get("child_stage"), "prek")
        self.assertNotIn("child_age", signal_row["attributes"])
        self.assertNotIn("child_name", signal_row["attributes"])


class TestLocalSignalClean(unittest.TestCase):
    @patch("app.local_signals.call_rpc")
    def test_save_local_signal_redacts_detail(self, mock_rpc) -> None:
        from app.local_signals import save_local_signal

        mock_rpc.return_value = {"signal_id": "sig-1", "intent": "swap_seek"}
        save_local_signal(
            "jwt",
            intent="swap_seek",
            detail_text=f"rain boots for Emma, {QA_SENTENCE}",
        )
        payload = mock_rpc.call_args.args[2]
        _assert_clean(self, payload["p_detail_text"])


class TestInquiryCaptureClean(unittest.TestCase):
    @patch("app.orchestrator.tools.service_client")
    @patch("app.orchestrator.tools.vertex_embed", return_value=[0.0] * 768)
    def test_inquiry_free_text_and_embedding_input_clean(self, mock_embed, mock_sc) -> None:
        from app.orchestrator.tools import _capture_inquiry

        sb = MagicMock()
        sb.table.return_value.insert.return_value.execute.return_value.data = [{"id": "i1"}]
        mock_sc.return_value = sb

        out = _capture_inquiry(
            user_id="u1",
            session_id="s1",
            block_id=None,
            args={"raw_query": QA_SENTENCE, "category": "childcare"},
            source_module="test",
        )
        self.assertEqual(out["status"], "ok")
        row = sb.table.return_value.insert.call_args.args[0]
        _assert_clean(self, row["free_text"])
        # The persisted embedding is computed from the redacted text too.
        _assert_clean(self, mock_embed.call_args.args[0])


class TestDbLogsClean(unittest.TestCase):
    @patch("app.db.service_client")
    def test_feature_request_text_clean(self, mock_sc) -> None:
        from app.db import log_feature_request

        sb = MagicMock()
        mock_sc.return_value = sb
        log_feature_request(
            user_id="u1", block_id=None, request_text=f"can you babysit? {QA_SENTENCE}"
        )
        row = sb.table.return_value.insert.call_args.args[0]
        _assert_clean(self, row["request_text"])

    @patch("app.db.service_client")
    def test_moderation_flag_text_clean(self, mock_sc) -> None:
        from app.db import log_moderation_flag

        sb = MagicMock()
        mock_sc.return_value = sb
        log_moderation_flag(user_id="u1", block_id=None, message=QA_SENTENCE)
        row = sb.table.return_value.insert.call_args.args[0]
        _assert_clean(self, row["message"])


class TestRapportGapClean(unittest.TestCase):
    @patch("app.rapport_gaps.service_client")
    @patch("app.rapport_gaps._question_embedding", return_value=None)
    def test_gap_question_and_teaser_clean(self, _mock_embed, mock_sc) -> None:
        from app.rapport_gaps import open_semantic_gap

        sb = MagicMock()
        mock_sc.return_value = sb
        opened = open_semantic_gap(
            "u1",
            None,
            "How is Emma liking Sunshine Preschool? my daughter Emma is 4 right",
            label="Emma at Sunshine Preschool",
            teaser="about Emma's preschool…",
        )
        self.assertTrue(opened)
        row = sb.table.return_value.insert.call_args.args[0]
        blob = repr(row)
        self.assertNotIn("Emma", blob)
        self.assertNotIn("Sunshine", blob)


class TestCallSiteRouting(unittest.TestCase):
    """Source-audit backstop: every audited persistence function routes through pii."""

    def test_audited_call_sites_reference_redaction(self) -> None:
        from app import claims_persist, db, latent_extract, local_signals, rapport_gaps
        from app.orchestrator import tools

        audited = [
            claims_persist.clean_claims_for_persist,   # user_identity_claims
            latent_extract.run_latent_intent,          # latent_signals + suggestion_queue
            latent_extract._sanitize_entity_for_persistence,
            local_signals.save_local_signal,           # block signals RPC
            tools._capture_inquiry,                    # inquiry_signals
            db.log_feature_request,                    # feature_requests
            db.log_moderation_flag,                    # moderation_flags
            rapport_gaps.open_semantic_gap,            # rapport_gaps
        ]
        for fn in audited:
            src = inspect.getsource(fn)
            self.assertIn(
                "redact", src,
                f"{fn.__module__}.{fn.__name__} no longer routes through the PII redaction",
            )

    def test_reply_guard_wired_into_synthesizer_and_pipeline(self) -> None:
        import app.lana_unified_pipeline as pipeline
        import app.orchestrator.synthesizer as synth
        from app.lana_ui import sanitize_assistant_message
        from app.profile_intake import lana_profile_turn

        self.assertIn("user_message=utterance", inspect.getsource(synth.synthesize_turn))
        self.assertIn(
            "user_message=user_message", inspect.getsource(pipeline.run_lana_unified_turn)
            if hasattr(pipeline, "run_lana_unified_turn")
            else inspect.getsource(pipeline),
        )
        self.assertIn("enforce_child_pii_nonstorage", inspect.getsource(lana_profile_turn))
        self.assertIn("enforce_child_pii_nonstorage", inspect.getsource(sanitize_assistant_message))


if __name__ == "__main__":
    unittest.main()
