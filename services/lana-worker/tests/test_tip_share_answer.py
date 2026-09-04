"""The share capture must survive its own follow-up questions.

Dev QA 2026-08-05: "dr mitchel is a great doctor" opened the share capture, Lana asked
"What type of doctor is Dr. Mitchel?", and the answer — "family doctor" — released the
lane and came back as a recommendation SEARCH: Google listings and a "YOUR ASK: Family
doctor" card, the inverse of what the user said. Three things had to line up for that:

  1. the router's state line said active_capture=none (no tip_share arm), so a bare
     category fragment was judged on its words alone -> tip_seek;
  2. the lane released on that verdict and threw the Dr. Mitchel draft away;
  3. `utterance_indicates_tip_seek` matched the bare service noun, so the released turn
     was guaranteed to land in the seek answer path.
"""

from __future__ import annotations

import unittest
from unittest import mock


class TestShareCaptureContextLine(unittest.TestCase):
    """The router must be TOLD a share is in flight, and which question is open."""

    def _capture(self, ctx: dict) -> str:
        from app.discovery_slots import _active_capture_context

        return _active_capture_context(ctx)

    def test_inert_when_no_share_is_active(self) -> None:
        self.assertEqual(self._capture({}), "none")
        self.assertEqual(self._capture({"tip_share_active": None}), "none")

    def test_names_the_pending_question_and_the_provider(self) -> None:
        line = self._capture(
            {
                "tip_share_active": True,
                "tip_pending_question": "What type of doctor is Dr. Mitchel?",
                "tip_draft": {"name": "Dr. Mitchel", "category": "doctor"},
            }
        )
        self.assertTrue(line.startswith("tip_share"))
        self.assertIn("What type of doctor is Dr. Mitchel?", line)
        self.assertIn("Dr. Mitchel", line)
        self.assertIn("NEVER tip_seek", line)
        self.assertIn("goal=save_signal", line)

    def test_bare_answer_is_covered_without_a_stored_question(self) -> None:
        line = self._capture({"tip_share_active": True})
        self.assertTrue(line.startswith("tip_share"))
        self.assertIn("NEVER tip_seek", line)

    def test_armed_offer_still_outranks_the_share(self) -> None:
        # The ask-neighbors offer writes on accept, so it keeps priority (2026-08-04 fix).
        line = self._capture(
            {"tip_ask_offer_pending": {"detail": "good doctor"}, "tip_share_active": True}
        )
        self.assertTrue(line.startswith("offer_reply"))

    def test_state_line_reaches_the_payload(self) -> None:
        from app.discovery_slots import _discovery_slot_payload

        payload = _discovery_slot_payload(
            "family doctor",
            routing_phase="listening",
            history=[{"role": "assistant", "content": "What type of doctor is Dr. Mitchel?"}],
            has_block=True,
            has_identity=True,
            phone_verified=True,
            session_ctx={
                "tip_share_active": True,
                "tip_pending_question": "What type of doctor is Dr. Mitchel?",
                "tip_draft": {"name": "Dr. Mitchel"},
            },
        )
        self.assertIn("active_capture: tip_share", payload)
        self.assertIn("NEVER tip_seek", payload)


_SEEK_SLOTS = {
    "goal": "save_signal",
    "signal_intent": "tip_seek",
    "linear_intent": "looking.tip",
    "confidence": 0.9,
}


