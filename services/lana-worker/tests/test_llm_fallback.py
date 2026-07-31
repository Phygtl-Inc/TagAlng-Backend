"""PR9 · LLM fallback parity — limits, failover, and cross-provider model safety."""

import os
import pathlib
import unittest
from unittest import mock

from app.orchestrator import llm


class _EnvCase(unittest.TestCase):
    ENV_KEYS = (
        "LANA_LLM_PROVIDER",
        "LANA_LLM_FALLBACK",
        "OPENAI_API_KEY",
        "OPENAI_TIMEOUT_SEC",
        "OPENAI_MAX_RETRIES",
        "OPENAI_ROUTER_MODEL",
        "OPENAI_SYNTH_MODEL",
        "GCP_VERTEX_PROJECT",
        "VERTEX_TIMEOUT_SEC",
        "VERTEX_MAX_OUTPUT_TOKENS",
        "VERTEX_LANA_ROUTER_MODEL",
        "VERTEX_LANA_SYNTH_MODEL",
    )

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}

    def tearDown(self) -> None:
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


class TestFallbackParity(_EnvCase):
    def test_vertex_timeout_defaults_to_openai_timeout(self) -> None:
        os.environ["OPENAI_TIMEOUT_SEC"] = "15"
        os.environ.pop("VERTEX_TIMEOUT_SEC", None)
        self.assertEqual(llm._vertex_timeout_sec(), 15.0)

    def test_vertex_timeout_override_wins(self) -> None:
        os.environ["OPENAI_TIMEOUT_SEC"] = "15"
        os.environ["VERTEX_TIMEOUT_SEC"] = "30"
        self.assertEqual(llm._vertex_timeout_sec(), 30.0)

    def test_gemini_config_always_caps_tokens_and_sets_timeout(self) -> None:
        os.environ.pop("VERTEX_MAX_OUTPUT_TOKENS", None)
        cfg = llm.gemini_config(system="s", temperature=0.2, max_tokens=512)
        self.assertEqual(cfg.max_output_tokens, 1024)
        self.assertIsNotNone(cfg.http_options.timeout)  # milliseconds

    def test_largest_caller_budget_keeps_thinking_headroom(self) -> None:
        """A 4096-token extract must still get 2x headroom on Gemini — a 4096
        ceiling would clamp it back to 4096 and truncate long transcripts."""
        os.environ.pop("VERTEX_MAX_OUTPUT_TOKENS", None)
        self.assertEqual(llm._vertex_max_output_tokens(4096), 8192)

    def test_token_ceiling_is_respected(self) -> None:
        os.environ["VERTEX_MAX_OUTPUT_TOKENS"] = "1000"
        self.assertEqual(llm._vertex_max_output_tokens(4096), 1000)

    def test_openai_max_retries_is_explicit_and_bounded(self) -> None:
        os.environ.pop("OPENAI_MAX_RETRIES", None)
        self.assertEqual(llm._openai_max_retries(), 2)
        os.environ["OPENAI_MAX_RETRIES"] = "99"
        self.assertEqual(llm._openai_max_retries(), 5)
        os.environ["OPENAI_MAX_RETRIES"] = "junk"
        self.assertEqual(llm._openai_max_retries(), 2)


def _status_error(code: int) -> Exception:
    exc = Exception(f"http {code}")
    exc.status_code = code  # type: ignore[attr-defined]
    return exc


