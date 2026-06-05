import json
import unittest

from app.orchestrator.json_util import (
    parse_json_object,
    repair_json_text,
    salvage_extract_object,
    salvage_json_object,
)


class TestJsonUtil(unittest.TestCase):
    def test_repair_union_syntax(self) -> None:
        raw = '{"status": "continue" | "ready_to_complete", "assistant_message": "Hi"}'
        data = json.loads(repair_json_text(raw))
        self.assertEqual(data["status"], "continue")

    def test_parse_with_fence_and_trailing_comma(self) -> None:
        raw = '```json\n{"outcome": "R", "confidence": 0.9,}\n```'
        data = parse_json_object(raw)
        self.assertEqual(data["outcome"], "R")

    def test_salvage_truncated_assistant_message(self) -> None:
        raw = '{\n  "assistant_message": "Hey — tell me more about'
        data = parse_json_object(raw)
        self.assertIn("assistant_message", data)
        self.assertTrue(data["assistant_message"])

    def test_salvage_router_outcome(self) -> None:
        raw = '{"outcome": "R", "confidence": 0.82, "thinking": "chat'
        data = salvage_json_object(raw)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["outcome"], "R")

    def test_repair_bucket_union_in_claim(self) -> None:
        raw = (
            '{"claims": [{"concept": "parent", "label": "Parent", '
            '"bucket": "stage" | "general", "disclosure": "public"}]}'
        )
        data = json.loads(repair_json_text(raw))
        self.assertEqual(data["claims"][0]["bucket"], "stage")

    def test_salvage_extract_with_broken_claims_array(self) -> None:
        raw = (
            '{\n  "mapped_summary": "Amanda is a Brazilian parent who loves baking.",\n'
            '  "claims": [\n'
            '    {"concept": "parent", "label": "Parent", "confidence": 0.9, '
            '"disclosure": "public", "source_quote": "I am a mom", "bucket": "stage"},\n'
            '    {"concept": "brazilian_heritage", "label": "Brazilian", "confidence": 0.85, '
            '"disclosure": "public", "source_quote": "Brazilian", "bucket": "heritage"'
        )
        data = salvage_extract_object(raw)
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(len(data["claims"]), 2)
        self.assertIn("Brazilian", data["mapped_summary"])


if __name__ == "__main__":
    unittest.main()
