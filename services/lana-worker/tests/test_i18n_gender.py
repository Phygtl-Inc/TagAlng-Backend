"""Grammatical-gender agreement on the AI render path (eval 2026-09-01, B1).

Two distinct defects, both live before this suite existed:

1. CROSS-USER CACHE LEAK. ``_AI_RENDER_CACHE`` was keyed ``(text, lang)``, so the
   first user to render a phrase fixed its gender agreement for every later user
   in the process — a user whose gender was known masculine was served a feminine
   user's cached "¡Bienvenida a la zona!" (``lt_gender_es_known_masculine``).
   These tests therefore WARM the cache and only then check the second user: a
   single render can never catch an order-dependent leak, which is exactly why the
   eval saw one language fail on run 1 and two on run 2.

2. UNKNOWN GENDER DEFAULTED FEMININE. The render prompt's only gender rule was
   "when the English is gender-neutral, stay gender-neutral — rephrase rather than
   pick a gendered form", which cannot be obeyed for bienvenido/bienvenida. The
   model sampled, and es/pt lean feminine in a warm welcoming register
   (``lt_gender_es_unknown_neutral``). A unit test can only assert the instruction
   now exists and names the fallback; the eval owns the behavioral check.
"""

import unittest

from app import i18n
from app.context import address_guidance, set_address_context, user_gram_gender

WELCOME = "Welcome to the area!"


class TestRenderCacheKey(unittest.TestCase):
    """The key must carry gender — this is the whole fix for defect 1."""

    def setUp(self) -> None:
        set_address_context(None, None)

    def tearDown(self) -> None:
        set_address_context(None, None)
        i18n._AI_RENDER_CACHE.clear()

    def test_key_includes_gender(self) -> None:
        set_address_context(None, "masculine")
        self.assertEqual(i18n._render_key(WELCOME, "es"), (WELCOME, "es", "masculine"))

    def test_key_differs_per_gender_for_same_text(self) -> None:
        keys = set()
        for gender in ("masculine", "feminine", "neutral", None):
            set_address_context(None, gender)
            keys.add(i18n._render_key(WELCOME, "es"))
        self.assertEqual(len(keys), 4, "genders must not collide on one cache entry")

    def test_unknown_gender_keys_empty_not_missing(self) -> None:
        set_address_context(None, None)
        self.assertEqual(i18n._render_key(WELCOME, "es"), (WELCOME, "es", ""))
        self.assertEqual(user_gram_gender(), "")


class TestNoCrossUserGenderLeak(unittest.TestCase):
    """The regression proper: render as one user, then another, same process.

    ``_ai_render`` is stubbed because the real one needs an LLM; the stub returns a
    gender-dependent string so a cache hit across users is directly visible.
    """

    def setUp(self) -> None:
        self._real = i18n._ai_render
        self.calls: list[str] = []

        def fake_render(en_text: str, lang: str) -> str:
            gender = user_gram_gender() or "unknown"
            self.calls.append(gender)
            return {
                "masculine": "¡Bienvenido a la zona!",
                "feminine": "¡Bienvenida a la zona!",
            }.get(gender, "¡Qué bueno tenerte por aquí!")

        i18n._ai_render = fake_render
        i18n._AI_RENDER_CACHE.clear()
        set_address_context(None, None)

    def tearDown(self) -> None:
        i18n._ai_render = self._real
        i18n._AI_RENDER_CACHE.clear()
        set_address_context(None, None)

    def test_masculine_user_is_not_served_a_feminine_cached_render(self) -> None:
        # Warm the cache as a feminine user FIRST — the poisoning step.
        set_address_context(None, "feminine")
        fem = i18n.localize_text(WELCOME, "es")
        self.assertEqual(fem, "¡Bienvenida a la zona!")

        # Now the same English string for a KNOWN-MASCULINE user.
        set_address_context(None, "masculine")
        masc = i18n.localize_text(WELCOME, "es")

        self.assertEqual(masc, "¡Bienvenido a la zona!")
        self.assertNotEqual(masc, fem, "masculine user served the feminine cached render")
        self.assertEqual(self.calls, ["feminine", "masculine"], "second user must re-render")

    def test_unknown_gender_user_is_not_served_a_gendered_cached_render(self) -> None:
        set_address_context(None, "feminine")
        i18n.localize_text(WELCOME, "es")
        set_address_context(None, None)
        unknown = i18n.localize_text(WELCOME, "es")
        self.assertEqual(unknown, "¡Qué bueno tenerte por aquí!")

    def test_same_user_still_hits_the_cache(self) -> None:
        """The fix must not cost the cache: one LLM call per (text, lang, gender)."""
        set_address_context(None, "masculine")
        first = i18n.localize_text(WELCOME, "es")
        second = i18n.localize_text(WELCOME, "es")
        self.assertEqual(first, second)
        self.assertEqual(self.calls, ["masculine"], "a repeat render must be cached")

    def test_labels_do_not_leak_gender_either(self) -> None:
        """localize_labels shared the same (label, lang) key shape. Chips are in the
        USER's voice ("Estoy listo" / "Estoy lista"), so they need agreement too."""
        set_address_context(None, "feminine")
        i18n._AI_RENDER_CACHE[i18n._render_key("I'm ready", "es")] = "Estoy lista"
        set_address_context(None, "masculine")
        out = i18n.localize_labels(["I'm ready"], "es")
        # No LLM in tests → a miss returns the label unchanged. The point is that it
        # must NOT return the feminine user's cached chip.
        self.assertNotEqual(out[0], "Estoy lista")


