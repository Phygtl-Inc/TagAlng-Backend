"""Queued questions that never went through the already-told check get it once, and the ones
the person already answered are closed.

Prod, 2026-10-15: after the check shipped (PR #241), 53 questions queued BEFORE it still asked
what people had said ("how often do you play tennis?" to someone who plays every other day).
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service")

from app import rapport_gaps  # noqa: E402
from tests.test_rapport_already_told import FIG_Q, _Rows  # noqa: E402

TENNIS = {"gap_row_id": "g-tennis", "gap_id": "deepen:tennis_player", "affiliation_ref": None,
          "question": "How often do you play tennis each week?", "question_embedding": "[0.1, 0.2]"}
BOOKS = {"gap_row_id": "g-books", "gap_id": "deepen:reader", "affiliation_ref": None,
         "question": "What kind of books do you read?", "question_embedding": None}
GROUND = {"gap_row_id": "g-ground", "gap_id": "ground:aff-1", "affiliation_ref": "aff-1",
          "question": "Which Fitness CF is yours?", "question_embedding": None}
RECO = {"gap_row_id": "g-reco", "gap_id": "reco:sig-1", "affiliation_ref": None,
        "question": "Is The Backhaus your go-to bakery?", "question_embedding": None}


class TestRevalidateQueue(unittest.TestCase):
    def _run(self, rows, verdicts):
        db = _Rows(list(rows))
        with patch.object(rapport_gaps, "service_client", return_value=db), \
             patch.object(rapport_gaps, "told_verdict", side_effect=lambda u, q, e: verdicts[q]) as judge:
            closed = rapport_gaps.revalidate_queue("u1")
        return closed, {g: p for g, p in db.updates}, judge

    def test_closes_what_they_already_told_and_stamps_the_rest(self):
        closed, updates, judge = self._run(
            [TENNIS, BOOKS],
            {TENNIS["question"]: "every other day (from: they said: tennis every other day)",
             BOOKS["question"]: ""},
        )
        self.assertEqual(closed, 1)
        self.assertEqual(updates["g-tennis"]["status"], "answered")
        self.assertIn("told_checked_at", updates["g-tennis"])
        self.assertNotIn("status", updates["g-books"])
        self.assertIn("told_checked_at", updates["g-books"])
        # The stored question embedding is handed to the check, parsed.
        self.assertEqual(judge.call_args_list[0][0][2], [0.1, 0.2])

    def test_a_question_it_could_not_judge_is_left_for_next_time(self):
        closed, updates, _ = self._run([TENNIS], {TENNIS["question"]: None})
        self.assertEqual((closed, updates), (0, {}))

    def test_grounding_and_confirming_questions_are_stamped_never_judged(self):
        closed, updates, judge = self._run([GROUND, RECO], {})
        judge.assert_not_called()
        self.assertEqual(closed, 0)
        self.assertEqual(set(updates), {"g-ground", "g-reco"})
        self.assertTrue(all("status" not in p for p in updates.values()))

    def test_database_without_the_column_does_nothing(self):
        db = MagicMock()
        db.table.side_effect = RuntimeError("column told_checked_at does not exist")
        with patch.object(rapport_gaps, "service_client", return_value=db), \
             patch.object(rapport_gaps, "told_verdict") as judge:
            self.assertEqual(rapport_gaps.revalidate_queue("u1"), 0)
        judge.assert_not_called()


class TestToldVerdictCanSayUnknown(unittest.TestCase):
    def _verdict(self, *, configured=True, data=None, raises=False):
        with patch.object(rapport_gaps, "_told_lines", return_value=["they said: x"]), \
             patch("app.orchestrator.llm.llm_configured", return_value=configured), \
             patch("app.orchestrator.llm.extractor_model", return_value="m"), \
             patch("app.orchestrator.llm.llm_json",
                   side_effect=RuntimeError("down") if raises else None, return_value=data):
            return rapport_gaps.told_verdict("u1", FIG_Q)

    def test_unknown_when_the_model_is_missing_failing_or_garbled(self):
        self.assertIsNone(self._verdict(configured=False))
        self.assertIsNone(self._verdict(raises=True))
        self.assertIsNone(self._verdict(data="not json"))

    def test_judged_not_told_is_empty_not_unknown(self):
        self.assertEqual(self._verdict(data={"can_answer": False}), "")

    def test_already_told_still_fails_open(self):
        with patch.object(rapport_gaps, "told_verdict", return_value=None):
            self.assertEqual(rapport_gaps.already_told("u1", FIG_Q), "")


class TestNewQuestionsAreStampedAtOpening(unittest.TestCase):
    def _open(self, verdict, fail_first=False, **kw):
        db = _Rows()
        inserted = []

        def insert(row):
            inserted.append(dict(row))
            if fail_first and len(inserted) == 1:
                raise RuntimeError("column told_checked_at does not exist")
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[{"gap_row_id": "g1"}]))

        db.insert = insert
        with patch.object(rapport_gaps, "service_client", return_value=db), \
             patch.object(rapport_gaps, "_question_embedding", return_value=[0.1]), \
             patch.object(rapport_gaps, "_is_semantic_duplicate", return_value=False), \
             patch.object(rapport_gaps, "told_verdict", return_value=verdict), \
             patch("app.rapport_priority.score_for", return_value=0.5), \
             patch("app.rapport_reasons.attach_ask_reason_async"), \
             patch("app.lang_pref.get_user_preferred_language", return_value="en"):
            opened = rapport_gaps.open_semantic_gap("u1", None, FIG_Q, label="fig", **kw)
        return opened, inserted

    def test_checked_question_carries_the_stamp(self):
        opened, inserted = self._open("")
        self.assertTrue(opened)
        self.assertIn("told_checked_at", inserted[0])

    def test_unjudged_question_opens_unstamped_so_revalidation_retries_it(self):
        opened, inserted = self._open(None)
        self.assertTrue(opened)
        self.assertNotIn("told_checked_at", inserted[0])

    def test_database_without_the_column_still_gets_the_question(self):
        opened, inserted = self._open("", fail_first=True)
        self.assertTrue(opened)
        self.assertEqual(len(inserted), 2)
        self.assertNotIn("told_checked_at", inserted[1])


class TestTileLoadTriggersRevalidation(unittest.TestCase):
    def _call(self, anonymous):
        from app import main

        bg = MagicMock()
        with patch.object(main, "verify_auth", return_value=SimpleNamespace(user_id="u1", is_anonymous=anonymous)), \
             patch.object(main, "rapport_next_ask", return_value=None):
            main.post_rapport_next_ask(bg, None, "Bearer x")
        return [c[0][0] for c in bg.add_task.call_args_list]

    def test_signed_in_tile_load_schedules_it(self):
        from app import main

        self.assertIn(main.rapport_revalidate_queue, self._call(anonymous=False))

    def test_guest_tile_load_does_not(self):
        self.assertEqual(self._call(anonymous=True), [])


if __name__ == "__main__":
    unittest.main()
