"""finalize_reply_language: the final-mile localizer renders EVERY outbound
reply into the session language — there is no composer opt-out.

Regression for QA 2026-07-30: composers stamped ``_reply_localized`` to skip
the final-mile render on the promise that their text was already in-language;
nobody verified the text, and a Spanish tip-seek reply shipped with an English
sentence inside ("I'll text you when a neighbor shares a tip that fits").
The contract now: stale flag residue must never suppress the render, and the
legacy key is cleared with None (never popped — the session merge resurrects
popped keys)."""

import unittest

import app.i18n as i18n
from app.i18n import finalize_reply_language


class TestFinalizeReplyLanguage(unittest.TestCase):
    def setUp(self) -> None:
        i18n._AI_RENDER_CACHE.clear()

    def tearDown(self) -> None:
        i18n._AI_RENDER_CACHE.clear()

    def test_renders_even_when_legacy_flag_stamped(self) -> None:
        # A composer vouching "already localized" must not skip the render.
        mixed = "Aquí tienes opciones cerca. I'll text you when a neighbor shares a tip that fits."
        # _render_key, not a bare (text, lang) tuple: the cache is keyed on the
        # addressee's grammatical gender too, so a hand-built 2-tuple silently misses
        # (that key shape is exactly the cross-user leak — see test_i18n_gender.py).
        i18n._AI_RENDER_CACHE[i18n._render_key(mixed, "es")] = (
            "Aquí tienes opciones cerca. Te aviso cuando llegue un dato."
        )
        ctx = {"lang": "es", "_reply_localized": True}
        out = finalize_reply_language(mixed, ctx)
        self.assertEqual(out, "Aquí tienes opciones cerca. Te aviso cuando llegue un dato.")

    def test_legacy_flag_cleared_with_none_not_popped(self) -> None:
        ctx = {"lang": "es", "_reply_localized": True}
        finalize_reply_language("Hola.", ctx)
        self.assertIn("_reply_localized", ctx)
        self.assertIsNone(ctx["_reply_localized"])

    def test_english_session_returns_text_unchanged(self) -> None:
        ctx = {"lang": "en", "_reply_localized": True}
        out = finalize_reply_language("See you there!", ctx)
        self.assertEqual(out, "See you there!")
        self.assertIsNone(ctx["_reply_localized"])

    def test_no_session_lang_returns_text_unchanged(self) -> None:
        out = finalize_reply_language("See you there!", {})
        self.assertEqual(out, "See you there!")

    def test_llm_unconfigured_falls_back_to_original_text(self) -> None:
        # No LLM in tests: localize_text returns the text unchanged rather
        # than failing the turn.
        ctx = {"lang": "es"}
        out = finalize_reply_language("Plain deterministic line.", ctx)
        self.assertEqual(out, "Plain deterministic line.")

    def test_empty_reply_passes_through(self) -> None:
        ctx = {"lang": "es"}
        self.assertEqual(finalize_reply_language("", ctx), "")
        self.assertIsNone(ctx["_reply_localized"])

    def test_none_session_ctx_is_safe(self) -> None:
        self.assertEqual(finalize_reply_language("Hi.", None), "Hi.")


if __name__ == "__main__":
    unittest.main()