class TestFallbackTrigger(_EnvCase):
    """The three triggers: timeout, 429, 5xx — plus what must NOT fail over."""

    def _run(self, exc: BaseException, *, model: str = "gpt-4.1"):
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GCP_VERTEX_PROJECT"] = "p"
        os.environ.setdefault("LANA_LLM_FALLBACK", "1")
        with mock.patch.object(llm, "_openai_json", side_effect=exc), mock.patch.object(
            llm, "_gemini_json", return_value=({"ok": True}, 1)
        ) as gem:
            out = llm.llm_json(model=model, system="s", user_payload="u", max_tokens=512)
        return out, gem

    def test_timeout_falls_back(self) -> None:
        out, gem = self._run(TimeoutError("timed out"))
        self.assertEqual(out, {"ok": True})
        self.assertEqual(gem.call_args.kwargs["max_tokens"], 512)  # SAME budget

    def test_rate_limit_429_falls_back(self) -> None:
        out, _ = self._run(_status_error(429))
        self.assertEqual(out, {"ok": True})

    def test_server_5xx_falls_back(self) -> None:
        out, _ = self._run(_status_error(503))
        self.assertEqual(out, {"ok": True})

    def test_google_style_code_attribute_falls_back(self) -> None:
        exc = Exception("vertex 503")
        exc.code = 503  # type: ignore[attr-defined]
        os.environ["LANA_LLM_PROVIDER"] = "gemini"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GCP_VERTEX_PROJECT"] = "p"
        with mock.patch.object(llm, "_gemini_json", side_effect=exc), mock.patch.object(
            llm, "_openai_json", return_value=({"ok": True}, 1)
        ):
            out = llm.llm_json(model="gemini-2.5-pro", system="s", user_payload="u")
        self.assertEqual(out, {"ok": True})

    def test_model_slot_translates(self) -> None:
        """A synth-tier request must become the VERTEX synth model, never the
        literal 'gpt-4.1'."""
        os.environ["OPENAI_SYNTH_MODEL"] = "gpt-4.1"
        os.environ["VERTEX_LANA_SYNTH_MODEL"] = "gemini-2.5-pro"
        _, gem = self._run(_status_error(429), model="gpt-4.1")
        self.assertEqual(gem.call_args.kwargs["model"], "gemini-2.5-pro")

    def test_router_slot_translates(self) -> None:
        os.environ["OPENAI_ROUTER_MODEL"] = "gpt-4.1-mini"
        os.environ["OPENAI_SYNTH_MODEL"] = "gpt-4.1"
        os.environ["VERTEX_LANA_ROUTER_MODEL"] = "gemini-2.5-flash"
        _, gem = self._run(_status_error(429), model="gpt-4.1-mini")
        self.assertEqual(gem.call_args.kwargs["model"], "gemini-2.5-flash")

    def test_auth_error_does_NOT_fall_back(self) -> None:
        """401/400/model_not_found are our bugs — they must stay loud."""
        with self.assertRaises(Exception) as ctx:
            self._run(_status_error(401))
        self.assertEqual(getattr(ctx.exception, "status_code", None), 401)

    def test_model_not_found_does_NOT_fall_back(self) -> None:
        exc = Exception("model_not_found")
        exc.status_code = 404  # type: ignore[attr-defined]
        with self.assertRaises(Exception):
            self._run(exc)

    def test_flag_off_restores_old_behaviour(self) -> None:
        os.environ["LANA_LLM_FALLBACK"] = "0"
        with self.assertRaises(TimeoutError):
            self._run(TimeoutError("timed out"))

    def test_no_fallback_when_other_provider_unconfigured(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ.pop("GCP_VERTEX_PROJECT", None)
        with mock.patch.object(llm, "_openai_json", side_effect=_status_error(429)):
            with self.assertRaises(Exception):
                llm.llm_json(model="gpt-4.1", system="s", user_payload="u")

    def test_original_error_surfaces_when_fallback_also_fails(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GCP_VERTEX_PROJECT"] = "p"
        primary = _status_error(429)
        with mock.patch.object(llm, "_openai_json", side_effect=primary), mock.patch.object(
            llm, "_gemini_json", side_effect=RuntimeError("vertex down")
        ):
            with self.assertRaises(Exception) as ctx:
                llm.llm_json(model="gpt-4.1", system="s", user_payload="u")
        self.assertIs(ctx.exception, primary)

    def test_fallback_is_silent_to_the_caller_and_logged(self) -> None:
        with self.assertLogs("app.orchestrator.llm", level="WARNING") as cm:
            out, _ = self._run(_status_error(429))
        self.assertEqual(out, {"ok": True})  # silent: normal return
        self.assertTrue(any("llm_fallback" in m for m in cm.output))  # logged

    def test_fallback_served_turn_is_countable(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["GCP_VERTEX_PROJECT"] = "p"
        box: list[int] = []
        with mock.patch.object(
            llm, "_openai_json", side_effect=_status_error(429)
        ), mock.patch.object(llm, "_gemini_json", return_value=({"ok": True}, 1)):
            llm.llm_json(model="gpt-4.1", system="s", user_payload="u", llm_attempts=box)
        self.assertGreaterEqual(box[0], 11)


class TestCrossProviderDownshift(_EnvCase):
    """The bad-JSON downshift must stay on the provider being dispatched to.

    router_model() follows provider(), which still names the PRIMARY provider
    during a fallback — using it would send a gpt-* id to Vertex (or a gemini-*
    id to OpenAI), which is the very defect this PR fixes."""

    def test_gemini_dispatch_downshifts_to_a_gemini_model(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "openai"  # primary stays openai
        os.environ["OPENAI_ROUTER_MODEL"] = "gpt-4.1-mini"
        os.environ["VERTEX_LANA_ROUTER_MODEL"] = "gemini-2.5-flash"
        captured: dict[str, object] = {}

        def fake_gemini_json(**kwargs):
            captured.update(kwargs)
            return {"ok": True}, 1

        with mock.patch.object(llm, "_gemini_json", side_effect=fake_gemini_json):
            llm._dispatch(
                "gemini",
                model="gemini-2.5-pro",
                system="s",
                user_payload="u",
                max_tokens=512,
                temperature=0.2,
                llm_attempts=None,
            )
        self.assertEqual(captured["downshift_model"], "gemini-2.5-flash")
        self.assertNotIn("gpt", str(captured["downshift_model"]))

    def test_openai_dispatch_downshifts_to_an_openai_model(self) -> None:
        os.environ["LANA_LLM_PROVIDER"] = "gemini"  # primary stays gemini
        os.environ["OPENAI_ROUTER_MODEL"] = "gpt-4.1-mini"
        os.environ["VERTEX_LANA_ROUTER_MODEL"] = "gemini-2.5-flash"
        captured: dict[str, object] = {}

        def fake_openai_json(**kwargs):
            captured.update(kwargs)
            return {"ok": True}, 1

        with mock.patch.object(llm, "_openai_json", side_effect=fake_openai_json):
            llm._dispatch(
                "openai",
                model="gpt-4.1",
                system="s",
                user_payload="u",
                max_tokens=512,
                temperature=0.2,
                llm_attempts=None,
            )
        self.assertEqual(captured["downshift_model"], "gpt-4.1-mini")
        self.assertNotIn("gemini", str(captured["downshift_model"]))


class TestNoUnboundedVertexCalls(unittest.TestCase):
    """Static guard: nobody may call generate_content() outside llm.py again."""

    def test_generate_content_only_in_llm_module(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "generate_content(" in p.read_text() and p.name != "llm.py"
        ]
        self.assertEqual(offenders, [], f"direct Vertex calls found: {offenders}")


if __name__ == "__main__":
    unittest.main()