class TestOfferedOptionNeverReleases(unittest.TestCase):
    """Picking one of Lana's OWN options is an answer, whatever the words look like alone."""

    def _ctx(self) -> dict:
        return {
            "tip_share_active": True,
            "tip_pending_ask": "doctor_type",
            "tip_pending_question": "What type of doctor is Dr. Mitchel?",
            "tip_draft": {
                "name": "Dr. Mitchel",
                "category": "doctor",
                "suggestions": ["Family doctor", "Pediatrician"],
            },
        }

    def test_offered_option_keeps_the_lane_despite_a_seek_verdict(self) -> None:
        from app.tip_share import tip_share_should_release

        self.assertFalse(tip_share_should_release("family doctor", self._ctx(), _SEEK_SLOTS))

    def test_match_ignores_case_and_trailing_punctuation(self) -> None:
        from app.tip_share import tip_share_should_release

        self.assertFalse(tip_share_should_release("Pediatrician.", self._ctx(), _SEEK_SLOTS))

    def test_a_real_pivot_still_releases(self) -> None:
        from app.tip_share import tip_share_should_release

        self.assertTrue(
            tip_share_should_release("actually, can you find me a dentist?", self._ctx(), _SEEK_SLOTS)
        )

    def test_free_text_answer_still_defers_to_the_classifier(self) -> None:
        # Not an offered option -> the AI keeps ownership of the read (that is what the
        # new state line is for). A confident foreign verdict releases, as before.
        from app.tip_share import tip_share_should_release

        self.assertTrue(tip_share_should_release("a doctor for grown ups", self._ctx(), _SEEK_SLOTS))

    def test_abandon_wins_over_an_offered_option(self) -> None:
        # No trapping: an abandon read releases even on an exact option match.
        from app.tip_share import tip_share_should_release

        slots = {**_SEEK_SLOTS, "abandon": True}
        self.assertTrue(tip_share_should_release("family doctor", self._ctx(), slots))

    def test_no_suggestions_means_no_override(self) -> None:
        from app.tip_share import tip_share_should_release

        ctx = self._ctx()
        ctx["tip_draft"] = {"name": "Dr. Mitchel", "category": "doctor"}
        self.assertTrue(tip_share_should_release("family doctor", ctx, _SEEK_SLOTS))


class TestPendingQuestionIsRecorded(unittest.TestCase):
    """The lane stores what it asked, so next turn's router can see it."""

    def test_tailored_followup_is_stored_then_cleared(self) -> None:
        from app.tip_share import run_tip_share_turn

        ctx: dict = {}
        ask = {
            "field": "doctor_type",
            "question": "What type of doctor is Dr. Mitchel?",
            "options": ["Family doctor", "Pediatrician"],
        }
        with mock.patch(
            "app.tip_share._extract_tip_fields",
            return_value=({"name": "Dr. Mitchel", "category": "doctor", "trait": "great"}, ask),
        ):
            reply = run_tip_share_turn(
                user_message="dr mitchel is a great doctor",
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id="block-1",
            )
        self.assertIn("What type of doctor", reply)
        self.assertEqual(ctx["tip_pending_question"], "What type of doctor is Dr. Mitchel?")
        self.assertEqual(ctx["tip_draft"]["suggestions"], ["Family doctor", "Pediatrician"])

        # The answer completes the draft -> ready card, nothing outstanding.
        with mock.patch("app.tip_share._extract_tip_fields", return_value=({}, None)), mock.patch(
            "app.tip_share.compose_reply", side_effect=lambda **kw: kw["fallback"]
        ):
            reply2 = run_tip_share_turn(
                user_message="family doctor",
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id="block-1",
            )
        self.assertIsNone(ctx["tip_pending_question"])
        self.assertTrue(ctx.get("tip_ready"))
        # The provider survived the follow-up — this is the turn that used to be lost.
        self.assertEqual(ctx["tip_draft"]["name"], "Dr. Mitchel")
        self.assertIn("family doctor", ctx["tip_draft"]["details"])
        self.assertIn("Dr. Mitchel", reply2)

    def test_reset_clears_the_pending_question(self) -> None:
        from app.tip_share import reset_tip_share_state

        ctx = {"tip_share_active": True, "tip_pending_question": "Who or where?"}
        reset_tip_share_state(ctx)
        self.assertIsNone(ctx["tip_pending_question"])
        self.assertIsNone(ctx["tip_share_active"])


class TestPlaceStepGetsRealPlaces(unittest.TestCase):
    """A map step asked in the CHAT fork has no Places picker to fall back on, so the
    nearby places have to arrive as the step's suggestions or the answer is typed prose."""

    def test_place_step_suggests_nearby_places(self) -> None:
        from app.tip_share import run_tip_share_turn

        ctx: dict = {"zip_code": "32827"}
        draft = {
            # No name: the SUBJECT step (kind=place for a location) is the map step the walk
            # reaches, and it is the one that used to be a hand-written "who or where?" ask
            # outside the set entirely (dev QA 2026-09-04).
            "category": "park",
            "trait": "shady",
            "reco_type": "location",
            "place_based": True,
            "answers": {"known_for": "the big shaded playground"},
        }
        with mock.patch("app.tip_share._extract_tip_fields", return_value=(draft, None)), mock.patch(
            "app.places.nearby_place_suggestions",
            return_value=["Lake Nona Park", "Nona Adventure Park"],
        ), mock.patch("app.tip_share._reco_tallies", return_value=[]):
            run_tip_share_turn(
                user_message="lake nona park is great, big shaded playground",
                session_ctx=ctx,
                history=[],
                user_jwt="jwt",
                home_block_id="block-1",
            )
        self.assertEqual(ctx["tip_pending_ask"], "subject")
        self.assertEqual(
            ctx["tip_draft"]["suggestions"], ["Lake Nona Park", "Nona Adventure Park"]
        )