class TestAddressGuidance(unittest.TestCase):
    """Defect 2 is a prompt defect: assert the instruction exists and is specific."""

    def tearDown(self) -> None:
        set_address_context(None, None)

    def test_unknown_gender_forbids_the_feminine_default(self) -> None:
        set_address_context(None, None)
        guidance = address_guidance()
        self.assertIn("UNKNOWN", guidance)
        # The eval's exact failure was defaulting feminine, so the ban must be explicit.
        self.assertIn("NEVER default to the feminine form", guidance)
        # Rephrasing comes first; masculine is only the forced-choice fallback.
        self.assertIn("MASCULINE", guidance)

    def test_unknown_gender_never_emits_an_empty_guidance(self) -> None:
        """The old code returned "" for unknown gender, which is what left the
        composer with no rule at all."""
        set_address_context(None, None)
        self.assertTrue(address_guidance().strip())

    def test_known_gender_states_the_agreement(self) -> None:
        for gender, expect in (("masculine", "masculino"), ("feminine", "femenino")):
            set_address_context(None, gender)
            self.assertIn(expect, address_guidance(), gender)

    def test_neutral_forbids_gendered_forms_outright(self) -> None:
        set_address_context(None, "neutral")
        guidance = address_guidance()
        self.assertIn("neutral", guidance)
        self.assertIn("NEVER use a gendered form", guidance)
        # A stated they/them must not fall through to the unknown-gender rule,
        # which is allowed to pick masculine.
        self.assertNotIn("NEVER default to the feminine form", guidance)

    def test_role_and_gender_both_land(self) -> None:
        set_address_context("parent", "feminine")
        guidance = address_guidance()
        self.assertIn("household role: parent", guidance)
        self.assertIn("grammatical gender: feminine", guidance)


if __name__ == "__main__":
    unittest.main()


class TestPronounReadback(unittest.TestCase):
    """"What are my pronouns?" must be answerable from the stored value.

    grammatical_gender is a CONJUGATION axis, documented never-shown, and reached
    prompts only as agreement guidance ("conjugate to agree") — decide_turn even
    receives world.user.grammatical_gender with no rule mentioning it. So a user who
    had just set their pronouns was told "I don't know your pronouns yet"
    (prod 2026-09-04): the model held the fact and had no permission to state it.
    Never-shown protects the value from OTHER users, not from its owner.
    """

    def tearDown(self) -> None:
        set_address_context(None, None)

    def test_known_gender_carries_the_english_pronoun(self) -> None:
        for gender, pronoun in (
            ("feminine", "she/her"),
            ("masculine", "he/him"),
            ("neutral", "they/them"),
        ):
            set_address_context(None, gender)
            self.assertIn(pronoun, address_guidance(), gender)

    def test_known_gender_permits_answering_when_asked(self) -> None:
        set_address_context(None, "feminine")
        guidance = address_guidance()
        self.assertIn("what are my pronouns?", guidance)
        self.assertIn("answer plainly", guidance)

    def test_known_gender_still_forbids_telling_anyone_else(self) -> None:
        """The readback permission must not widen never-shown into shown."""
        set_address_context(None, "masculine")
        guidance = address_guidance()
        self.assertIn("never state it to any OTHER user", guidance)
        self.assertIn("Do NOT volunteer it unprompted", guidance)

    def test_unknown_gender_asks_with_storable_wording(self) -> None:
        """Lana improvised she/her · he/him · they/them chips that posted text the
        extractor had no rule for, so tapping one saved nothing. The ask now names
        the exact wording that persists."""
        set_address_context(None, None)
        guidance = address_guidance()
        self.assertIn("don't have it yet", guidance)
        for chip in ("she/her", "he/him", "they/them"):
            self.assertIn(chip, guidance, chip)
        self.assertIn("Never guess", guidance)

    def test_unknown_gender_does_not_claim_to_know(self) -> None:
        set_address_context(None, None)
        guidance = address_guidance()
        self.assertNotIn("Their pronouns are", guidance)


class TestExtractorRecognisesOfferedChips(unittest.TestCase):
    """Whatever Lana offers as a tappable answer must be something the extractor
    can store — otherwise the offer is a dead end (see the action-chips rule)."""

    def test_bare_pronoun_sets_are_named_in_the_stated_rule(self) -> None:
        from app.vertex_extract import INCREMENTAL_EXTRACT_PROMPT as prompt
        for chip in ('"she/her"', '"he/him"', '"they/them"'):
            self.assertIn(chip, prompt, chip)

    def test_save_my_pronouns_phrasing_is_covered(self) -> None:
        from app.vertex_extract import INCREMENTAL_EXTRACT_PROMPT as prompt
        self.assertIn("save my pronouns as she", prompt)

    def test_names_are_still_excluded_as_a_source(self) -> None:
        from app.vertex_extract import INCREMENTAL_EXTRACT_PROMPT as prompt
        self.assertIn("NEVER guess it from their NAME", prompt)
