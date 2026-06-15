import os
import unittest

from app.orchestrator.llm import (
    llm_configured,
    openai_configured,
    provider,
    router_model,
    synthesizer_model,
    vertex_configured,
)


class TestLlmProvider(unittest.TestCase):
    def setUp(self) -> None:
        self._saved: dict[str, str | None] = {}
        for key in (
            "LANA_LLM_PROVIDER",
            "OPENAI_API_KEY",
            "GCP_VERTEX_PROJECT",
            "OPENAI_ROUTER_MODEL",
            "OPENAI_SYNTH_MODEL",
        ):
            self._saved[key] = os.environ.get(key)

    def tearDown(self) -> None:
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_openai_provider_when_key_set(self) -> None:
        os.environ.pop("LANA_LLM_PROVIDER", None)
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ.pop("GCP_VERTEX_PROJECT", None)
        self.assertEqual(provider(), "openai")
        self.assertTrue(openai_configured())
        self.assertTrue(llm_configured())
        self.assertEqual(router_model(), "gpt-4o-mini")
        self.assertEqual(synthesizer_model(), "gpt-4o")

    def test_explicit_openai_models(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_ROUTER_MODEL"] = "gpt-4o-mini-2024-07-18"
        os.environ["OPENAI_SYNTH_MODEL"] = "gpt-4o-2024-08-06"
        self.assertEqual(router_model(), "gpt-4o-mini-2024-07-18")
        self.assertEqual(synthesizer_model(), "gpt-4o-2024-08-06")

    def test_gemini_when_vertex_only(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "gemini"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["GCP_VERTEX_PROJECT"] = "test-project"
        self.assertEqual(provider(), "gemini")
        self.assertTrue(vertex_configured())
        self.assertTrue(llm_configured())

    def test_not_configured_without_keys(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("GCP_VERTEX_PROJECT", None)
        self.assertFalse(llm_configured())


if __name__ == "__main__":
    unittest.main()
