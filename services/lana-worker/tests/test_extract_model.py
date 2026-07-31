import os
import unittest

from app.orchestrator import extract


class TestExtractModelRegression(unittest.TestCase):
    """_extract_model() must never hand a Gemini model id to the OpenAI branch
    of llm_json — that sent "gemini-2.5-flash" to chat.completions.create and
    502'd every POST /lana/sessions/{id}/complete (found 2026-07-30)."""

    def setUp(self) -> None:
        self._saved: dict[str, str | None] = {}
        for key in (
            "LANA_LLM_PROVIDER",
            "OPENAI_API_KEY",
            "OPENAI_SYNTH_MODEL",
            "VERTEX_EXTRACT_MODEL",
            "LANA_EXTRACT_MODEL",
        ):
            self._saved[key] = os.environ.get(key)

    def tearDown(self) -> None:
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_openai_provider_never_returns_a_gemini_model(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_SYNTH_MODEL"] = "gpt-4.1"
        os.environ["VERTEX_EXTRACT_MODEL"] = "gemini-2.5-flash"
        os.environ.pop("LANA_EXTRACT_MODEL", None)
        self.assertNotIn("gemini", extract._extract_model())
        self.assertEqual(extract._extract_model(), "gpt-4.1")

    def test_gemini_provider_still_uses_vertex_extract_model(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "gemini"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["VERTEX_EXTRACT_MODEL"] = "gemini-2.5-flash"
        os.environ.pop("LANA_EXTRACT_MODEL", None)
        self.assertEqual(extract._extract_model(), "gemini-2.5-flash")

    def test_explicit_override_wins(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["LANA_EXTRACT_MODEL"] = "gpt-4.1-mini"
        self.assertEqual(extract._extract_model(), "gpt-4.1-mini")


if __name__ == "__main__":
    unittest.main()
