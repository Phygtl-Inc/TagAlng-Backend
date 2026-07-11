"""Language mirroring — detection, session stickiness, canned strings, and the
no-spurious-event-draft guard (QA 2026-07-08: Brazilian moms got English)."""

import unittest

from app.i18n import (
    detect_language,
    resolve_session_lang,
    session_lang,
    synth_language_directive,
    t,
)

# The two production QA messages, verbatim.
QA_ES = "hola! busco otras mamás con niños pequeños cerca"
QA_PT = "oi Lana, sou brasileira, acabei de me mudar. quero conhecer outras mães"


class TestDetectLanguage(unittest.TestCase):
    def test_qa_spanish_message_detects_es(self) -> None:
        self.assertEqual(detect_language(QA_ES), "es")

    def test_qa_portuguese_message_detects_pt(self) -> None:
        self.assertEqual(detect_language(QA_PT), "pt")

    def test_english_message_detects_en(self) -> None:
        self.assertEqual(
            detect_language("hi Lana, I just moved here and want to meet other moms"),
            "en",
        )

    def test_ambiguous_short_messages_detect_nothing(self) -> None:
        for msg in ("32827", "ok", "Maria", "", None, "fix:kind"):
            self.assertIsNone(detect_language(msg), msg)

    def test_more_portuguese_variants(self) -> None:
        self.assertEqual(detect_language("não, obrigada"), "pt")
        self.assertEqual(detect_language("quero conhecer outras mães aqui perto"), "pt")

    def test_more_spanish_variants(self) -> None:
        self.assertEqual(detect_language("¿dónde encuentro otras mamás?"), "es")


class TestSessionStickiness(unittest.TestCase):
    def test_non_english_detection_persists_on_session(self) -> None:
        ctx: dict = {}
        self.assertEqual(resolve_session_lang(ctx, QA_PT), "pt")
        self.assertEqual(ctx.get("lang"), "pt")

    def test_ambiguous_turn_keeps_sticky_language(self) -> None:
        ctx: dict = {}
        resolve_session_lang(ctx, QA_PT)
        # A bare ZIP answer (or any ambiguous turn) must not flip the language.
        self.assertEqual(resolve_session_lang(ctx, "32827"), "pt")
        self.assertEqual(ctx.get("lang"), "pt")
        # Even a short borrowed-English line keeps the session in Portuguese.
        self.assertEqual(resolve_session_lang(ctx, "ok thanks"), "pt")
        self.assertEqual(ctx.get("lang"), "pt")

    def test_in_english_please_flips_back(self) -> None:
        ctx: dict = {}
        resolve_session_lang(ctx, QA_PT)
        self.assertEqual(resolve_session_lang(ctx, "in english please"), "en")
        self.assertEqual(ctx.get("lang"), "en")
        self.assertIsNone(session_lang(ctx))  # en → no mirroring directive

    def test_explicit_language_requests(self) -> None:
        ctx: dict = {}
        self.assertEqual(resolve_session_lang(ctx, "em português, por favor"), "pt")
        self.assertEqual(resolve_session_lang(ctx, "en español por favor"), "es")
        self.assertEqual(ctx.get("lang"), "es")

    def test_session_lang_reports_any_non_english_code(self) -> None:
        # Any ISO-shaped code counts (AI renders the reply); only EN/garbage → None.
        self.assertEqual(session_lang({"lang": "pt"}), "pt")
        self.assertEqual(session_lang({"lang": "es"}), "es")
        self.assertEqual(session_lang({"lang": "ur"}), "ur")
        self.assertEqual(session_lang({"lang": "ht"}), "ht")
        self.assertIsNone(session_lang({"lang": "en"}))
        self.assertIsNone(session_lang({"lang": "not a code"}))
        self.assertIsNone(session_lang({}))
        self.assertIsNone(session_lang(None))


class TestCannedStrings(unittest.TestCase):
    def test_unknown_lang_falls_back_to_english(self) -> None:
        self.assertEqual(
            t("browse.ask_interest", "de"),
            "Love it — what kind of thing are you up for?",
        )
        self.assertEqual(
            t("browse.ask_interest", None),
            "Love it — what kind of thing are you up for?",
        )

    def test_unknown_key_returns_key_never_raises(self) -> None:
        self.assertEqual(t("nope.not_a_key", "es"), "nope.not_a_key")

    def test_localized_strings_exist_for_qa_surfaces(self) -> None:
        # Category ask, ZIP ask, empty state, verify gate — es AND pt all differ from EN.
        for key in (
            "browse.ask_interest",
            "meet.ask_kind",
            "discovery.ask_zip_peers",
            "discovery.ask_zip_short",
            "discovery.activities_header",
            "discovery.activities_empty",
            "discovery.verify_gate_neighbors",
            "meet.verify_gate",
            "browse.zip_no_block",
        ):
            en = t(key, "en")
            self.assertNotEqual(t(key, "es"), en, key)
            self.assertNotEqual(t(key, "pt"), en, key)

    def test_format_args_and_missing_args_are_safe(self) -> None:
        self.assertIn("99999", t("discovery.zip_unplaceable", "es", zip="99999"))
        # Missing kwargs never raise mid-turn — the unformatted string comes back.
        self.assertTrue(t("discovery.zip_unplaceable", "pt"))

    def test_en_strings_match_previous_literals(self) -> None:
        # Behavior-preserving for English sessions (the exact strings tests/QA knew).
        self.assertEqual(
            t("discovery.ask_zip_peers", "en"),
            "What ZIP code is your block? That helps me find neighbors near you.",
        )
        self.assertEqual(
            t("meet.ask_kind", "en"), "Love it — what kind of meet would help?"
        )


