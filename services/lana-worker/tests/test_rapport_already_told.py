"""Lana checks a question against what the person already TOLD her, not only against her
own earlier questions.

Prod, todiba: asked which pizzeria he gets his fig and gorgonzola pizza from (he had
answered it at Pausa), which bakery for mortadella pizza (answered in August), and which
running club (he had said he runs alone). Every check compared questions with questions;
nothing he said, in chat or in an answer, counted.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service")

from app import claims_persist, rapport_gaps  # noqa: E402
from app.claims_persist import ClaimExtractResult  # noqa: E402

FIG_Q = "Which local pizzeria or spot do you usually get your fig and gorgonzola pizza from?"


class _Rows:
    """A supabase-ish client: every select returns ``rows``; updates are recorded."""

    def __init__(self, rows=None, fail_columns: str | None = None):
        self.rows, self.updates, self.selects = rows or [], [], []
        self.fail_columns = fail_columns

    def table(self, name):
        self._table = name
        return self

    def select(self, cols, *_a, **_k):
        self._mode, self._cols = "select", cols
        self.selects.append((self._table, cols))
        return self

    def update(self, patch_):
        self._mode, self._patch = "update", dict(patch_)
        return self

    def eq(self, col, val):
        if self._mode == "update" and col == "gap_row_id":
            self._target = val
        return self

    def in_(self, *_a):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self._mode == "update":
            if self.fail_columns and self.fail_columns in self._patch:
                raise RuntimeError(f"column {self.fail_columns} does not exist")
            self.updates.append((self._target, self._patch))
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=list(self.rows.get(self._table, [])) if isinstance(self.rows, dict) else self.rows)


class TestAlreadyToldVerdict(unittest.TestCase):
    def _verdict(self, data, lines=("Lana asked \"another topping at Pausa?\" and they answered: the fig pizza",)):
        with patch.object(rapport_gaps, "_told_lines", return_value=list(lines)), \
             patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.extractor_model", return_value="m"), \
             patch("app.orchestrator.llm.llm_json", return_value=data) as llm:
            return rapport_gaps.already_told("u1", FIG_Q, [0.1]), llm

    def test_answerable_question_returns_the_answer_and_its_source(self):
        out, _ = self._verdict({"lana_would_answer": "Pausa", "from": "another topping at Pausa", "can_answer": True})
        self.assertIn("Pausa", out)
        self.assertIn("from:", out)

    def test_a_bare_true_without_an_answer_is_not_evidence(self):
        out, _ = self._verdict({"lana_would_answer": "", "from": "x", "can_answer": True})
        self.assertEqual(out, "")
        out, _ = self._verdict({"lana_would_answer": "Pausa", "from": "", "can_answer": True})
        self.assertEqual(out, "")

    def test_cannot_answer_means_ask(self):
        out, _ = self._verdict({"lana_would_answer": "", "from": "", "can_answer": False})
        self.assertEqual(out, "")

    def test_model_failure_fails_open(self):
        with patch.object(rapport_gaps, "_told_lines", return_value=["they said: x"]), \
             patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.extractor_model", return_value="m"), \
             patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")):
            self.assertEqual(rapport_gaps.already_told("u1", FIG_Q), "")

    def test_nothing_told_never_calls_the_model(self):
        out, llm = self._verdict({"can_answer": True}, lines=())
        self.assertEqual(out, "")
        llm.assert_not_called()

    def test_uses_the_extraction_model(self):
        _, llm = self._verdict({"can_answer": False})
        self.assertEqual(llm.call_args.kwargs["model"], "m")


class TestToldLines(unittest.TestCase):
    def test_only_their_own_words_count_as_said(self):
        recalled = [
            {"source_type": "message", "content": "user: I love the pizza with figs"},
            {"source_type": "message", "content": "assistant: What makes it your favorite?"},
            {"source_type": "inquiry", "content": "food: find pizza near me"},
            {"source_type": "claim", "content": "Likes fig and gorgonzola pizza (fig_gorgonzola_pizza_fan)"},
        ]
        db = _Rows({
            "local_signals": [{"reco_name": "Pausa", "category": "restaurant", "reco_place": "San Mateo",
                               "reco_fields": [{"field": "dish", "label": "Must-try dish", "answer": "Neapolitan pizza"},
                                               {"field": "ask_ok", "label": "Neighbours", "answer": "Let them ask"}]}],
            "rapport_gaps": [{"question": "What do you enjoy most about Pausa?", "answer_text": "The pizza"},
                             {"question": "Which gym?", "answer_text": None}],
        })
        with patch("app.orchestrator.recall.recall_memories", return_value=recalled), \
             patch.object(rapport_gaps, "service_client", return_value=db):
            lines = rapport_gaps._told_lines("u1", FIG_Q, None)
        self.assertIn("they said: I love the pizza with figs", lines)
        self.assertFalse(any("What makes it your favorite" in line for line in lines))
        self.assertFalse(any("find pizza near me" in line for line in lines))
        self.assertIn("on their profile: Likes fig and gorgonzola pizza (fig_gorgonzola_pizza_fan)", lines)
        self.assertIn("they recommended Pausa (restaurant, San Mateo): Must-try dish: Neapolitan pizza", lines)
        self.assertIn('Lana asked "What do you enjoy most about Pausa?" and they answered: The pizza', lines)
        self.assertIn('Lana asked "Which gym?" and they answered', lines)

    def test_nearest_ranks_by_similarity_and_keeps_k(self):
        rows = [{"id": "far", "v": "[0, 1]"}, {"id": "none", "v": None}, {"id": "near", "v": "[1, 0.1]"}]
        self.assertEqual([r["id"] for r in rapport_gaps._nearest(rows, "v", [1.0, 0.0], 2)], ["near", "far"])
        # No query vector: newest first, as read.
        self.assertEqual([r["id"] for r in rapport_gaps._nearest(rows, "v", None, 2)], ["far", "none"])


class TestOpenGapChecksWhatTheySaid(unittest.TestCase):
    def _open(self, told="", **kw):
        db = _Rows()
        db.insert = MagicMock(return_value=SimpleNamespace(execute=lambda: SimpleNamespace(data=[{"gap_row_id": "g1"}])))
        with patch.object(rapport_gaps, "service_client", return_value=db), \
             patch.object(rapport_gaps, "_question_embedding", return_value=[0.1]), \
             patch.object(rapport_gaps, "_is_semantic_duplicate", return_value=False), \
             patch.object(rapport_gaps, "already_told", return_value=told) as check, \
             patch("app.rapport_priority.score_for", return_value=0.5), \
             patch("app.rapport_reasons.attach_ask_reason_async"), \
             patch("app.lang_pref.get_user_preferred_language", return_value="en"):
            opened = rapport_gaps.open_semantic_gap("u1", None, FIG_Q, label="fig pizza", **kw)
        return opened, check, db

    def test_question_they_already_answered_is_not_opened(self):
        opened, check, db = self._open(told="Pausa (from: …)")
        self.assertFalse(opened)
        db.insert.assert_not_called()
        self.assertEqual(check.call_args[0][:2], ("u1", FIG_Q))

    def test_question_they_never_answered_is_opened(self):
        opened, _, db = self._open(told="")
        self.assertTrue(opened)
        db.insert.assert_called_once()

    def test_confirming_and_grounding_questions_are_not_checked(self):
        _, check, _ = self._open(told="x", skip_dedup=True)
        check.assert_not_called()
        _, check, _ = self._open(told="x", affiliation_ref="aff-1")
        check.assert_not_called()


class TestAnswerTextIsKept(unittest.TestCase):
    def test_answer_text_is_written(self):
        db = _Rows()
        with patch.object(rapport_gaps, "service_client", return_value=db):
            rapport_gaps.mark_answered("g1", "c1", answer_text="  Pausa in   San Mateo, CA ")
        self.assertEqual(db.updates[0][1]["answer_text"], "Pausa in San Mateo, CA")
        self.assertEqual(db.updates[0][1]["answer_claim_id"], "c1")

    def test_gap_still_closes_on_a_database_without_the_column(self):
        db = _Rows(fail_columns="answer_text")
        with patch.object(rapport_gaps, "service_client", return_value=db):
            rapport_gaps.mark_answered("g1", "c1", answer_text="Pausa")
        self.assertEqual(len(db.updates), 1)
        self.assertEqual(db.updates[0][1]["status"], "answered")
        self.assertNotIn("answer_text", db.updates[0][1])


class TestChatClosesWhatItAnswers(unittest.TestCase):
    QUEUED = [{"gap_row_id": "g-church", "question": "How long have you been attending Lagoinha?"},
              {"gap_row_id": "g-lake", "question": "How long have you lived in Lake On The Park?"}]

    def _run(self, verdict):
        db = _Rows(list(self.QUEUED))
        with patch.object(rapport_gaps, "service_client", return_value=db), \
             patch("app.orchestrator.llm.llm_configured", return_value=True), \
             patch("app.orchestrator.llm.extractor_model", return_value="m"), \
             patch("app.orchestrator.llm.llm_json", return_value=verdict):
            n = rapport_gaps.close_gaps_told_in_chat("u1", "I've gone to Lagoinha for three years", "c1")
        return n, db

    def test_closes_only_questions_it_can_answer(self):
        n, db = self._run({"answerable": [{"n": 1, "answer": "about three years"}]})
        self.assertEqual(n, 1)
        self.assertEqual([u[0] for u in db.updates], ["g-church"])
        self.assertEqual(db.updates[0][1]["status"], "answered")

    def test_items_without_an_answer_or_out_of_range_close_nothing(self):
        n, db = self._run({"answerable": [{"n": 2, "answer": ""}, {"n": 9, "answer": "x"}, {"n": "x"}]})
        self.assertEqual((n, db.updates), (0, []))

    def test_chat_extraction_runs_the_closer_only_when_it_saved_something(self):
        for saved, allowed, expect in ((1, True, True), (0, True, False), (1, False, False)):
            with patch.object(claims_persist, "try_upsert_claims_from_message",
                              return_value=ClaimExtractResult(saved=saved, claim_ids=["c1"] if saved else [])), \
                 patch.object(rapport_gaps, "close_gaps_told_in_chat") as closer:
                claims_persist.extract_and_upsert_claims_from_message("u1", "I read history books", allow_rapport_gap=allowed)
            self.assertEqual(closer.called, expect, (saved, allowed))
            if expect:
                self.assertEqual(closer.call_args[0], ("u1", "I read history books", "c1"))


if __name__ == "__main__":
    unittest.main()
