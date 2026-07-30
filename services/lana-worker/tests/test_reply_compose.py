"""compose_reply: AI-authored deterministic-path replies with a canned floor.

The contract under test: the fallback string is returned verbatim whenever the
LLM is unconfigured, disabled via LANA_AI_REPLIES, or fails; a successful
compose returns the model text and honors the static cache only when asked.
Composers never stamp _reply_localized — the final-mile localizer in main.py
renders every outbound reply unconditionally (the trust-based opt-out shipped
mixed-language replies, QA 2026-07-30)."""

import os
import unittest
from unittest.mock import patch

import app.reply_compose as rc
from app.reply_compose import compose_reply


class TestFallbackFloor(unittest.TestCase):
    def test_llm_unconfigured_returns_fallback(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            out = compose_reply(goal="Ask for a ZIP.", fallback="What ZIP are you in?")
        self.assertEqual(out, "What ZIP are you in?")

    def test_kill_switch_returns_fallback(self) -> None:
        with patch.dict(os.environ, {"LANA_AI_REPLIES": "0"}):
            with patch("app.orchestrator.llm.llm_configured", return_value=True):
                out = compose_reply(goal="Ask for a ZIP.", fallback="What ZIP are you in?")
        self.assertEqual(out, "What ZIP are you in?")

    def test_llm_failure_returns_fallback(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", side_effect=RuntimeError("boom")
        ):
            out = compose_reply(goal="Ask for a ZIP.", fallback="What ZIP are you in?")
        self.assertEqual(out, "What ZIP are you in?")

    def test_empty_model_message_returns_fallback(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value={"message": "  "}
        ):
            out = compose_reply(goal="Ask for a ZIP.", fallback="What ZIP are you in?")
        self.assertEqual(out, "What ZIP are you in?")


class TestCompose(unittest.TestCase):
    def setUp(self) -> None:
        rc._STATIC_CACHE.clear()

    def test_composed_message_returned(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json",
            return_value={"message": "Which ZIP should I look at for you?"},
        ):
            out = compose_reply(goal="Ask for a ZIP.", fallback="What ZIP are you in?")
        self.assertEqual(out, "Which ZIP should I look at for you?")

    def test_cache_hits_skip_second_llm_call(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value={"message": "Hi there!"}
        ) as llm:
            a = compose_reply(goal="Greet.", fallback="Hi.", cache=True)
            b = compose_reply(goal="Greet.", fallback="Hi.", cache=True)
        self.assertEqual((a, b), ("Hi there!", "Hi there!"))
        self.assertEqual(llm.call_count, 1)

    def test_no_cache_by_default(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value={"message": "Hi there!"}
        ) as llm:
            compose_reply(goal="Greet.", fallback="Hi.")
            compose_reply(goal="Greet.", fallback="Hi.")
        self.assertEqual(llm.call_count, 2)

    def test_facts_and_user_message_reach_payload(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value={"message": "ok"}
        ) as llm:
            compose_reply(
                goal="Confirm the post.",
                facts=["The ask: stroller swap"],
                user_message="posted?",
                fallback="Posted!",
            )
        payload = llm.call_args.kwargs["user_payload"]
        self.assertIn("The ask: stroller swap", payload)
        self.assertIn("posted?", payload)

    def test_constitution_in_system_prompt(self) -> None:
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value={"message": "ok"}
        ) as llm:
            compose_reply(goal="Greet.", fallback="Hi.")
        self.assertIn("Lana lingo", llm.call_args.kwargs["system"])


class TestLocalization(unittest.TestCase):
    def setUp(self) -> None:
        rc._STATIC_CACHE.clear()

    def test_non_english_session_composes_in_language(self) -> None:
        ctx = {"lang": "es"}
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", return_value={"message": "¡Hola!"}
        ) as llm:
            out = compose_reply(goal="Greet.", fallback="Hi.", session_ctx=ctx)
        self.assertEqual(out, "¡Hola!")
        self.assertIn("Spanish", llm.call_args.kwargs["system"])

    def test_compose_never_stamps_reply_localized(self) -> None:
        # The final-mile localizer renders every reply; a composer stamping
        # the legacy opt-out would re-open the mixed-language leak.
        for configured in (True, False):
            ctx = {"lang": "es"}
            with patch(
                "app.orchestrator.llm.llm_configured", return_value=configured
            ), patch(
                "app.orchestrator.llm.llm_json", return_value={"message": "¡Hola!"}
            ):
                compose_reply(goal="Greet.", fallback="Hi.", session_ctx=ctx)
            self.assertNotIn("_reply_localized", ctx)


if __name__ == "__main__":
    unittest.main()
