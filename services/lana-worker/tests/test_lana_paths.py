import os
import unittest

from app.lana_paths import (
    event_fast_path_enabled,
    profile_fast_path_enabled,
    use_orchestrator_for_purpose,
)


class TestLanaPaths(unittest.TestCase):
    def setUp(self) -> None:
        self._saved: dict[str, str | None] = {}
        for key in (
            "LANA_EVENT_FAST_PATH",
            "LANA_PROFILE_FAST_PATH",
            "LANA_ORCHESTRATOR",
            "GCP_VERTEX_PROJECT",
            "OPENAI_API_KEY",
            "LANA_LLM_PROVIDER",
        ):
            self._saved[key] = os.environ.get(key)

    def tearDown(self) -> None:
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def test_event_fast_path_default_on(self) -> None:
        os.environ.pop("LANA_EVENT_FAST_PATH", None)
        self.assertTrue(event_fast_path_enabled())

    def test_event_draft_skips_orchestrator_when_vertex_on(self) -> None:
        os.environ.pop("LANA_EVENT_FAST_PATH", None)
        os.environ["LANA_ORCHESTRATOR"] = "1"
        os.environ["GCP_VERTEX_PROJECT"] = "test-project"
        self.assertFalse(use_orchestrator_for_purpose("event_draft"))

    def test_profile_fast_path_default_on(self) -> None:
        os.environ.pop("LANA_PROFILE_FAST_PATH", None)
        self.assertTrue(profile_fast_path_enabled())

    def test_profile_intake_skips_orchestrator_when_vertex_on(self) -> None:
        os.environ.pop("LANA_EVENT_FAST_PATH", None)
        os.environ.pop("LANA_PROFILE_FAST_PATH", None)
        os.environ["LANA_ORCHESTRATOR"] = "1"
        os.environ["GCP_VERTEX_PROJECT"] = "test-project"
        self.assertFalse(use_orchestrator_for_purpose("profile_intake"))

    def test_event_fast_path_can_be_disabled(self) -> None:
        os.environ["LANA_EVENT_FAST_PATH"] = "0"
        os.environ["LANA_ORCHESTRATOR"] = "1"
        os.environ["GCP_VERTEX_PROJECT"] = "test-project"
        self.assertTrue(use_orchestrator_for_purpose("event_draft"))

    def test_orchestrator_off_for_all_purposes(self) -> None:
        os.environ["LANA_ORCHESTRATOR"] = "0"
        os.environ["GCP_VERTEX_PROJECT"] = "test-project"
        self.assertFalse(use_orchestrator_for_purpose("profile_intake"))
        self.assertFalse(use_orchestrator_for_purpose("event_draft"))

    def test_lana_uses_orchestrator_when_vertex_on(self) -> None:
        os.environ["LANA_ORCHESTRATOR"] = "1"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["GCP_VERTEX_PROJECT"] = "test-project"
        os.environ["LANA_LLM_PROVIDER"] = "gemini"
        self.assertTrue(use_orchestrator_for_purpose("lana"))

    def test_lana_uses_orchestrator_when_openai_on(self) -> None:
        os.environ["LANA_ORCHESTRATOR"] = "1"
        os.environ.pop("GCP_VERTEX_PROJECT", None)
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        self.assertTrue(use_orchestrator_for_purpose("lana"))

    def test_unified_rules_first_default_on(self) -> None:
        from app.lana_paths import unified_rules_first_enabled

        os.environ.pop("LANA_UNIFIED_RULES_FIRST", None)
        self.assertTrue(unified_rules_first_enabled())


class TestStretchOfferFlag(unittest.TestCase):
    """Rapport Reply ships dark: off unless LANA_STRETCH_OFFER is set AND the model is
    configured (no model means no scores to read)."""

    def _enabled(self, env: dict[str, str], *, llm: bool) -> bool:
        from unittest.mock import patch

        from app.lana_paths import stretch_offer_enabled

        with patch.dict(os.environ, env, clear=False), patch(
            "app.orchestrator.llm.llm_configured", return_value=llm
        ):
            if "LANA_STRETCH_OFFER" not in env:
                os.environ.pop("LANA_STRETCH_OFFER", None)
            return stretch_offer_enabled()

    def test_off_by_default(self) -> None:
        self.assertFalse(self._enabled({}, llm=True))

    def test_on_when_set_and_model_configured(self) -> None:
        self.assertTrue(self._enabled({"LANA_STRETCH_OFFER": "1"}, llm=True))

    def test_off_without_a_model_even_when_set(self) -> None:
        self.assertFalse(self._enabled({"LANA_STRETCH_OFFER": "1"}, llm=False))

    def test_explicit_off_values(self) -> None:
        for val in ("0", "false", "off"):
            self.assertFalse(self._enabled({"LANA_STRETCH_OFFER": val}, llm=True), val)


if __name__ == "__main__":
    unittest.main()
