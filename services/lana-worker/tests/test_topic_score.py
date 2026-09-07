"""Topic closeness scoring on _filter_events_by_query.

Groundwork commit: every event the matcher returns carries `topic_score` (0.0-1.0) and
`topic_mismatch` (how it differs, "" when exact). Nothing consumes them yet, so the bar
here is the invariant itself — anything returned is scored, a model that answers badly
never crashes or silently drops a match, and an UNJUDGED row scores 0.0 rather than
passing for a good one.

llm_configured/llm_json are imported INSIDE _filter_events_by_query, so they are patched
on their source module (app.orchestrator.llm), not on app.activity_browse.
"""

import unittest
from unittest.mock import patch

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
        """Run the filter with the model returning `response` (or raising it)."""
        rows = _events() if events is None else events
        kwargs = (
            {"side_effect": response}
            if isinstance(response, BaseException)
            else {"return_value": response}
        )
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", **kwargs
        ), patch("app.orchestrator.llm.router_model", return_value="m"):
            return _filter_events_by_query(rows, query)


class TopicScoreAttachTests(_LLMCase):
    def test_scores_and_mismatches_land_on_the_right_events(self):
        """Alignment is POSITIONAL against match_indices, not by event index — so an
        out-of-order response must not smear the scores onto the wrong rows."""
        matched, label = self._run(
            {
                "match_indices": [2, 0],
                "scores": [0.7, 1.0],
                "mismatches": ["asked for cricket, this is basketball", ""],
                "label": "cricket",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e2", "e0"])
        self.assertEqual(matched[0]["topic_score"], 0.7)
        self.assertEqual(
            matched[0]["topic_mismatch"], "asked for cricket, this is basketball"
        )
        self.assertEqual(matched[1]["topic_score"], 1.0)
        self.assertEqual(label, "cricket")

    def test_an_exact_match_carries_an_empty_mismatch(self):
        matched, _ = self._run(
            {"match_indices": [0], "scores": [1.0], "mismatches": [""], "label": "cricket"}
        )
        self.assertEqual(matched[0]["topic_score"], 1.0)
        self.assertEqual(matched[0]["topic_mismatch"], "")

    def test_return_value_is_still_a_two_tuple(self):
        """Both callers destructure (matched, label); a third element would break them."""
        result = self._run(
            {"match_indices": [0], "scores": [0.9], "mismatches": [""], "label": "x"}
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], list)
        self.assertIsInstance(result[1], str)

    def test_an_out_of_range_index_is_skipped_without_shifting_the_rest(self):
        """Index 99 is dropped, but score 0.4 belongs to it — e1 must still get 0.2."""
        matched, _ = self._run(
            {
                "match_indices": [99, 1],
                "scores": [0.4, 0.2],
                "mismatches": ["ignored", "not cricket"],
                "label": "cricket",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e1"])
        self.assertEqual(matched[0]["topic_score"], 0.2)
        self.assertEqual(matched[0]["topic_mismatch"], "not cricket")


class RaggedResponseTests(_LLMCase):
    """A model that answers badly must never crash and never drop a match."""

    def test_scores_shorter_than_match_indices_defaults_the_tail(self):
        matched, _ = self._run(
            {"match_indices": [0, 1, 2], "scores": [0.9], "mismatches": [""], "label": ""}
        )
        self.assertEqual([e["id"] for e in matched], ["e0", "e1", "e2"])
        self.assertEqual([e["topic_score"] for e in matched], [0.9, 0.0, 0.0])
        self.assertEqual([e["topic_mismatch"] for e in matched], ["", "", ""])

    def test_missing_arrays_entirely(self):
        matched, _ = self._run({"match_indices": [0, 1], "label": "cricket"})
        self.assertEqual(len(matched), 2)
        self.assertTrue(all(e["topic_score"] == 0.0 for e in matched))
        self.assertTrue(all(e["topic_mismatch"] == "" for e in matched))

    def test_arrays_of_the_wrong_type(self):
        matched, _ = self._run(
            {"match_indices": [0], "scores": "0.9", "mismatches": {"a": 1}, "label": ""}
        )
        self.assertEqual(matched[0]["topic_score"], 0.0)
        self.assertEqual(matched[0]["topic_mismatch"], "")

    def test_junk_score_values_read_as_unjudged_and_nulls_are_not_the_word_none(self):
        matched, _ = self._run(
            {
                "match_indices": [0, 1, 2],
                "scores": ["high", None, float("nan")],
                "mismatches": [None, 12, "  padded  "],
                "label": "",
            }
        )
        self.assertEqual([e["topic_score"] for e in matched], [0.0, 0.0, 0.0])
        # A JSON null must render as "", never as the literal string "None".
        self.assertEqual(matched[0]["topic_mismatch"], "")
        self.assertEqual(matched[1]["topic_mismatch"], "12")
        self.assertEqual(matched[2]["topic_mismatch"], "padded")

    def test_out_of_range_scores_are_clamped_and_rounded(self):
        matched, _ = self._run(
            {"match_indices": [0, 1, 2], "scores": [1.7, -3, 0.66], "mismatches": [], "label": ""}
        )
        self.assertEqual([e["topic_score"] for e in matched], [1.0, 0.0, 0.7])

    def test_a_non_dict_response_falls_back_without_crashing(self):
        matched, label = self._run(["not", "a", "dict"], query="cricket")
        # Keyword fallback: "cricket" hits e0's title.
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual(matched[0]["topic_score"], 0.0)
        self.assertEqual(label, "")

    def test_a_raising_model_falls_back_without_crashing(self):
        matched, label = self._run(RuntimeError("truncated"), query="cricket")
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual(matched[0]["topic_score"], 0.0)
        self.assertEqual(label, "")

    def test_match_indices_of_the_wrong_type_falls_back(self):
        matched, _ = self._run({"match_indices": "0,1", "label": "x"}, query="cricket")
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertTrue(all("topic_score" in e for e in matched))


class UnjudgedPathTests(unittest.TestCase):
    """Every path out of the matcher scores its rows. An unscored row reaching a caller
    that ranks by score would sort as well as a perfect match."""

    def test_keyword_fallback_yields_zero(self):
        with patch("app.orchestrator.llm.llm_configured", return_value=False):
            matched, label = _filter_events_by_query(_events(), "cricket")
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual(matched[0]["topic_score"], 0.0)
        self.assertEqual(matched[0]["topic_mismatch"], "")
        self.assertEqual(label, "")

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


class ScaleConstantTests(unittest.TestCase):
    def test_the_scale_is_a_named_constant_carried_into_the_prompt(self):
        """The bands are tuned in one place; the prompt must actually carry them."""
        from app.activity_browse import _TOPIC_SCORE_SCALE

        for band in ("0.9-1.0", "0.6-0.8", "0.3-0.5", "0.0-0.2"):
            self.assertIn(band, _TOPIC_SCORE_SCALE)

        seen = {}
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json",
            side_effect=lambda **kw: seen.update(kw) or {"match_indices": [], "label": ""},
        ), patch("app.orchestrator.llm.router_model", return_value="m"):
            _filter_events_by_query(_events(), "cricket")

        self.assertIn(_TOPIC_SCORE_SCALE, seen["system"])
        # Sized for scores + mismatch phrases across a full _BROWSE_POOL of 40.
        self.assertGreaterEqual(seen["max_tokens"], 1200)


if __name__ == "__main__":
    unittest.main()
