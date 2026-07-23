"""Language preference: seeding, AI lang verdicts, and the divergence nudge.

No LLM is configured under test, so every AI-composed line falls back to the
t() strings deterministically — the logic under test is the state machine.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.i18n import apply_ai_lang, localize_text, normalize_lang_code, t
from app.lang_pref import language_preference_post_turn, seed_session_language


class TestLangHelpers(unittest.TestCase):
    def test_normalize_lang_code(self) -> None:
        self.assertEqual(normalize_lang_code(" ES "), "es")
        self.assertEqual(normalize_lang_code("pt-br"), "pt-br")
        self.assertEqual(normalize_lang_code("ur"), "ur")
        self.assertIsNone(normalize_lang_code("not a code"))
        self.assertIsNone(normalize_lang_code(""))
        self.assertIsNone(normalize_lang_code(None))

    def test_apply_ai_lang_flips_session_both_ways(self) -> None:
        ctx: dict = {"lang": "ur"}
        apply_ai_lang(ctx, "en")  # confident English flips back — user switched
        self.assertEqual(ctx["lang"], "en")
        apply_ai_lang(ctx, "es")
        self.assertEqual(ctx["lang"], "es")
        apply_ai_lang(ctx, None)  # ambiguous (ZIP, 'ok') keeps the sticky lang
        self.assertEqual(ctx["lang"], "es")
        apply_ai_lang(ctx, "garbage!!")
        self.assertEqual(ctx["lang"], "es")

    def test_localize_text_without_llm_returns_text(self) -> None:
        self.assertEqual(localize_text("Hello neighbor!", "ur"), "Hello neighbor!")
        self.assertEqual(localize_text("Hello neighbor!", "en"), "Hello neighbor!")
        self.assertEqual(localize_text("Hello neighbor!", None), "Hello neighbor!")


class TestSeedSessionLanguage(unittest.TestCase):
    def test_seeds_lang_and_preference(self) -> None:
        ctx: dict = {}
        seed_session_language(ctx, "ur")
        self.assertEqual(ctx["lang"], "ur")
        self.assertEqual(ctx["preferred_lang"], "ur")

    def test_english_preference_seeds_no_lang(self) -> None:
        ctx: dict = {}
        seed_session_language(ctx, "en")
        self.assertEqual(ctx["preferred_lang"], "en")
        self.assertNotIn("lang", ctx)

    def test_never_overrides_existing_session_lang(self) -> None:
        ctx: dict = {"lang": "es"}
        seed_session_language(ctx, "ur")
        self.assertEqual(ctx["lang"], "es")

    def test_invalid_preference_is_ignored(self) -> None:
        ctx: dict = {}
        seed_session_language(ctx, "definitely not a code")
        self.assertEqual(ctx, {})


class TestPostTurnNudge(unittest.TestCase):
    def _turn(self, ctx: dict, *, msg: str = "hello there", reply: str = "Hi!") -> str:
        return language_preference_post_turn(
            user_id="u1",
            user_message=msg,
            session_ctx=ctx,
            reply=reply,
            is_anonymous=False,
        )

    def test_anonymous_untouched(self) -> None:
        ctx: dict = {"preferred_lang": "ur", "lang": "en"}
        out = language_preference_post_turn(
            user_id=None,
            user_message="hi",
            session_ctx=ctx,
            reply="Hi!",
            is_anonymous=True,
        )
        self.assertEqual(out, "Hi!")
        self.assertNotIn("lang_divergence_count", ctx)

    @patch("app.lang_pref._mark_nudged")
    @patch("app.lang_pref._nudge_allowed_by_cooldown", return_value=True)
    def test_nudge_after_three_divergent_turns(self, _cool, mark) -> None:
        ctx: dict = {"preferred_lang": "ur", "lang": "en"}
        out1 = self._turn(ctx)
        out2 = self._turn(ctx)
        self.assertEqual(out1, "Hi!")  # turns 1-2: no nudge yet
        self.assertEqual(out2, "Hi!")
        out3 = self._turn(ctx)
        expected = t("lang.nudge_offer", "en", new_name="English", old_name="Urdu")
        self.assertIn(expected, out3)
        self.assertEqual(ctx["lang_nudge_pending"], "en")
        mark.assert_called_once_with("u1")

    @patch("app.lang_pref._mark_nudged")
    @patch("app.lang_pref._nudge_allowed_by_cooldown", return_value=True)
    def test_matching_turn_resets_divergence(self, _cool, _mark) -> None:
        ctx: dict = {"preferred_lang": "ur", "lang": "en"}
        self._turn(ctx)
        self._turn(ctx)
        ctx["lang"] = "ur"  # she writes Urdu again — streak resets
        self._turn(ctx)
        self.assertEqual(ctx["lang_divergence_count"], 0)
        self.assertNotIn("lang_nudge_pending", ctx)

    @patch("app.lang_pref._nudge_allowed_by_cooldown", return_value=False)
    def test_cooldown_blocks_nudge(self, _cool) -> None:
        ctx: dict = {"preferred_lang": "ur", "lang": "en"}
        for _ in range(4):
            out = self._turn(ctx)
        self.assertEqual(out, "Hi!")
        self.assertFalse(ctx.get("lang_nudge_pending"))

    @patch("app.lang_pref._mark_nudged")
    @patch("app.lang_pref._nudge_allowed_by_cooldown", return_value=True)
    def test_ignored_offer_never_reasks_this_session(self, _cool, _mark) -> None:
        ctx: dict = {"preferred_lang": "ur", "lang": "en"}
        for _ in range(3):
            self._turn(ctx)
        self.assertEqual(ctx["lang_nudge_pending"], "en")
        out = self._turn(ctx)  # next turn doesn't accept → offer dropped for good
        self.assertEqual(out, "Hi!")
        self.assertIsNone(ctx["lang_nudge_pending"])
        self.assertTrue(ctx["lang_nudge_done"])
        for _ in range(5):
            out = self._turn(ctx)
        self.assertEqual(out, "Hi!")  # keeps diverging — still no second ask

    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    def test_accept_persists_and_confirms(self, save) -> None:
        msg = "yes please"
        ctx: dict = {
            "preferred_lang": "ur",
            "lang": "en",
            "lang_nudge_pending": "en",
            "_discovery_slots": {"set_preferred_lang": "en"},
            "_discovery_slots_for": msg,
        }
        out = self._turn(ctx, msg=msg)
        save.assert_called_once_with("u1", "en")
        self.assertIn(t("lang.pref_saved", "en", lang_name="English"), out)
        self.assertEqual(ctx["preferred_lang"], "en")
        self.assertIsNone(ctx["lang_nudge_pending"])
        self.assertTrue(ctx["lang_nudge_done"])

    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    def test_explicit_change_without_nudge(self, save) -> None:
        # "always talk to me in Spanish" — settings.change_language straight up.
        msg = "always talk to me in spanish"
        ctx: dict = {
            "lang": "es",
            "_discovery_slots": {"set_preferred_lang": "es"},
            "_discovery_slots_for": msg,
        }
        out = self._turn(ctx, msg=msg)
        save.assert_called_once_with("u1", "es")
        self.assertIn(t("lang.pref_saved", "es", lang_name="Spanish"), out)

    def test_guest_accept_flips_session_and_disarms_offer(self) -> None:
        # QA 2026-07-23: for anonymous signup chats an accepted language offer
        # was a full no-op (hook returned early), so the armed offer never
        # expired and every "sí, hablemos en español" got another ack forever.
        msg = "Sí, hablemos en español"
        ctx: dict = {
            "lang": "es",
            "lang_offer_langs": ["es"],
            "lang_offer_ttl": 3,
            "_discovery_slots": {"set_preferred_lang": "es"},
            "_discovery_slots_for": msg,
        }
        out = language_preference_post_turn(
            user_id="anon-1",
            user_message=msg,
            session_ctx=ctx,
            reply="¿Cuál es tu código postal?",
            is_anonymous=True,
        )
        # First accept: confirm PREPENDED, then the turn's own question re-anchors —
        # a silent accept made users repeat it and derailed the funnel (QA transcript #3).
        self.assertEqual(
            out,
            t("lang.guest_confirm", "es") + "\n\n¿Cuál es tu código postal?",
        )
        self.assertEqual(ctx["lang"], "es")
        self.assertEqual(ctx["preferred_lang"], "es")
        self.assertEqual(ctx["guest_locale"], "es")
        self.assertTrue(ctx["lang_nudge_done"])
        self.assertFalse(ctx["lang_offer_langs"])  # None-cleared (survives the merge as a delete)
        self.assertFalse(ctx["lang_offer_ttl"])

        # A REPEATED accept stays silent — no confirm spam, the funnel question
        # alone re-anchors.
        ctx["_discovery_slots"] = {"set_preferred_lang": "es"}
        ctx["_discovery_slots_for"] = msg
        out2 = language_preference_post_turn(
            user_id="anon-1",
            user_message=msg,
            session_ctx=ctx,
            reply="¿Cuál es tu código postal?",
            is_anonymous=True,
        )
        self.assertEqual(out2, "¿Cuál es tu código postal?")

    def test_guest_offer_ttl_expires(self) -> None:
        # The TTL decrement used to live behind the anonymous early-return, so
        # a guest's armed offer stayed armed forever.
        ctx: dict = {"lang_offer_langs": ["es"], "lang_offer_ttl": 2}
        language_preference_post_turn(
            user_id="anon-1", user_message="hola", session_ctx=ctx,
            reply="Hi!", is_anonymous=True,
        )
        self.assertEqual(ctx["lang_offer_ttl"], 1)
        language_preference_post_turn(
            user_id="anon-1", user_message="hola", session_ctx=ctx,
            reply="Hi!", is_anonymous=True,
        )
        self.assertFalse(ctx["lang_offer_langs"])  # None-cleared (survives the merge as a delete)
        self.assertFalse(ctx["lang_offer_ttl"])

    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    @patch("app.lang_pref.get_user_preferred_language", return_value=None)
    def test_guest_locale_inherited_after_signup(self, _get, save) -> None:
        # A language accepted as a guest survives signup in-session and lands
        # on the fresh account's users row.
        ctx: dict = {"guest_locale": "es", "lang": "es"}
        out = self._turn(ctx)
        self.assertEqual(out, "Hi!")
        save.assert_called_once_with("u1", "es")
        self.assertIsNone(ctx["guest_locale"])
        self.assertEqual(ctx["preferred_lang"], "es")

    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    @patch("app.lang_pref.get_user_preferred_language", return_value="ur")
    def test_guest_locale_never_overrides_chosen_pref(self, _get, save) -> None:
        ctx: dict = {"guest_locale": "es", "lang": "es"}
        self._turn(ctx)
        save.assert_not_called()
        self.assertIsNone(ctx["guest_locale"])

    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    def test_stale_slots_never_fire(self, save) -> None:
        # Slots cached from an EARLIER turn (different message) must not re-fire.
        ctx: dict = {
            "lang": "en",
            "_discovery_slots": {"set_preferred_lang": "es"},
            "_discovery_slots_for": "always talk to me in spanish",
        }
        out = self._turn(ctx, msg="what's on my block?")
        save.assert_not_called()
        self.assertEqual(out, "Hi!")


if __name__ == "__main__":
    unittest.main()
