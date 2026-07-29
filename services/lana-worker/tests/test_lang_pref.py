"""Language preference: seeding, AI lang verdicts, and the divergence nudge.

No LLM is configured under test, so every AI-composed line falls back to the
t() strings deterministically — the logic under test is the state machine.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

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


class TestLanguageClaim(unittest.TestCase):
    """Accepting a language as default is a statement the user SPEAKS it — the hook
    must remember it as an identity claim, not just flip the locale."""

    def _turn(self, ctx: dict, *, msg: str = "hello there", reply: str = "Hi!") -> str:
        return language_preference_post_turn(
            user_id="u1",
            user_message=msg,
            session_ctx=ctx,
            reply=reply,
            is_anonymous=False,
        )

    @patch("app.lang_pref.remember_language_claim_async")
    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    def test_accept_kicks_language_claim(self, _save, remember) -> None:
        msg = "talk to me in german from now on"
        ctx: dict = {
            "lang": "de",
            "_discovery_slots": {"set_preferred_lang": "de"},
            "_discovery_slots_for": msg,
        }
        self._turn(ctx, msg=msg)
        remember.assert_called_once_with("u1", "de", msg)

    @patch("app.lang_pref.remember_language_claim_async")
    @patch("app.lang_pref.set_user_preferred_language", return_value=False)
    def test_failed_locale_save_kicks_no_claim(self, _save, remember) -> None:
        msg = "talk to me in german from now on"
        ctx: dict = {
            "lang": "de",
            "_discovery_slots": {"set_preferred_lang": "de"},
            "_discovery_slots_for": msg,
        }
        self._turn(ctx, msg=msg)
        remember.assert_not_called()

    @patch("app.lang_pref.remember_language_claim_async")
    @patch("app.lang_pref.set_user_preferred_language", return_value=True)
    @patch("app.lang_pref.get_user_preferred_language", return_value=None)
    def test_guest_carryover_kicks_language_claim(self, _get, _save, remember) -> None:
        ctx: dict = {"guest_locale": "es", "lang": "es"}
        self._turn(ctx)
        remember.assert_called_once_with("u1", "es")

    @patch("app.lang_pref.remember_language_claim_async")
    @patch("app.lang_pref.get_user_preferred_language", return_value="ur")
    def test_guest_carryover_claim_even_when_locale_kept(self, _get, remember) -> None:
        # A self-chosen pref blocks the locale overwrite, but "I speak Spanish"
        # is still true — the claim lands regardless.
        ctx: dict = {"guest_locale": "es", "lang": "es"}
        self._turn(ctx)
        remember.assert_called_once_with("u1", "es")

    @patch("app.lang_pref.remember_language_claim_async")
    def test_anonymous_accept_kicks_no_claim(self, remember) -> None:
        # Guests have no users row — the claim waits for the post-signup carry-over.
        msg = "Sí, hablemos en español"
        ctx: dict = {
            "lang": "es",
            "_discovery_slots": {"set_preferred_lang": "es"},
            "_discovery_slots_for": msg,
        }
        language_preference_post_turn(
            user_id="anon-1", user_message=msg, session_ctx=ctx,
            reply="Hi!", is_anonymous=True,
        )
        remember.assert_not_called()

    def test_async_wrapper_skips_english_and_missing_user(self) -> None:
        with patch("app.lang_pref.threading.Thread") as thread:
            from app.lang_pref import remember_language_claim_async

            remember_language_claim_async("u1", "en", "english is fine")
            remember_language_claim_async("", "de", None)
            thread.assert_not_called()


class TestLanguageObservationCounting(unittest.TestCase):
    """Merely WRITING in a language ramps a low-surety claim: the post-turn hook
    counts non-English turns per session and kicks the async observation write
    after 2 turns, then every 4 more."""

    def _turn(self, ctx: dict, *, msg: str = "hallo zusammen") -> str:
        return language_preference_post_turn(
            user_id="u1",
            user_message=msg,
            session_ctx=ctx,
            reply="Hi!",
            is_anonymous=False,
        )

    @patch("app.lang_pref.record_language_observation_async")
    def test_first_write_after_two_turns_then_every_four(self, record) -> None:
        ctx: dict = {"lang": "de"}
        self._turn(ctx)
        record.assert_not_called()  # one message could be a one-off paste
        self._turn(ctx)
        record.assert_called_once_with("u1", "de", "hallo zusammen")
        for _ in range(3):  # turns 3-5: no re-corroboration yet
            self._turn(ctx)
        record.assert_called_once()
        self._turn(ctx)  # turn 6 = 2 + 4 — corroborate again
        self.assertEqual(record.call_count, 2)
        self.assertEqual(ctx["lang_obs_counts"], {"de": 6})

    @patch("app.lang_pref.record_language_observation_async")
    def test_counts_are_per_language(self, record) -> None:
        ctx: dict = {"lang": "de"}
        self._turn(ctx)
        ctx["lang"] = "es"
        self._turn(ctx, msg="hola")
        record.assert_not_called()  # neither language reached two turns
        self.assertEqual(ctx["lang_obs_counts"], {"de": 1, "es": 1})

    @patch("app.lang_pref.record_language_observation_async")
    def test_english_turns_never_count(self, record) -> None:
        ctx: dict = {"lang": "en"}
        for _ in range(6):
            self._turn(ctx, msg="hello there")
        record.assert_not_called()
        self.assertNotIn("lang_obs_counts", ctx)

    @patch("app.lang_pref.record_language_observation_async")
    def test_anonymous_turns_never_count(self, record) -> None:
        ctx: dict = {"lang": "de"}
        for _ in range(3):
            language_preference_post_turn(
                user_id="anon-1",
                user_message="hallo",
                session_ctx=ctx,
                reply="Hi!",
                is_anonymous=True,
            )
        record.assert_not_called()

    @patch("app.lang_pref.record_language_observation_async")
    def test_flag_off_disables_observation(self, record) -> None:
        ctx: dict = {"lang": "de"}
        with patch.dict("os.environ", {"LANA_LANG_OBSERVED_CLAIMS": "off"}):
            for _ in range(3):
                self._turn(ctx)
        record.assert_not_called()


class TestRecordLanguageObservation(unittest.TestCase):
    """The observation write itself: create at the persist floor, re-corroborate
    (upsert bumps), graduate onto the confirmed thread, self-heal duplicates."""

    def _rows(self, rows: list[dict]) -> object:
        sb = MagicMock()
        result = MagicMock()
        result.data = rows
        (
            sb.table.return_value.select.return_value.eq.return_value
            .is_.return_value.limit.return_value.execute.return_value
        ) = result
        return sb

    def test_creates_observed_row_at_persist_floor(self) -> None:
        from app.claims_persist import MIN_CLAIM_CONFIDENCE
        from app.lang_pref import _record_language_observation

        with patch("app.lang_pref.service_client", return_value=self._rows([])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _record_language_observation("u1", "de", "hallo zusammen")
        upsert.assert_called_once()
        claim = upsert.call_args[0][1][0]
        self.assertEqual(claim.concept, "lang_observed_de")
        self.assertEqual(claim.label, "Speaks German")
        self.assertEqual(claim.confidence, MIN_CLAIM_CONFIDENCE)
        self.assertEqual(claim.synonyms, ["German"])
        self.assertEqual(claim.source_quote, "hallo zusammen")

    def test_recorroboration_upserts_existing_row(self) -> None:
        # upsert_claims merges by concept: max(old, new) + bump — the ramp.
        from app.lang_pref import _record_language_observation

        existing = {
            "concept": "lang_observed_de",
            "label": "Speaks German",
            "synonyms": ["German"],
            "details": [],
            "confidence": 0.7,
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([existing])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _record_language_observation("u1", "de", None)
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args[0][1][0].concept, "lang_observed_de")

    @patch("app.lang_pref._remember_language_claim")
    def test_graduates_to_confirmed_thread_when_sure(self, remember) -> None:
        from app.lang_pref import _record_language_observation

        existing = {
            "concept": "lang_observed_de",
            "label": "Speaks German",
            "synonyms": ["German"],
            "details": [],
            "confidence": 0.85,  # + bump crosses the graduation bar
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([existing])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _record_language_observation("u1", "de", "noch eine nachricht")
        remember.assert_called_once_with("u1", "de", "noch eine nachricht")
        upsert.assert_not_called()

    @patch("app.lang_pref._dismiss_observed_language_row")
    def test_language_already_proven_dismisses_and_skips(self, dismiss) -> None:
        from app.lang_pref import _record_language_observation

        confirmed = {
            "concept": "multilingual",
            "label": "Speaks Urdu, English, and German",
            "synonyms": [],
            "details": [],
            "confidence": 1.0,
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([confirmed])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _record_language_observation("u1", "de", None)
        upsert.assert_not_called()
        dismiss.assert_called_once_with("u1", "de")

    def test_skips_english(self) -> None:
        from app.lang_pref import _record_language_observation

        with patch("app.lang_pref.service_client") as sb:
            _record_language_observation("u1", "en", None)
        sb.assert_not_called()


class TestRememberLanguageClaimWrite(unittest.TestCase):
    """The deterministic claim write: enrich the extractor's languages thread, never
    clobber its label, no-op on a language already recorded."""

    def _rows(self, rows: list[dict]) -> object:
        sb = MagicMock()
        result = MagicMock()
        result.data = rows
        (
            sb.table.return_value.select.return_value.eq.return_value
            .is_.return_value.limit.return_value.execute.return_value
        ) = result
        return sb

    def test_enriches_existing_thread_keeping_label(self) -> None:
        from app.lang_pref import _remember_language_claim

        existing = {
            "concept": "multilingual",
            "label": "Speaks Urdu and English",
            "synonyms": ["urdu", "english"],
            "details": [],
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([existing])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _remember_language_claim("u1", "de", "talk to me in german from now on")
        upsert.assert_called_once()
        claim = upsert.call_args[0][1][0]
        self.assertEqual(claim.concept, "multilingual")
        self.assertEqual(claim.label, "Speaks Urdu and English")  # never clobbered
        self.assertEqual(claim.synonyms, ["German"])
        self.assertEqual(claim.details, ["Speaks German"])

    def test_noop_when_language_already_on_thread(self) -> None:
        from app.lang_pref import _remember_language_claim

        existing = {
            "concept": "multilingual",
            "label": "Speaks Urdu, English, and German",
            "synonyms": [],
            "details": [],
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([existing])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _remember_language_claim("u1", "de", None)
        upsert.assert_not_called()

    def test_creates_thread_when_none_exists(self) -> None:
        from app.lang_pref import _remember_language_claim

        unrelated = {"concept": "badminton", "label": "Badminton player",
                     "synonyms": [], "details": []}
        with patch("app.lang_pref.service_client", return_value=self._rows([unrelated])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _remember_language_claim("u1", "de", None)
        claim = upsert.call_args[0][1][0]
        self.assertEqual(claim.concept, "multilingual")
        self.assertEqual(claim.label, "Speaks German")
        self.assertEqual(claim.details, [])

    def test_skips_english(self) -> None:
        from app.lang_pref import _remember_language_claim

        with patch("app.lang_pref.service_client") as sb:
            _remember_language_claim("u1", "en", None)
        sb.assert_not_called()

    @patch("app.lang_pref._dismiss_observed_language_row")
    def test_observed_row_is_not_the_thread_and_gets_retired(self, dismiss) -> None:
        # An explicit accept of URDU must not enrich the watch-and-learn German
        # row (its label matches the thread-hint regex) — and an explicit accept
        # of GERMAN retires that row after promoting the claim.
        from app.lang_pref import _remember_language_claim

        observed = {
            "concept": "lang_observed_de",
            "label": "Speaks German",
            "synonyms": ["German"],
            "details": [],
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([observed])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _remember_language_claim("u1", "ur", None)
        claim = upsert.call_args[0][1][0]
        self.assertEqual(claim.concept, "multilingual")  # fresh thread, not the observed row
        self.assertEqual(claim.label, "Speaks Urdu")
        dismiss.assert_called_once_with("u1", "ur")

    @patch("app.lang_pref._dismiss_observed_language_row")
    def test_already_confirmed_language_still_retires_observed_row(self, dismiss) -> None:
        from app.lang_pref import _remember_language_claim

        confirmed = {
            "concept": "multilingual",
            "label": "Speaks Urdu, English, and German",
            "synonyms": [],
            "details": [],
        }
        with patch("app.lang_pref.service_client", return_value=self._rows([confirmed])), \
                patch("app.claims_persist.upsert_claims") as upsert:
            _remember_language_claim("u1", "de", None)
        upsert.assert_not_called()
        dismiss.assert_called_once_with("u1", "de")


if __name__ == "__main__":
    unittest.main()