class TestFirstPersonOfferIsAShare(unittest.TestCase):
    """dev QA 2026-09-04: "I know a really reliable plumber" was answered with three Google
    plumbers and an "ask your neighbors?" offer — the user's own recommendation read as a
    request for one. Two causes: `_TIP_SEEK_CUE_RE` matches the bare "know a", so the seek
    fallback claimed the turn; and the classifier itself returned looking.tip (the prompt's
    canonical tip_seek examples ARE the phrase "know a good <service>"). The prompt was
    fixed too, but its verdict on this sentence is not stable at temperature 0 — the
    structural matcher is what makes the routing deterministic."""

    def test_i_know_a_service_is_a_share_not_a_seek(self) -> None:
        from app.layer1_intents import (
            utterance_indicates_tip_seek,
            utterance_indicates_tip_share,
        )

        for text in (
            "I know a really reliable plumber",
            "I know a great dentist",
            "we go to a wonderful pediatrician",
            "i found a good mechanic",
        ):
            with self.subTest(text=text):
                self.assertTrue(utterance_indicates_tip_share(text))
                self.assertFalse(utterance_indicates_tip_seek(text))

    def test_asking_for_one_is_still_a_seek(self) -> None:
        """The offer branch is first-person and negation-aware, so none of these flip."""
        from app.layer1_intents import (
            utterance_indicates_tip_seek,
            utterance_indicates_tip_share,
        )

        for text in (
            "do you know a good plumber",
            "anyone know a reliable plumber?",
            "I don't know a good plumber, anyone?",
            "i need a plumber",
            "I have a leak, need a plumber",
            "recommend a plumber",
        ):
            with self.subTest(text=text):
                self.assertFalse(utterance_indicates_tip_share(text))
                self.assertTrue(utterance_indicates_tip_seek(text))

    def test_the_seek_engine_declines_the_turn(self) -> None:
        """The routing consequence: `_try_signal_seek_early_turn` bails on a share, so the
        capture picks it up even when the classifier says looking.tip."""
        from app.discovery_route import _try_signal_seek_early_turn
        from app.lana_unified_pipeline import _turn_is_tip_share

        msg = "I know a really reliable plumber"
        misread = {"linear_intent": "looking.tip", "signal_intent": "tip_seek",
                   "goal": "save_signal", "confidence": 0.9}
        self.assertTrue(_turn_is_tip_share(misread, msg), "the capture has to claim it")
        self.assertIsNone(
            _try_signal_seek_early_turn(
                msg=msg, slots=misread, session_ctx={}, user_jwt="", phone_verified=True,
                home_block_id="b1", phase="listening",
            ),
            "the seek engine must not answer the user's own recommendation",
        )


class TestBareServiceNounIsNotASeek(unittest.TestCase):
    """The regex fallback fires on a REQUEST, not on a service noun standing alone."""

    def test_bare_noun_is_not_a_seek(self) -> None:
        from app.layer1_intents import utterance_indicates_tip_seek

        for text in ("family doctor", "pediatrician", "dentist", "restaurant"):
            with self.subTest(text=text):
                self.assertFalse(utterance_indicates_tip_seek(text))

    def test_real_requests_still_are(self) -> None:
        from app.layer1_intents import utterance_indicates_tip_seek

        for text in (
            "do you know a good doctor",
            "any good dentist nearby?",
            "I need a plumber",
            "can you recommend a pediatrician",
            "looking for a tutor",
            "where can I find a vet",
        ):
            with self.subTest(text=text):
                self.assertTrue(utterance_indicates_tip_seek(text))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
