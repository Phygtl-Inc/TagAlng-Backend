"""Topic closeness scoring on _filter_events_by_query.

Groundwork. The model decides membership exactly as it always has, via match_indices —
this function applies no threshold and does not second-guess it. What is new is that the
model also RATES every event it was shown, matched or not, so a near-miss survives long
enough for the widening work to read. Under the old contract non-matches were deleted
before anything could rate them.

The separation matters and is the thing most likely to be broken later: match and
closeness are different axes. A real eval (2026-09-06) had the model match "Sunday jam
night" for "violin" while scoring it 0.6 — so a score threshold, tried and reverted,
dropped an event the model had matched. MembershipIsTheModelsCallTests pins that.

llm_configured/llm_json are imported INSIDE _filter_events_by_query, so they are patched
on their source module (app.orchestrator.llm), not on app.activity_browse.
"""

import unittest
from unittest.mock import patch

import app.activity_browse as ab
from app.activity_browse import _filter_events_by_query

_EVENTS = [
    {"id": "e0", "title": "Sunday Cricket", "cohort_tags": ["sports"]},
    {"id": "e1", "title": "Book club", "cohort_tags": ["social"]},
    {"id": "e2", "title": "Pickup basketball", "cohort_tags": ["sports"]},
]


def _events():
    """Fresh dicts per test — the matcher stamps rows IN PLACE."""
    return [dict(e) for e in _EVENTS]


class _LLMCase(unittest.TestCase):
    def _run(self, response, *, events=None, query="cricket"):
        """Run the filter with the model returning `response` (or raising it).

        Returns (matched, label, all_events) — the third is the caller's own input list,
        which is where every candidate's score lands.
        """
        rows = _events() if events is None else events
        kwargs = (
            {"side_effect": response}
            if isinstance(response, BaseException)
            else {"return_value": response}
        )
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", **kwargs
        ), patch("app.orchestrator.llm.router_model", return_value="m"):
            matched, label = _filter_events_by_query(rows, query)
        return matched, label, rows


class EveryCandidateIsScoredTests(_LLMCase):
    def test_events_outside_match_indices_are_still_scored(self):
        """The point of the commit: a near-miss is rated even though it is not returned.
        Under the old contract it was dropped before anything could rate it."""
        matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "scores": [1.0, 0.1, 0.7],
                "mismatches": ["", "not cricket at all", "asked for cricket, this is basketball"],
                "label": "cricket",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 0.1, 0.7])
        self.assertEqual(rows[2]["topic_mismatch"], "asked for cricket, this is basketball")

    def test_scores_are_parallel_to_the_event_list_not_to_match_indices(self):
        """The alignment bug this contract is shaped to prevent. match_indices names one
        event; the scores still describe events 0, 1, 2 in order."""
        _matched, _label, rows = self._run(
            {
                "match_indices": [2],
                "scores": [0.2, 0.5, 0.9],
                "mismatches": ["a", "b", "c"],
                "label": "cricket",
            }
        )
        self.assertEqual([e["topic_score"] for e in rows], [0.2, 0.5, 0.9])
        self.assertEqual([e["topic_mismatch"] for e in rows], ["a", "b", "c"])

    def test_the_scores_land_on_the_callers_own_list(self):
        """Requirement 4 with no new API: rows are stamped in place, so the caller
        already holds every scored candidate. A future widening caller reads its input."""
        rows = _events()
        matched, _label, same = self._run(
            {"match_indices": [0], "scores": [1.0, 0.4, 0.6], "mismatches": ["", "", ""],
             "label": ""},
            events=rows,
        )
        self.assertIs(same, rows)
        self.assertTrue(all("topic_score" in e for e in rows))
        self.assertIs(matched[0], rows[0])

    def test_an_exact_match_carries_an_empty_mismatch(self):
        matched, _label, _rows = self._run(
            {"match_indices": [0], "scores": [1.0, 0.0, 0.0], "mismatches": ["", "x", "y"],
             "label": "cricket"}
        )
        self.assertEqual(matched[0]["topic_score"], 1.0)
        self.assertEqual(matched[0]["topic_mismatch"], "")

    def test_return_value_is_still_a_two_tuple(self):
        """Both callers destructure (matched, label); a third element would break them."""
        rows = _events()
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json",
            return_value={"match_indices": [0], "scores": [0.9], "mismatches": [""],
                          "label": "x"},
        ), patch("app.orchestrator.llm.router_model", return_value="m"):
            result = _filter_events_by_query(rows, "cricket")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], list)
        self.assertIsInstance(result[1], str)


