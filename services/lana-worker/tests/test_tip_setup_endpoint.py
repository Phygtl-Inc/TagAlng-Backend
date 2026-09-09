"""The carousel fork of the recommendation capture posts every answer at once.

The chat fork asks one question per turn, which is right in conversation and wrong on the
"flip through cards" screen: eight steps would be eight round trips, and a user who swiped
ahead and filled step 6 first would have their answer land on step 2's question. This
endpoint takes the whole set, and takes it against the session's OWN generated steps — the
question set is written per recommendation, so there is no enum to validate against and
"whatever key the client sent" is not an acceptable substitute.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import AuthSession
from app.main import TipSetupRequest, set_tip_setup

AUTH = "Bearer test-token"
AUTH_SESSION = AuthSession(
    user_id="u-1", is_anonymous=False, phone_verified=True, home_block_id="b1"
)

STEPS = [
    {"field": "used_for", "label": "Used for", "question": "What is it used for?",
     "kind": "text", "required": True},
    {"field": "where_to_buy", "label": "Where to buy", "question": "Where can neighbours buy it?",
     "kind": "text", "required": True},
    {"field": "liked", "label": "What you liked", "question": "What did you like about it?",
     "kind": "text", "required": False},
    {"field": "ask_ok", "label": "Neighbours", "question": "Can neighbours ask you more?",
     "kind": "toggle", "required": False},
]


def _run(
    answers: dict, *, draft: dict | None = None, judge: list | None = None
) -> tuple[dict, dict]:
    """Returns (response, the session context as it was written back)."""
    ctx = {
        "tip_draft": draft
        if draft is not None
        else {"name": "Hatch Rest", "category": "baby gear", "reco_type": "product",
              "step_set": STEPS, "answers": {"used_for": "A night light"}},
        "tip_share_active": True,
    }
    written: dict = {}
    with (
        patch("app.main.verify_auth", return_value=AUTH_SESSION),
        patch("app.main.get_session_for_user", return_value={"context": ctx}),
        patch("app.main.update_session_context", side_effect=lambda sid, c: written.update(c)),
        patch("app.tip_share.judge_answers", return_value=list(judge or [])),
    ):
        res = set_tip_setup("s-1", TipSetupRequest(answers=answers), authorization=AUTH)
    return res, written


class TestTipSetup(unittest.TestCase):
    def test_answers_merge_onto_the_draft(self) -> None:
        res, ctx = _run({"where_to_buy": "  Amazon ·  ~$60 ", "liked": "The soft glow"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["missing"], [])
        self.assertEqual(
            ctx["tip_draft"]["answers"],
            # Whitespace squeezed; the answer captured in chat before the carousel survives.
            {"used_for": "A night light", "where_to_buy": "Amazon · ~$60", "liked": "The soft glow"},
        )

    def test_only_this_recommendations_own_fields_are_accepted(self) -> None:
        # A generated set means no fixed enum to check against — so the session's step set
        # IS the allowlist. Without it a client could write any key into the draft.
        _, ctx = _run({"where_to_buy": "Amazon", "reco_type": "professional", "evil": "x"})
        self.assertEqual(
            sorted(ctx["tip_draft"]["answers"]), ["used_for", "where_to_buy"]
        )
        self.assertEqual(ctx["tip_draft"]["reco_type"], "product", "not overwritable")

    def test_ready_only_once_the_required_steps_are_in(self) -> None:
        res, ctx = _run({"liked": "The soft glow"})
        self.assertEqual(res["missing"], ["where_to_buy"])
        self.assertIsNone(ctx.get("tip_ready"))
        res, ctx = _run({"where_to_buy": "Amazon"})
        self.assertTrue(ctx["tip_ready"])

    def test_blank_answers_do_not_erase_what_is_there(self) -> None:
        _, ctx = _run({"used_for": "   "})
        self.assertEqual(ctx["tip_draft"]["answers"]["used_for"], "A night light")

    def test_every_shown_step_counts_as_offered(self) -> None:
        # Otherwise the turn right after the carousel re-asks, one at a time, every optional
        # the user deliberately left blank.
        _, ctx = _run({"where_to_buy": "Amazon"})
        self.assertEqual(
            ctx["tip_asked_fields"], ["ask_ok", "liked", "used_for", "where_to_buy"]
        )
        self.assertIsNone(ctx["tip_pending_ask"])

    def test_no_draft_is_a_conflict_not_a_new_draft(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            _run({"used_for": "x"}, draft={})
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()


class TestJunkAnswers(unittest.TestCase):
    """A submit full of "bla bla bla" came back as the same form with no reason given: the
    worker's nudge went to a chat bubble the carousel was covering (dev QA 2026-09-08)."""

    def test_a_junk_answer_comes_back_named_and_blocks_the_ready_card(self) -> None:
        res, ctx = _run(
            {"where_to_buy": "bla bla bla"},
            judge=[{"field": "where_to_buy", "why": "not a place to buy it"}],
        )
        self.assertEqual(res["weak"], [{"field": "where_to_buy", "why": "not a place to buy it"}])
        self.assertNotEqual(ctx.get("tip_ready"), True, "not ready while one answer is junk")
        # Kept, not discarded — it is in the box the user is about to edit.
        self.assertEqual(ctx["tip_draft"]["answers"]["where_to_buy"], "bla bla bla")
        self.assertEqual(ctx["tip_reasked_fields"], ["where_to_buy"])

    def test_the_second_submit_always_goes_through(self) -> None:
        """One nudge, then their words stand — a neighbour doing us a favour is not a form
        validator. The endpoint tells the judge what it has already queried."""
        draft = {"name": "Hatch Rest", "category": "baby gear", "reco_type": "product",
                 "step_set": STEPS, "answers": {"used_for": "A night light"}}
        ctx = {"tip_draft": draft, "tip_share_active": True,
               "tip_reasked_fields": ["where_to_buy"]}
        seen: dict = {}
        with (
            patch("app.main.verify_auth", return_value=AUTH_SESSION),
            patch("app.main.get_session_for_user", return_value={"context": ctx}),
            patch("app.main.update_session_context", side_effect=lambda sid, c: None),
            patch("app.tip_share.judge_answers", side_effect=lambda *a, **kw: seen.update(kw)),
        ):
            set_tip_setup(
                "s-1",
                TipSetupRequest(answers={"where_to_buy": "bla bla bla"}),
                authorization=AUTH,
            )
        self.assertEqual(seen["skip"], ["where_to_buy"], "already queried once — let it be")

    def test_a_clean_submit_is_ready(self) -> None:
        res, ctx = _run({"where_to_buy": "Target"}, judge=None)
        self.assertEqual(res["weak"], [])
        self.assertTrue(ctx["tip_ready"])

    def test_every_bad_answer_comes_back_in_one_pass(self) -> None:
        """One problem per submit made fixing three answers cost three round trips, each
        one hiding the next (dev QA 2026-09-08)."""
        res, ctx = _run(
            {"where_to_buy": "idk", "liked": "bla bla", "used_for": "why?"},
            judge=[
                {"field": "where_to_buy", "why": "not a place"},
                {"field": "liked", "why": "not a real answer"},
                {"field": "used_for", "why": "answered with a question",
                 "reply": "So a neighbour knows what it's for — what do you use it for?"},
            ],
        )
        self.assertEqual([w["field"] for w in res["weak"]],
                         ["where_to_buy", "liked", "used_for"])
        # All three are queried once, so the next submit stands whatever they say.
        self.assertEqual(sorted(ctx["tip_reasked_fields"]),
                         ["liked", "used_for", "where_to_buy"])

    def test_a_cleared_submit_is_not_re_judged_by_the_turn(self) -> None:
        """The turn after a submit runs its own per-answer judge. It was re-rejecting a set
        this endpoint had just cleared — a different field each time — so the carousel
        reopened for ever and the tip could never be posted (dev QA 2026-09-08). Every
        submitted field is marked queried, which is what the turn's nudge skips on."""
        _, ctx = _run({"where_to_buy": "Target", "liked": "the staff"}, judge=None)
        self.assertEqual(sorted(ctx["tip_reasked_fields"]), ["liked", "where_to_buy"])
        self.assertTrue(ctx["tip_ready"])


class TestQuestionsBack(unittest.TestCase):
    def test_a_question_back_gets_an_answer_not_a_label(self) -> None:
        """"why?" in a card was met with "Answered with a question, not info" — a label
        that answers nothing (dev QA 2026-09-08). The judge writes the answer instead."""
        res, _ = _run(
            {"where_to_buy": "why?"},
            judge=[{
                "field": "where_to_buy",
                "why": "answered with a question",
                "reply": "So a neighbour knows where to actually get one — where did you?",
            }],
        )
        self.assertEqual(
            res["weak"][0]["reply"],
            "So a neighbour knows where to actually get one — where did you?",
        )
