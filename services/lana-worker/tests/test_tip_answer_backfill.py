"""A neighbour who says everything up front must not be asked it all again.

Prod 2026-09-14 (Tommaso, session dcdb8498): a minute of detail about Pausa came back as
"Heard you — Pausa · restaurant · authentic Italian, homemade dishes, imported
ingredients, Neapolitan pizza, great wine. What dishes at Pausa are must-tries? (2/9)"
— Lana's own summary proving she understood, over a card with one row filled. He repeated
himself three times, was told his answer did not answer the question, then tapped out four
answers he had already spoken.

Cause: the tailored question set is written on the SAME turn the message arrives, and
extraction runs BEFORE it exists — so the model is handed "(type not known yet — return {}
for answers)" and nothing ever re-reads the message against the fields it then creates.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.tip_share import COMMUNITY_FIELD, resolve_community

PAUSA = (
    "So the restaurant Pausa in San Mateo is one of the best restaurants when it comes to "
    "a contemporary but original authentic Italian cuisine. All appetizer pizzas, pastas, "
    "desserts are homemade. The pizza dough and the preparation is Neapolitan style."
)

# What the extractor returns on each of the two calls of the set-writing turn. The first is
# the real shape of the bug: rich `steps`, and `answers` EMPTY because no fields existed to
# map onto yet. The second is the backfill, reading the same words against the new set.
FIRST = {
    "name": "Pausa",
    "category": "restaurant",
    "trait": "authentic Italian, homemade dishes, Neapolitan pizza",
    "reco_type": "restaurant",
    "place_based": True,
    "answers": {},
    "steps_raw": [
        {"field": "subject", "label": "Which", "question": "Which restaurant?",
         "placeholder": "Pausa", "options": []},
        {"field": "dish", "label": "Must-try dish", "question": "What dishes at Pausa are must-tries?",
         "placeholder": "Neapolitan pizza",
         "options": ["Neapolitan pizza", "Housemade pasta", "Imported cheese board"]},
        {"field": "vibe", "label": "Atmosphere", "question": "What's the vibe at Pausa?",
         "placeholder": "Lively", "options": ["Lively and upscale", "Quiet", "Casual"]},
    ],
}
BACKFILL = {"answers": {"dish": "Neapolitan pizza"}}


def _run(msg: str, ctx: dict, extracts: list[dict]) -> str:
    """One capture turn with the extractor replayed from `extracts`, in call order."""
    from app import tip_share

    calls = iter(extracts)

    def fake_extract(**_kw):
        return dict(next(calls)), None

    with patch.object(tip_share, "_extract_tip_fields", side_effect=fake_extract), patch(
        "app.discovery_route.resolve_block_id", return_value="block-1"
    ), patch.object(tip_share, "_reco_tallies", return_value=[]), patch.object(
        tip_share, "my_communities", return_value=[]
    ), patch.object(tip_share, "readback", return_value=""):
        return tip_share.run_tip_share_turn(
            user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt",
            home_block_id="block-1",
        )


class Backfill(unittest.TestCase):
    def test_the_opening_message_fills_the_set_it_just_created(self) -> None:
        ctx: dict = {}
        reply = _run(PAUSA, ctx, [FIRST, BACKFILL])
        answers = ctx["tip_draft"]["answers"]
        # The whole point: a field the user answered in prose, on the turn the field was
        # born. Before the fix this was absent and the next question asked for it.
        self.assertEqual(answers.get("dish"), "Neapolitan pizza")
        self.assertNotIn("must-tries", reply)

    def test_a_tap_the_user_made_outranks_the_backfill(self) -> None:
        ctx: dict = {}
        greedy = {"answers": {"dish": "something the re-read invented"}}
        _run(PAUSA, ctx, [{**FIRST, "answers": {"dish": "Housemade pasta"}}, greedy])
        self.assertEqual(ctx["tip_draft"]["answers"]["dish"], "Housemade pasta")

    def test_a_message_that_answered_other_steps_is_never_called_a_non_answer(self) -> None:
        # His third repeat was judged weak against the open question and binned. A message
        # carrying answers for OTHER steps is a neighbour saying more than was asked.
        ctx: dict = {"tip_draft": {"step_set": FIRST["steps_raw"], "reco_type": "restaurant",
                                   "name": "Pausa", "category": "restaurant",
                                   "answers": {"subject": "Pausa"}},
                     "tip_share_active": True}
        # Faithful to prod: the monologue was stamped into the OPEN field (dish) and
        # judged weak there, while also answering vibe. The guard has to read the second.
        rich = {"answers": {"dish": PAUSA, "vibe": "Lively and upscale"},
                "weak_answer": {"field": "dish", "why": "not a dish"}}
        _run(PAUSA, ctx, [rich])
        answers = ctx["tip_draft"]["answers"]
        self.assertEqual(answers.get("vibe"), "Lively and upscale")
        self.assertNotIn("dish", ctx.get("tip_reasked_fields") or [])


class SubjectIsTheCommunity(unittest.TestCase):
    def test_recommending_your_own_community_links_without_asking(self) -> None:
        ctx = {"tip_communities": [{"place_id": "p1", "name": "Pausa Bar & Cookery"}]}
        draft: dict = {"name": "Pausa"}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertEqual(draft["circle_place_id"], "p1")
        # Pre-ANSWERED, not just pre-filled — that is what takes the question off the walk.
        self.assertEqual(draft["answers"][COMMUNITY_FIELD], "Pausa Bar & Cookery")

    def test_an_unrelated_subject_never_claims_a_community(self) -> None:
        # The plumber: scoped to Pausa, but the tip is not about Pausa. The header
        # pre-select still applies (product decision) — the NAME match must not.
        ctx = {"tip_communities": [{"place_id": "p1", "name": "Pausa Bar & Cookery"}]}
        draft: dict = {"name": "Dave the plumber"}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertIsNone(draft.get("circle_place_id"))
        self.assertNotIn(COMMUNITY_FIELD, draft.get("answers") or {})

    def test_a_short_name_does_not_swallow_a_longer_community(self) -> None:
        ctx = {"tip_communities": [{"place_id": "p1", "name": "Joe's Gym"}]}
        draft: dict = {"name": "Joe"}
        resolve_community(draft, user_jwt="jwt", session_ctx=ctx)
        self.assertIsNone(draft.get("circle_place_id"))


class SeedTurnNeverReleases(unittest.TestCase):
    """The turn that OPENS the capture cannot be a pivot away from it.

    Dev 2026-09-15: "I want to recommend Bella Vita in San Mateo — it's authentic
    Italian..." armed the share, and the classifier — reading the sentence on its words
    alone — called it `looking.tip`. The lane released on that same turn and the
    recommendation came back answered as a SEARCH: "No neighbor has recommended one yet,
    here's what's nearby (from Google)".

    The guard itself (app/lana_unified_pipeline.py) is inline in run_lana_turn, which has
    no test harness — it is covered by the live retest, not from here. What IS worth
    pinning is the arming it depends on: lose this and the seed turn never even reaches
    the gate.
    """

    def test_the_entry_phrase_arms_the_share_not_the_seek(self) -> None:
        from app.tip_share import looks_like_tip_share_entry

        self.assertTrue(looks_like_tip_share_entry("I want to recommend Bella Vita"))
        self.assertTrue(looks_like_tip_share_entry("a tip to share"))
        # The seek side must NOT arm it — that lane answers, it does not capture.
        self.assertFalse(looks_like_tip_share_entry("anyone know a good plumber?"))
        self.assertFalse(looks_like_tip_share_entry("find me an italian place"))


if __name__ == "__main__":
    unittest.main()


class PolicyQuestionKeepsItsAnswer(unittest.TestCase):
    """An answer to a question Lana asked in chat is an ANSWER, not a new recommendation.

    Prod 2026-09-14 (Tommaso, session a1f06c89): he answered the rapport question "What do
    you enjoy most about Pausa Bar & Cookery?", Lana followed up with "Mortadella pizza
    especially, or just the pizza there in general?", and he typed his own answer instead
    of tapping a chip — "The fig, gorgonzola with caramelized onions is the best". That
    opened a BRAND NEW recommendation with no subject and asked him which pizzeria he
    meant, offering four unrelated ones, while he sat inside the Pausa community.

    Cause: every other capture publishes its open question into _active_capture_context; a
    policy ask published nothing, so the answer was classified cold. A chip TAP survived
    (the pipeline recognises policy_chip_msgs) — only typing was punished.
    """

    def test_the_question_is_stamped_when_the_turn_asks_one(self) -> None:
        from app.policy.decide import NextAction, note_ask_streak

        ctx: dict = {}
        note_ask_streak(ctx, NextAction(kind="ask_gap", utterance="What do you enjoy most about Pausa?"))
        self.assertEqual(ctx["policy_pending_question"], "What do you enjoy most about Pausa?")

    def test_a_turn_that_asks_nothing_clears_it(self) -> None:
        from app.policy.decide import NextAction, note_ask_streak

        ctx = {"policy_pending_question": "stale question"}
        note_ask_streak(ctx, NextAction(kind="reply", utterance="Nice, thanks for telling me."))
        self.assertIsNone(ctx["policy_pending_question"])

    def test_the_classifier_is_told_a_question_is_open(self) -> None:
        from app.discovery_slots import _active_capture_context

        note = _active_capture_context({"policy_pending_question": "Mortadella pizza, or the pizza in general?"})
        self.assertTrue(note.startswith("conversation"))
        self.assertIn("Mortadella pizza", note)
        # The discriminator is the SUBJECT: same subject = answer, different = pivot.
        self.assertIn("PIVOT", note)

    def test_a_real_capture_still_outranks_it(self) -> None:
        from app.discovery_slots import _active_capture_context

        note = _active_capture_context(
            {"tip_share_active": True, "policy_pending_question": "anything"}
        )
        self.assertTrue(note.startswith("tip_share"))

    def test_inert_with_no_question_open(self) -> None:
        from app.discovery_slots import _active_capture_context

        self.assertEqual(_active_capture_context({}), "none")