class TestSynthDirective(unittest.TestCase):
    def test_directive_for_es_and_pt_names_language_and_register(self) -> None:
        es = synth_language_directive("es") or ""
        pt = synth_language_directive("pt") or ""
        self.assertIn("Spanish", es)
        self.assertIn("tú", es)
        self.assertIn("Brazilian Portuguese", pt)
        self.assertIn("você", pt)
        for d in (es, pt):
            self.assertIn("event titles", d)  # titles stay as authored

    def test_no_directive_for_english(self) -> None:
        self.assertIsNone(synth_language_directive("en"))
        self.assertIsNone(synth_language_directive(""))

    def test_lana_synth_fallbacks_localized(self) -> None:
        from app.orchestrator.synthesizer import _parse_lana_synth

        msg, _, _, _, _ = _parse_lana_synth(
            {}, routing={"enforce_notes": ["discovery_need_zip"]}, tool_result=None, lang="pt"
        )
        self.assertEqual(msg, t("discovery.ask_zip_short", "pt"))
        msg_en, _, _, _, _ = _parse_lana_synth(
            {}, routing={"enforce_notes": ["discovery_need_zip"]}, tool_result=None
        )
        self.assertEqual(msg_en, "What ZIP code is your block? (e.g. 32827)")


class TestNoSpuriousEventDraft(unittest.TestCase):
    """The PT greeting must never spawn an event draft (the QA bug: an empty
    event_draft appeared because non-English text confused extraction)."""

    def _should_extract(self, utterance: str, *, session_ctx=None, routing=None) -> bool:
        from app.orchestrator.pipeline import _should_reconcile_event_turn

        return _should_reconcile_event_turn(
            purpose="lana",
            utterance=utterance,
            session_ctx=session_ctx or {},
            routing=routing or {},
            tool_result=None,
            prev_draft=None,
        )

    def test_pt_greeting_fixture_never_triggers_extraction(self) -> None:
        # Even when the router mis-guesses hosting for the PT text (the QA failure),
        # extraction must not fire — no host state, no explicit PT hosting phrase.
        self.assertFalse(
            self._should_extract(
                QA_PT,
                routing={"intent_class": "activity", "tool_to_call": "update_event_draft"},
            )
        )
        self.assertFalse(self._should_extract(QA_PT))

    def test_es_greeting_never_triggers_extraction(self) -> None:
        self.assertFalse(
            self._should_extract(
                QA_ES,
                routing={"intent_class": "activity", "tool_to_call": "update_event_draft"},
            )
        )

    def test_explicit_pt_hosting_phrase_still_extracts(self) -> None:
        self.assertTrue(
            self._should_extract(
                "quero organizar um café para outras mães no sábado",
                routing={"intent_class": "activity", "tool_to_call": "update_event_draft"},
            )
        )

    def test_english_hosting_still_extracts(self) -> None:
        self.assertTrue(self._should_extract("I want to host a coffee meetup on Saturday"))

    def test_active_host_mode_still_extracts_regardless_of_language(self) -> None:
        # Mid-flow answers in PT ("no parque, sábado de manhã") keep accumulating.
        self.assertTrue(
            self._should_extract(
                "no parque, sábado de manhã", session_ctx={"event_host_active": True}
            )
        )


class TestLocalizedFunnelTurns(unittest.TestCase):
    """The deterministic flows answer in the session language (QA surfaces)."""

    def test_browse_category_ask_in_spanish(self) -> None:
        # An empty entry (generic CTA seed) asks what they're up for — in ES.
        from app.activity_browse import run_activity_browse_turn

        ctx: dict = {"lang": "es"}
        reply = run_activity_browse_turn(
            user_message="",
            session_ctx=ctx,
            history=[],
            user_jwt="",
            home_block_id=None,
        )
        self.assertEqual(reply, t("browse.ask_interest", "es"))

    def test_browse_zip_ask_in_spanish(self) -> None:
        # A concrete ES ask carries the interest, so Lana skips to the ZIP ask —
        # localized fallback when no LLM is configured.
        from app.activity_browse import run_activity_browse_turn

        ctx: dict = {"lang": "es"}
        reply = run_activity_browse_turn(
            user_message=QA_ES,
            session_ctx=ctx,
            history=[],
            user_jwt="",
            home_block_id=None,
        )
        self.assertEqual(reply, t("browse.ask_zip", "es"))

    def test_look_meet_kind_ask_in_portuguese(self) -> None:
        from app.look_meet import run_look_meet_turn

        ctx: dict = {"lang": "pt"}
        reply = run_look_meet_turn(
            user_message="",
            session_ctx=ctx,
            history=[],
            user_jwt="",
            home_block_id=None,
        )
        self.assertEqual(reply, t("meet.ask_kind", "pt"))

    def test_activities_message_localized_but_titles_as_authored(self) -> None:
        from app.discovery_route import format_activities_message

        # Events render as FE cards under the message — the text is a short
        # localized lead-in only; titles stay as authored in the cards.
        events = [{"title": "FIFA watch party", "venue_name": "Boxi Park", "starts_at": None}]
        msg = format_activities_message(events, "Lake Nona", phone_verified=False, lang="pt")
        self.assertIn(t("discovery.activities_header", "pt", where="Lake Nona"), msg)
        self.assertNotIn("FIFA watch party", msg)  # cards carry the events, not the text
        self.assertIn(t("discovery.activities_tail_guest", "pt"), msg)

    def test_activities_message_english_unchanged(self) -> None:
        from app.discovery_route import format_activities_message

        msg = format_activities_message([], "Lake Nona", phone_verified=True)
        self.assertEqual(
            msg,
            "I don't see open activities on Lake Nona in the next couple weeks yet. "
            "You can host something, or tell me what you're looking for.",
        )


if __name__ == "__main__":
    unittest.main()
