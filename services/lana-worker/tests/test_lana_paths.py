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


if __name__ == "__main__":
    unittest.main()
