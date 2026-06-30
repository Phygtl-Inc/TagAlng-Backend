import unittest
from unittest.mock import MagicMock, patch

from app.latent_extract import (
    ExtractedEntity,
    extract_entities_from_message,
    parse_entities,
    run_latent_intent,
    should_extract_entities,
)


class TestParseEntities(unittest.TestCase):
    def test_parses_valid_entity(self) -> None:
        out = parse_entities(
            {"entities": [{"text": "karate", "type": "activity", "subject": "child",
                           "confidence": 0.9, "attributes": {"child_age": 5}}]}
        )
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual((e.text, e.type, e.subject), ("karate", "activity", "child"))
        self.assertEqual(e.attributes, {"child_age": 5})

    def test_unknown_subject_normalized(self) -> None:
        out = parse_entities({"entities": [{"text": "yoga", "type": "activity", "subject": "grandma"}]})
        self.assertEqual(out[0].subject, "unknown")

    def test_drops_entity_without_text_or_type(self) -> None:
        out = parse_entities({"entities": [{"text": "", "type": "activity"},
                                           {"text": "karate", "type": ""}]})
        self.assertEqual(out, [])

    def test_confidence_clamped_and_attrs_coerced(self) -> None:
        out = parse_entities({"entities": [{"text": "x", "type": "t", "confidence": 5, "attributes": "nope"}]})
        self.assertEqual(out[0].confidence, 1.0)
        self.assertEqual(out[0].attributes, {})

    def test_caps_at_six(self) -> None:
        many = {"entities": [{"text": f"e{i}", "type": "t"} for i in range(20)]}
        self.assertEqual(len(parse_entities(many)), 6)

    def test_garbage_input(self) -> None:
        self.assertEqual(parse_entities(None), [])
        self.assertEqual(parse_entities({"entities": "no"}), [])


class TestShouldExtract(unittest.TestCase):
    def test_skips_trivial(self) -> None:
        for msg in ["", "ok", "thanks", "12345", "555-123-4567", "+1 (555) 123 4567"]:
            self.assertFalse(should_extract_entities(msg), msg)

    def test_allows_real_message(self) -> None:
        self.assertTrue(should_extract_entities("my kid does karate on saturdays"))


class TestExtractViaLLM(unittest.TestCase):
    def test_uses_llm_when_configured(self) -> None:
        import app.orchestrator.llm as llm

        orig = (llm.llm_configured, llm.llm_json)
        llm.llm_configured = lambda: True
        llm.llm_json = lambda **kwargs: {"entities": [{"text": "karate", "type": "activity",
                                                        "subject": "child", "confidence": 0.9}]}
        self.addCleanup(lambda: setattr(llm, "llm_configured", orig[0]))
        self.addCleanup(lambda: setattr(llm, "llm_json", orig[1]))

        out = extract_entities_from_message("my kid does karate")
        self.assertEqual(out[0].text, "karate")
        self.assertEqual(out[0].subject, "child")


class TestRunLatentIntent(unittest.TestCase):
    @patch("app.latent_extract.service_client")
    @patch("app.latent_extract._embed_entity", return_value=[0.0] * 768)
    @patch("app.latent_extract.extract_entities_from_message")
    def test_writes_signal_and_queues_match(self, mock_extract, _mock_embed, mock_sc) -> None:
        mock_extract.return_value = [ExtractedEntity(text="karate", type="activity",
                                                     subject="child", confidence=0.9)]
        sb = MagicMock()
        sb.rpc.return_value.execute.return_value.data = [
            {"capability_id": "looking.meet", "similarity": 0.82}
        ]
        mock_sc.return_value = sb

        result = run_latent_intent("u1", "s1", "t1", "blk1", "my kid does karate on saturdays")

        self.assertEqual(result["entities"], 1)
        self.assertEqual(result["signals"], 1)
        self.assertEqual(result["suggestions"], 1)
        # one insert into latent_signals, one into suggestion_queue
        inserted_tables = [c.args[0] for c in sb.table.call_args_list]
        self.assertIn("latent_signals", inserted_tables)
        self.assertIn("suggestion_queue", inserted_tables)

    @patch("app.latent_extract.service_client")
    @patch("app.latent_extract.extract_entities_from_message")
    def test_guard_short_circuits(self, mock_extract, mock_sc) -> None:
        result = run_latent_intent("u1", "s1", "t1", "blk1", "ok")
        self.assertEqual(result, {"entities": 0, "signals": 0, "suggestions": 0})
        mock_extract.assert_not_called()
        mock_sc.assert_not_called()

    @patch("app.latent_extract.service_client")
    @patch("app.latent_extract._embed_entity", return_value=[0.0] * 768)
    @patch("app.latent_extract.extract_entities_from_message")
    def test_skips_low_confidence_entity(self, mock_extract, _mock_embed, mock_sc) -> None:
        mock_extract.return_value = [ExtractedEntity(text="maybe", type="interest", confidence=0.2)]
        mock_sc.return_value = MagicMock()
        result = run_latent_intent("u1", "s1", "t1", "blk1", "i kind of like something maybe")
        self.assertEqual(result["signals"], 0)

    @patch("app.latent_extract.service_client")
    @patch("app.latent_extract._embed_entity", return_value=None)
    @patch("app.latent_extract.extract_entities_from_message")
    def test_signal_written_without_embedding_skips_queue(self, mock_extract, _mock_embed, mock_sc) -> None:
        mock_extract.return_value = [ExtractedEntity(text="karate", type="activity", confidence=0.9)]
        sb = MagicMock()
        mock_sc.return_value = sb
        result = run_latent_intent("u1", "s1", "t1", "blk1", "my kid does karate")
        self.assertEqual(result["signals"], 1)
        self.assertEqual(result["suggestions"], 0)  # no embedding -> no match query
        sb.rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
