"""Product FAQ intents — QA 2026-07-08: four direct questions went unanswered.

Each QA phrasing must route to its help.faq_* intent and get the on-topic static answer —
even when the classifier confidently read the turn as the find-peers funnel, and even when
a CTA chip (intent_hint="look_meet") primed a sticky capture on the session context.
"""

import unittest
from unittest.mock import patch

from app.discovery_route import handle_discovery_turn
from app.lana_unified_pipeline import run_lana_unified_pipeline
from app.layer1_intents import faq_linear_intent

QA_SAFETY = "how do I know the moms on here are real and not creeps?"
QA_WHO_FOR_DAD = "I'm a stay at home dad with a 3 year old, is this app for me too?"
QA_WHO_FOR_PREGNANT = "I'm pregnant with my first, due in october. too early for this app?"
QA_ZIP = "I'd rather not give my zip code, is that ok?"
QA_CHILDCARE = "do you offer babysitting?"

# QA phrasing → (expected intent, distinctive on-topic reply substring).
QA_CASES = (
    (QA_SAFETY, "help.faq_safety", "email-verified"),
    (QA_WHO_FOR_DAD, "help.faq_who_for", "stay-at-home dads"),
    (QA_WHO_FOR_PREGNANT, "help.faq_who_for", "not too early"),
    (QA_ZIP, "help.faq_zip_privacy", "never shown to neighbors"),
    (QA_CHILDCARE, "help.faq_childcare", "sitter"),
)

# What main.py stamps on the session context for intent_hint="look_meet" (chip tap).
_LOOK_MEET_HINT_CTX = {
    "routing_phase": "listening",
    "activity_browse_active": True,
    "browse_turns": 0,
    "browse_draft": None,
    "browse_skip_seed": True,
}


class TestFaqDetection(unittest.TestCase):
    def test_qa_phrasings_route_to_their_faq_intent(self) -> None:
        for msg, intent, _ in QA_CASES:
            self.assertEqual(faq_linear_intent(msg), intent, msg)

    def test_childcare_provision_vs_recommendation_seek(self) -> None:
        """Asking Lana to PROVIDE childcare is the FAQ; a sitter recommendation is a tip seek."""
        self.assertEqual(faq_linear_intent("can you watch my kids on saturday?"), "help.faq_childcare")
        for seek in (
            "do you know a good babysitter",
            "can you recommend a daycare near me",
            "any good nanny around here?",
        ):
            self.assertIsNone(faq_linear_intent(seek), seek)

    def test_ordinary_turns_do_not_match(self) -> None:
        for msg in (
            "find me italian moms",
            "i want brazilian coffee this weekend",
            "looking for stroller walk buddies",
            "my name is Zane",
            "what can you do",
            "I am a teacher",
            "32827",
            "weekend mornings",
            "yes",
        ):
            self.assertIsNone(faq_linear_intent(msg), msg)


class TestFaqDiscoveryRouting(unittest.TestCase):
    """A direct question outranks the funnel — even a confident find-peers read."""

    FUNNEL_SLOTS = {
        # The QA failure mode: the classifier confidently funnels the question.
        "linear_intent": "discovery.find_peers",
        "goal": "peers",
        "in_discovery": True,
        "confidence": 0.9,
    }

    @patch("app.discovery_route.track")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_qa_phrasings_answered_not_funnelled(
        self, mock_slots, _mock_ai, mock_track
    ) -> None:
        from app.faq_replies import faq_topic

        for msg, intent, needle in QA_CASES:
            mock_slots.return_value = dict(self.FUNNEL_SLOTS)
            mock_track.reset_mock()
            reply, ctx, _, peers = handle_discovery_turn(
                msg,
                session_ctx={"routing_phase": "listening"},
                user_jwt="jwt",
                phone_verified=True,
                home_block_id="block-1",
                is_anonymous=False,
            )
            self.assertEqual(peers, [], msg)
            self.assertEqual(ctx.get("active_intent"), intent, msg)
            self.assertIn(needle, reply, msg)
            mock_track.assert_called_once()
            self.assertEqual(
                mock_track.call_args.kwargs["event_properties"]["topic"],
                faq_topic(intent),
                msg,
            )

    @patch("app.discovery_route.track")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_zip_question_keeps_need_zip_phase(self, mock_slots, _mock_ai, _tr) -> None:
        """The answer is a detour, not a lane change — the ZIP funnel resumes next turn."""
        mock_slots.return_value = {"goal": "continue", "confidence": 0.6}
        reply, ctx, _, _ = handle_discovery_turn(
            QA_ZIP,
            session_ctx={"routing_phase": "need_zip"},
            user_jwt="jwt",
            phone_verified=False,
            home_block_id=None,
            is_anonymous=True,
        )
        self.assertIn("never shown to neighbors", reply)
        self.assertEqual(ctx.get("routing_phase"), "need_zip")

    @patch("app.discovery_route.track")
    @patch("app.discovery_route.discovery_ai_enabled", return_value=True)
    @patch("app.discovery_route.discovery_slots_for_turn")
    def test_classifier_labelled_faq_paraphrase_answered(
        self, mock_slots, _mock_ai, _tr
    ) -> None:
        """A paraphrase the regex can't see still answers via the classifier's label."""
        mock_slots.return_value = {
            "linear_intent": "help.faq_childcare",
            "goal": "chat",
            "in_discovery": False,
            "confidence": 0.9,
        }
        reply, ctx, _, _ = handle_discovery_turn(
            "any chance someone could take the kids for an hour?",
            session_ctx={"routing_phase": "listening"},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
        )
        self.assertEqual(ctx.get("active_intent"), "help.faq_childcare")
        self.assertIn("sitter", reply)


class TestFaqOutranksIntentHint(unittest.TestCase):
    """A chip tap (intent_hint="look_meet") primes a sticky capture on the session context —
    a typed direct question must still be answered, with the flow left intact to resume."""

    def _run(self, msg: str, mock_track) -> tuple[str, dict]:
        reply, status, ctx, _, _ = run_lana_unified_pipeline(
            user_id="user-1",
            session_id="session-1",
            history=[],
            user_message=msg,
            session_ctx=dict(_LOOK_MEET_HINT_CTX),
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
            is_anonymous=False,
            use_orchestrator=False,
        )
        self.assertEqual(status, "continue", msg)
        return reply, ctx

    @patch("app.lana_unified_pipeline.track")
    def test_qa_phrasings_answered_with_look_meet_hint(self, mock_track) -> None:
        from app.faq_replies import faq_topic

        for msg, intent, needle in QA_CASES:
            mock_track.reset_mock()
            reply, ctx = self._run(msg, mock_track)
            self.assertIn(needle, reply, msg)
            self.assertEqual(ctx.get("active_intent"), intent, msg)
            mock_track.assert_called_once()
            self.assertEqual(
                mock_track.call_args.kwargs["event_properties"]["topic"],
                faq_topic(intent),
                msg,
            )

    @patch("app.lana_unified_pipeline.track")
    def test_answer_leaves_the_primed_flow_intact(self, mock_track) -> None:
        """The FAQ is a detour: the browse capture the chip primed must survive the turn."""
        _, ctx = self._run(QA_SAFETY, mock_track)
        self.assertTrue(ctx.get("activity_browse_active"))
        self.assertTrue(ctx.get("browse_skip_seed"))


if __name__ == "__main__":
    unittest.main()
