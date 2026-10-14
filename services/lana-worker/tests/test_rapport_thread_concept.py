"""A rapport question records the claim it deepens by that claim's CONCEPT, and an answer
to it stays on that thread.

Prod, 2026-10-12 (Tommaso, handle todiba): asked "which pizzeria do you get your fig and
gorgonzola pizza from?" after answering two fig-pizza-at-Pausa questions in September.

1. The extractor's follow-up gap carried no deepens_concept, only gap_id
   'deepen:<slug of the LABEL>' (likes_fig_and_gorgonzola_pizza). The claim's concept is
   fig_gorgonzola_pizza_fan, so coverage never saw the thread as asked.
2. A bare answer ("Pausa in San Mateo, CA") holds no fact about the user on its own; the
   extractor saved nothing, the gap closed unlinked, and the place was lost.
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service")

from app import claims_persist  # noqa: E402
from app.claims_persist import ClaimExtractResult  # noqa: E402
from app.vertex_extract import _name_context_block  # noqa: E402

FIG = {"id": "fig-claim", "concept": "fig_gorgonzola_pizza_fan",
       "label": "Likes fig and gorgonzola pizza", "details": []}


def _extracted(followup="Where do you usually get it?"):
    return {
        "claims": [{
            "concept": "fig_gorgonzola_pizza_fan", "label": "Likes fig and gorgonzola pizza",
            "confidence": 0.9, "disclosure": "public", "synonyms": ["pizza"], "details": [],
            "source_quote": "I love the pizza with figs, gorgonzola and caramelized onions",
            "bucket": "interest",
        }],
        "followup_question": followup,
    }


def _run_extract(data, *, threads=None, asked_concept=None, already_asked=False, thread=None):
    """Drive try_upsert_claims_from_message with the DB edges stubbed; return the spies."""
    spies = SimpleNamespace(opened=MagicMock(return_value=True), extract=MagicMock(return_value=data))
    with patch.object(claims_persist, "current_nickname", return_value="Tommaso"), \
         patch.object(claims_persist, "persist_nickname_if_stated", return_value=None), \
         patch.object(claims_persist, "fetch_active_claim_threads", return_value=list(threads or [])), \
         patch.object(claims_persist, "incremental_claims_from_utterance", spies.extract), \
         patch.object(claims_persist, "apply_subject_updates"), \
         patch("app.circles_capture.run_circle_capture", return_value={}), \
         patch.object(claims_persist, "persist_kids_count"), \
         patch.object(claims_persist, "persist_role_gender"), \
         patch.object(claims_persist, "upsert_claims", side_effect=lambda u, c, written=None: (written.append(("fig-claim", 0.9)), 1)[1]), \
         patch("app.rapport_gaps.open_semantic_gap", spies.opened), \
         patch("app.rapport_gaps.thread_already_asked", return_value=already_asked), \
         patch("app.rapport_gaps.thread_claim", return_value=thread), \
         patch("app.rapport_gaps.recent_gap_questions", return_value=[]):
        spies.result = claims_persist.try_upsert_claims_from_message(
            "u1", "I love the pizza with figs, gorgonzola and caramelized onions",
            asked_concept=asked_concept,
        )
    return spies


class TestFollowUpRecordsTheClaimConcept(unittest.TestCase):
    def test_follow_up_gap_carries_the_claim_concept_not_the_label_slug(self):
        spies = _run_extract(_extracted())
        spies.opened.assert_called_once()
        self.assertEqual(spies.opened.call_args.kwargs["deepens_concept"], "fig_gorgonzola_pizza_fan")

    def test_follow_up_skipped_when_the_thread_was_already_asked(self):
        spies = _run_extract(_extracted(), already_asked=True)
        spies.opened.assert_not_called()

    def test_follow_up_opens_when_the_thread_is_new(self):
        spies = _run_extract(_extracted(), already_asked=False)
        spies.opened.assert_called_once()


class TestAnswerIsReadAgainstItsThread(unittest.TestCase):
    def test_asked_thread_reaches_the_extractor_and_its_thread_list(self):
        spies = _run_extract(_extracted(None), threads=[], asked_concept="fig_gorgonzola_pizza_fan", thread=FIG)
        args, kwargs = spies.extract.call_args
        self.assertEqual(kwargs["asked_thread"], FIG)
        # Thread list capped at 40 rows: the answered thread is added when missing.
        self.assertIn(FIG, args[1])

    def test_no_asked_concept_means_no_thread(self):
        spies = _run_extract(_extracted(None), threads=[])
        self.assertIsNone(spies.extract.call_args.kwargs["asked_thread"])

    def test_prompt_names_the_thread_only_when_given(self):
        q = "Which local pizzeria do you get your fig and gorgonzola pizza from?"
        with_thread = _name_context_block("Tommaso", q, FIG)
        self.assertIn("THAT QUESTION IS ABOUT THEIR THREAD: fig_gorgonzola_pizza_fan", with_thread)
        self.assertNotIn("THAT QUESTION IS ABOUT", _name_context_block("Tommaso", q))


class TestRecordAnswerLinksTheThread(unittest.TestCase):
    def _answer(self, res, gap_row, thread):
        from app import main

        marked, seen = {}, {}
        with patch.object(main, "verify_auth", return_value=SimpleNamespace(user_id="u1", is_anonymous=False)), \
             patch("app.rapport_gaps.get_gap_row", return_value=gap_row), \
             patch("app.rapport_gaps.thread_claim", return_value=thread), \
             patch.object(main, "try_upsert_claims_from_message",
                          side_effect=lambda *a, **k: (seen.update(k), res)[1]), \
             patch.object(main, "rapport_reconcile_gaps"), \
             patch("app.claims_persist.claim_ids_created_since", return_value=[]), \
             patch("app.circles_flow.tag_claim_place_from_gap"), \
             patch.object(main, "rapport_mark_answered",
                          side_effect=lambda g, answer_claim_id=None, answer_text=None: marked.update(id=answer_claim_id, text=answer_text)), \
             patch.object(main, "amplitude_track"):
            main.post_rapport_record_answer(
                main.RapportAnswerBody(gap_row_id="g1", text="Pausa in San Mateo, CA"), MagicMock(), "Bearer x"
            )
        return marked, seen

    GAP = {"question": "Which local pizzeria do you get your fig and gorgonzola pizza from?",
           "deepens_concept": "fig_gorgonzola_pizza_fan"}

    def test_bare_place_answer_links_the_thread_it_answers(self):
        """The todiba case: nothing extracted, the gap still links the fig-pizza thread."""
        marked, seen = self._answer(ClaimExtractResult(saved=0), self.GAP, FIG)
        self.assertEqual(marked["id"], "fig-claim")
        self.assertEqual(marked["text"], "Pausa in San Mateo, CA")
        self.assertEqual(seen["asked_concept"], "fig_gorgonzola_pizza_fan")

    def test_written_claim_still_wins(self):
        marked, _ = self._answer(ClaimExtractResult(saved=1, claim_ids=["new-claim"]), self.GAP, FIG)
        self.assertEqual(marked["id"], "new-claim")

    def test_gap_without_a_thread_stays_unlinked(self):
        marked, _ = self._answer(ClaimExtractResult(saved=0), {"question": "q"}, FIG)
        self.assertIsNone(marked["id"])


if __name__ == "__main__":
    unittest.main()