class MembershipIsTheModelsCallTests(_LLMCase):
    """match_indices decides, the score has no vote. These pin the acceptance failure
    that a 0.9 threshold produced, so it cannot be reintroduced unnoticed."""

    def test_a_low_scored_event_the_model_matched_is_still_returned(self):
        """The real regression: "Sunday jam night" matched "violin" at 0.6. Any score
        threshold drops it; the model's judgement keeps it."""
        matched, _label, rows = self._run(
            {
                "match_indices": [1],
                "scores": [0.0, 0.6, 0.0],
                "mismatches": ["", "violin is only in the description", ""],
                "label": "violin",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e1"])
        self.assertEqual(matched[0]["topic_score"], 0.6)
        self.assertEqual(rows[1]["topic_mismatch"], "violin is only in the description")

    def test_a_high_scored_event_the_model_did_not_match_is_not_returned(self):
        """The mirror case: a 1.0 outside match_indices stays out. The score describes,
        it does not admit."""
        matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "scores": [1.0, 1.0, 1.0],
                "mismatches": ["", "", ""],
                "label": "cricket",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 1.0, 1.0])

    def test_membership_order_follows_match_indices(self):
        matched, _label, _rows = self._run(
            {"match_indices": [2, 0], "scores": [0.9, 0.1, 0.9], "mismatches": ["", "", ""],
             "label": ""}
        )
        self.assertEqual([e["id"] for e in matched], ["e2", "e0"])

    def test_empty_match_indices_returns_nothing_however_high_the_scores(self):
        matched, label, rows = self._run(
            {"match_indices": [], "scores": [1.0, 1.0, 1.0], "mismatches": ["", "", ""],
             "label": "cricket"}
        )
        self.assertEqual(matched, [])
        self.assertEqual(label, "cricket")
        # Still rated, though — that is what the widening work will read.
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 1.0, 1.0])

    def test_there_is_no_score_threshold_constant(self):
        """A threshold here reads the wrong axis. Reintroducing one is the widening
        work's call, with a consumer in hand — not this module's."""
        self.assertFalse(hasattr(ab, "_TOPIC_MATCH_THRESHOLD"))


class RaggedResponseTests(_LLMCase):
    """A model that answers badly must never crash, never misalign, and never cost us a
    valid membership answer."""

    def test_junk_scores_do_not_discard_the_match_decision(self):
        """scores being unusable is not a reason to throw away match_indices. Everything
        reads 0.0; the right events still come back."""
        matched, label, rows = self._run(
            {"match_indices": [0, 2], "scores": "0.9", "mismatches": {"a": 1},
             "label": "cricket"}
        )
        self.assertEqual([e["id"] for e in matched], ["e0", "e2"])
        self.assertEqual(label, "cricket")
        self.assertTrue(all(e["topic_score"] == 0.0 for e in rows))
        self.assertTrue(all(e["topic_mismatch"] == "" for e in rows))

    def test_missing_scores_entirely_still_returns_the_matches(self):
        matched, _label, rows = self._run({"match_indices": [1], "label": "cricket"})
        self.assertEqual([e["id"] for e in matched], ["e1"])
        self.assertTrue(all(e["topic_score"] == 0.0 for e in rows))

    def test_a_short_scores_array_leaves_the_tail_unscored(self):
        matched, _label, rows = self._run(
            {"match_indices": [0], "scores": [1.0], "mismatches": [""], "label": "cricket"}
        )
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 0.0, 0.0])
        self.assertEqual([e["topic_mismatch"] for e in rows], ["", "", ""])

    def test_a_long_scores_array_is_ignored_past_the_end(self):
        matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "scores": [1.0, 0.0, 0.0, 1.0, 1.0],
                "mismatches": ["", "", "", "extra", "extra"],
                "label": "cricket",
            }
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual([e["id"] for e in matched], ["e0"])

    def test_an_out_of_range_index_is_skipped_without_affecting_scores(self):
        matched, _label, rows = self._run(
            {"match_indices": [99, 1], "scores": [0.1, 0.2, 0.3], "mismatches": ["a", "b", "c"],
             "label": "cricket"}
        )
        self.assertEqual([e["id"] for e in matched], ["e1"])
        self.assertEqual([e["topic_score"] for e in rows], [0.1, 0.2, 0.3])

    def test_junk_score_values_read_as_unjudged_and_nulls_are_not_the_word_none(self):
        _matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "scores": ["high", None, float("nan")],
                "mismatches": [None, 12, "  padded  "],
                "label": "",
            }
        )
        self.assertEqual([e["topic_score"] for e in rows], [0.0, 0.0, 0.0])
        # A JSON null must render as "", never as the literal string "None".
        self.assertEqual(rows[0]["topic_mismatch"], "")
        self.assertEqual(rows[1]["topic_mismatch"], "12")
        self.assertEqual(rows[2]["topic_mismatch"], "padded")

    def test_out_of_range_scores_are_clamped_and_rounded(self):
        _matched, _label, rows = self._run(
            {"match_indices": [], "scores": [1.7, -3, 0.66], "mismatches": [], "label": ""}
        )
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 0.0, 0.7])

    def test_match_indices_of_the_wrong_type_falls_back(self):
        """No usable membership answer — that IS a reason to fall back."""
        matched, label, rows = self._run(
            {"match_indices": "0,1", "scores": [1.0, 1.0, 1.0], "label": "x"}, query="cricket"
        )
        self.assertEqual([e["id"] for e in matched], ["e0"])  # keyword hit on "cricket"
        self.assertEqual(label, "")
        self.assertTrue(all(e["topic_score"] == 0.0 for e in rows))

    def test_a_non_dict_response_falls_back_without_crashing(self):
        matched, label, _rows = self._run(["not", "a", "dict"], query="cricket")
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual(matched[0]["topic_score"], 0.0)
        self.assertEqual(label, "")

    def test_a_raising_model_falls_back_without_crashing(self):
        matched, label, _rows = self._run(RuntimeError("truncated"), query="cricket")
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual(matched[0]["topic_score"], 0.0)
        self.assertEqual(label, "")


class UnjudgedPathTests(unittest.TestCase):
    """Every path out of the matcher scores its rows — all of them, not just the ones
    returned. An unscored row reaching a caller that ranks by score would sort as well as
    a perfect match, and one reading near-misses would hit a KeyError."""

    def test_keyword_fallback_yields_zero(self):
        rows = _events()
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            matched, label = _filter_events_by_query(rows, "cricket")
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual(label, "")
        # Including the rows that were NOT returned.
        self.assertTrue(all(e["topic_score"] == 0.0 for e in rows))
        self.assertTrue(all(e["topic_mismatch"] == "" for e in rows))

    def test_keyword_fallback_showing_everything_still_scores_zero(self):
        """Nothing matched → show all. "All" is not "all perfect"."""
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            matched, _ = _filter_events_by_query(_events(), "underwater basket weaving")
        self.assertEqual(len(matched), 3)
        self.assertTrue(all(e["topic_score"] == 0.0 for e in matched))

    def test_vague_query_path_yields_zero(self):
        """An open request returns everything, but nothing was judged — there was no
        topic to judge against."""
        matched, label = _filter_events_by_query(_events(), "what's happening")
        self.assertEqual(len(matched), 3)
        self.assertTrue(all(e["topic_score"] == 0.0 for e in matched))
        self.assertTrue(all(e["topic_mismatch"] == "" for e in matched))
        self.assertEqual(label, "")

    def test_empty_query_path_yields_zero(self):
        matched, _ = _filter_events_by_query(_events(), "")
        self.assertEqual(len(matched), 3)
        self.assertTrue(all(e["topic_score"] == 0.0 for e in matched))

    def test_no_events_is_still_an_empty_answer(self):
        self.assertEqual(_filter_events_by_query([], "cricket"), ([], ""))


class PromptContractTests(unittest.TestCase):
    def _prompt(self):
        seen = {}
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json",
            side_effect=lambda **kw: seen.update(kw) or {"match_indices": [], "label": ""},
        ), patch("app.orchestrator.llm.router_model", return_value="m"):
            _filter_events_by_query(_events(), "cricket")
        return seen

    def test_the_scale_is_a_named_constant_carried_into_the_prompt(self):
        for band in ("0.9-1.0", "0.6-0.8", "0.3-0.5", "0.0-0.2"):
            self.assertIn(band, ab._TOPIC_SCORE_SCALE)
        seen = self._prompt()
        self.assertIn(ab._TOPIC_SCORE_SCALE, seen["system"])
        self.assertGreaterEqual(seen["max_tokens"], 1200)

    def test_the_model_is_asked_for_all_four_keys(self):
        seen = self._prompt()
        for key in ("match_indices", "scores", "mismatches", "label"):
            self.assertIn(key, seen["system"])
            self.assertIn(key, seen["user_payload"])

    def test_the_membership_rules_are_unchanged_from_before_scoring(self):
        """Reproducing the model's old judgement means reproducing the old words. These
        sentences are verbatim from e76bcc9; paraphrasing them is what moved membership
        the last time, so pin them rather than trusting review to notice."""
        system = self._prompt()["system"]
        for sentence in (
            "indices of events satisfying EVERY constraint the request expresses "
            "(a date query must match the event's date; a time-of-day query the start "
            "time; a host query the host).",
            "the TOPIC is a hard constraint too: only events that are genuinely that "
            "kind of activity match — a matching date or time of day alone NEVER "
            "qualifies an unrelated event (a coffee catch-up is not a match for "
            "'runners', even at the right hour).",
            "If open, return all indices. Empty match_indices if nothing fits.",
        ):
            self.assertIn(sentence, system)

    def test_the_prompt_says_the_score_decides_nothing(self):
        """The model must not start withholding low scores from match_indices to be
        helpful — that would re-couple the two axes inside the model instead of here."""
        system = self._prompt()["system"]
        self.assertIn("rate EVERY event you were shown", system)
        self.assertIn("match_indices alone says what matched", system)


if __name__ == "__main__":
    unittest.main()
