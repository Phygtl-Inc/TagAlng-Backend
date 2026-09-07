"""Topic closeness scoring on _filter_events_by_query.

Groundwork. The model decides membership exactly as it always has, via match_indices —
this function applies no threshold and does not second-guess it. What is new is that the
model also RATES every event it was shown, matched or not, so a near-miss survives long
enough for the widening work to read.

Two failures shape most of what is pinned here, both found by live evals rather than by
review:

  * Match and closeness are DIFFERENT AXES. The model matched "Sunday jam night" for
    "violin" while scoring it 0.6, so a score threshold dropped an event the model had
    matched. MembershipIsTheModelsCallTests holds that line.

  * Parallel arrays MISALIGN. Asked for one mismatch per event, the model skipped the
    empty string for an exact match and returned four phrases for five events — every
    phrase landed on the event before it, and Lana would have told someone their violin
    recital was actually guitar. Ratings now name their own event; RatingAlignmentTests
    holds that line.

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


def _rate(index, mismatch="", score=0.0):
    """One ratings object as the model is asked to emit it."""
    return {"index": index, "mismatch": mismatch, "score": score}


class _LLMCase(unittest.TestCase):
    def _run(self, response, *, events=None, query="cricket"):
        """Run the filter with the model returning `response` (or raising it).

        Returns (matched, label, all_events) — the third is the caller's own input list,
        which is where every candidate's rating lands.
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


class RatingAlignmentTests(_LLMCase):
    """Each rating names its own event, so nothing the model does to the list can move a
    rating onto the wrong row."""

    def test_a_skipped_rating_leaves_a_gap_it_does_not_shift_the_rest(self):
        """THE regression. Under parallel arrays, omitting event 0's empty mismatch slid
        every later phrase one row up. Event 1 must keep its own phrase and event 0 must
        read as unjudged — never inherit its neighbour's."""
        _matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "ratings": [
                    _rate(1, "asked for cricket, this is a book club", 0.1),
                    _rate(2, "asked for cricket, this is basketball", 0.4),
                ],
                "label": "cricket",
            }
        )
        self.assertEqual([e["topic_score"] for e in rows], [0.0, 0.1, 0.4])
        self.assertEqual(rows[1]["topic_mismatch"], "asked for cricket, this is a book club")
        self.assertEqual(rows[2]["topic_mismatch"], "asked for cricket, this is basketball")
        self.assertEqual(rows[0]["topic_mismatch"], "")

    def test_ratings_out_of_order_land_on_the_right_events(self):
        _matched, _label, rows = self._run(
            {
                "match_indices": [],
                "ratings": [_rate(2, "c", 0.3), _rate(0, "a", 0.9), _rate(1, "b", 0.1)],
                "label": "",
            }
        )
        self.assertEqual([e["topic_score"] for e in rows], [0.9, 0.1, 0.3])
        self.assertEqual([e["topic_mismatch"] for e in rows], ["a", "b", "c"])

    def test_a_duplicate_index_keeps_the_first(self):
        """Deterministic, and it keeps a real rating rather than discarding both."""
        _matched, _label, rows = self._run(
            {
                "match_indices": [],
                "ratings": [_rate(1, "first", 0.7), _rate(1, "second", 0.2)],
                "label": "",
            }
        )
        self.assertEqual(rows[1]["topic_score"], 0.7)
        self.assertEqual(rows[1]["topic_mismatch"], "first")

    def test_an_out_of_range_index_is_dropped_without_touching_anyone_else(self):
        _matched, _label, rows = self._run(
            {
                "match_indices": [],
                "ratings": [_rate(99, "nowhere", 1.0), _rate(-1, "also nowhere", 1.0),
                            _rate(0, "here", 0.5)],
                "label": "",
            }
        )
        self.assertEqual([e["topic_score"] for e in rows], [0.5, 0.0, 0.0])
        self.assertEqual(rows[0]["topic_mismatch"], "here")

    def test_a_boolean_index_is_rejected(self):
        """bool is a subclass of int: True would otherwise silently claim event 1."""
        _matched, _label, rows = self._run(
            {
                "match_indices": [],
                "ratings": [_rate(True, "should not land", 0.9), _rate(1, "real", 0.3)],
                "label": "",
            }
        )
        self.assertEqual(rows[1]["topic_score"], 0.3)
        self.assertEqual(rows[1]["topic_mismatch"], "real")

    def test_malformed_entries_are_skipped_not_positionally_consumed(self):
        """A junk entry must not act as a placeholder that shifts the good ones."""
        _matched, _label, rows = self._run(
            {
                "match_indices": [],
                "ratings": ["nonsense", None, {"mismatch": "no index", "score": 1.0},
                            {"index": "0", "score": 1.0}, _rate(2, "real", 0.6)],
                "label": "",
            }
        )
        self.assertEqual([e["topic_score"] for e in rows], [0.0, 0.0, 0.6])
        self.assertEqual(rows[2]["topic_mismatch"], "real")


class EveryCandidateIsScoredTests(_LLMCase):
    def test_events_outside_match_indices_are_still_scored(self):
        """The point of the commit: a near-miss is rated even though it is not returned.
        Under the old contract it was dropped before anything could rate it."""
        matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "ratings": [
                    _rate(0, "", 1.0),
                    _rate(1, "not cricket at all", 0.1),
                    _rate(2, "asked for cricket, this is basketball", 0.7),
                ],
                "label": "cricket",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 0.1, 0.7])
        self.assertEqual(rows[2]["topic_mismatch"], "asked for cricket, this is basketball")

    def test_the_ratings_land_on_the_callers_own_list(self):
        """Requirement 4 with no new API: rows are stamped in place, so the caller
        already holds every scored candidate. A future widening caller reads its input."""
        rows = _events()
        matched, _label, same = self._run(
            {"match_indices": [0],
             "ratings": [_rate(0, "", 1.0), _rate(1, "x", 0.4), _rate(2, "y", 0.6)],
             "label": ""},
            events=rows,
        )
        self.assertIs(same, rows)
        self.assertTrue(all("topic_score" in e for e in rows))
        self.assertIs(matched[0], rows[0])

    def test_an_exact_match_carries_an_empty_mismatch(self):
        matched, _label, _rows = self._run(
            {"match_indices": [0],
             "ratings": [_rate(0, "", 1.0), _rate(1, "x"), _rate(2, "y")],
             "label": "cricket"}
        )
        self.assertEqual(matched[0]["topic_score"], 1.0)
        self.assertEqual(matched[0]["topic_mismatch"], "")

    def test_return_value_is_still_a_two_tuple(self):
        """Both callers destructure (matched, label); a third element would break them."""
        rows = _events()
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json",
            return_value={"match_indices": [0], "ratings": [_rate(0, "", 0.9)],
                          "label": "x"},
        ), patch("app.orchestrator.llm.router_model", return_value="m"):
            result = _filter_events_by_query(rows, "cricket")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], list)
        self.assertIsInstance(result[1], str)


class MembershipIsTheModelsCallTests(_LLMCase):
    """match_indices decides, the score has no vote. These pin the acceptance failure a
    0.9 threshold produced, so it cannot be reintroduced unnoticed."""

    def test_a_low_scored_event_the_model_matched_is_still_returned(self):
        """The real regression: "Sunday jam night" matched "violin" at 0.6. Any score
        threshold drops it; the model's judgement keeps it."""
        matched, _label, rows = self._run(
            {
                "match_indices": [1],
                "ratings": [_rate(0, "", 0.0),
                            _rate(1, "violin is only in the description", 0.6),
                            _rate(2, "", 0.0)],
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
                "ratings": [_rate(0, "", 1.0), _rate(1, "", 1.0), _rate(2, "", 1.0)],
                "label": "cricket",
            }
        )
        self.assertEqual([e["id"] for e in matched], ["e0"])
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 1.0, 1.0])

    def test_membership_order_follows_match_indices(self):
        matched, _label, _rows = self._run(
            {"match_indices": [2, 0], "ratings": [], "label": ""}
        )
        self.assertEqual([e["id"] for e in matched], ["e2", "e0"])

    def test_empty_match_indices_returns_nothing_however_high_the_scores(self):
        matched, label, rows = self._run(
            {"match_indices": [],
             "ratings": [_rate(0, "", 1.0), _rate(1, "", 1.0), _rate(2, "", 1.0)],
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

    def test_junk_ratings_do_not_discard_the_match_decision(self):
        matched, label, rows = self._run(
            {"match_indices": [0, 2], "ratings": "not a list", "label": "cricket"}
        )
        self.assertEqual([e["id"] for e in matched], ["e0", "e2"])
        self.assertEqual(label, "cricket")
        self.assertTrue(all(e["topic_score"] == 0.0 for e in rows))
        self.assertTrue(all(e["topic_mismatch"] == "" for e in rows))

    def test_missing_ratings_entirely_still_returns_the_matches(self):
        matched, _label, rows = self._run({"match_indices": [1], "label": "cricket"})
        self.assertEqual([e["id"] for e in matched], ["e1"])
        self.assertTrue(all(e["topic_score"] == 0.0 for e in rows))

    def test_more_ratings_than_events_are_ignored(self):
        matched, _label, rows = self._run(
            {"match_indices": [0],
             "ratings": [_rate(i, f"m{i}", 0.5) for i in range(9)],
             "label": "cricket"}
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual([e["topic_score"] for e in rows], [0.5, 0.5, 0.5])
        self.assertEqual([e["id"] for e in matched], ["e0"])

    def test_an_out_of_range_match_index_is_skipped_without_affecting_ratings(self):
        matched, _label, rows = self._run(
            {"match_indices": [99, 1],
             "ratings": [_rate(0, "a", 0.1), _rate(1, "b", 0.2), _rate(2, "c", 0.3)],
             "label": "cricket"}
        )
        self.assertEqual([e["id"] for e in matched], ["e1"])
        self.assertEqual([e["topic_score"] for e in rows], [0.1, 0.2, 0.3])

    def test_junk_score_values_read_as_unjudged_and_nulls_are_not_the_word_none(self):
        _matched, _label, rows = self._run(
            {
                "match_indices": [0],
                "ratings": [_rate(0, None, "high"), _rate(1, 12, None),
                            _rate(2, "  padded  ", float("nan"))],
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
            {"match_indices": [],
             "ratings": [_rate(0, "", 1.7), _rate(1, "", -3), _rate(2, "", 0.66)],
             "label": ""}
        )
        self.assertEqual([e["topic_score"] for e in rows], [1.0, 0.0, 0.7])

    def test_match_indices_of_the_wrong_type_falls_back(self):
        """No usable membership answer — that IS a reason to fall back."""
        matched, label, rows = self._run(
            {"match_indices": "0,1", "ratings": [_rate(0, "", 1.0)], "label": "x"},
            query="cricket",
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

    def test_an_unusable_response_is_logged_not_swallowed(self):
        """A parseable response we cannot use used to be indistinguishable from "the
        model rated everything 0.0" — both produced an all-zero, empty-label result. It
        cost a full eval cycle to tell those apart."""
        with self.assertLogs("app.activity_browse", level="WARNING") as logs:
            self._run({"match_indices": None, "label": "x"}, query="cricket")
        self.assertTrue(
            any("activity_browse_filter_unusable" in line for line in logs.output)
        )


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
        for band in ("0.8-1.0", "0.5-0.7", "0.2-0.4", "0.0-0.1"):
            self.assertIn(band, ab._TOPIC_SCORE_SCALE)
        seen = self._prompt()
        self.assertIn(ab._TOPIC_SCORE_SCALE, seen["system"])
        # A ratings object runs ~25 tokens, so a full _BROWSE_POOL of 40 needs ~1000 for
        # ratings alone. Truncation here is silent: it falls into the keyword fallback.
        self.assertGreaterEqual(seen["max_tokens"], 2000)
        # Exactly once. Pasting it twice is an easy edit to make and expensive to leave.
        self.assertEqual(seen["system"].count(ab._TOPIC_SCORE_SCALE), 1)

    def test_the_scale_asks_about_substitutability_not_resemblance(self):
        """The reframe that fixed a collapsed scale (eval 2026-09-06: only 0.0/0.6/0.9/
        1.0, beach volleyball at 0.0 against basketball). Grading resemblance is what
        produced that, so the word and its counter-example are pinned."""
        scale = ab._TOPIC_SCORE_SCALE
        self.assertIn("SUBSTITUTABILITY", scale)
        self.assertIn("water polo looks like basketball and substitutes badly", scale)

    def test_the_scale_carries_a_second_ladder_outside_sport(self):
        """A sports-only worked example teaches the model to read every request through a
        sports frame. The music ladder exists to break it, and stays until there is
        evidence about non-sport requests — which the alignment bug destroyed."""
        scale = ab._TOPIC_SCORE_SCALE
        self.assertIn("For 'basketball'", scale)
        self.assertIn("For 'violin recital'", scale)
        # And the bottom of the scale is not a dumping ground: a pottery workshop and a
        # startup pitch night both scored 0.0 against violin, which cannot both be right.
        self.assertIn("Never 0.0", scale)

    def test_the_model_is_asked_for_the_three_keys(self):
        seen = self._prompt()
        for key in ("match_indices", "ratings", "label"):
            self.assertIn(key, seen["system"])
            self.assertIn(key, seen["user_payload"])

    def test_each_rating_is_asked_to_name_its_own_event(self):
        """The structural fix. Without "index" in the object, ratings go back to being
        positional and a skipped entry silently shifts every one after it."""
        for text in (self._prompt()["system"], self._prompt()["user_payload"]):
            self.assertIn('"index"', text)

    def test_the_mismatch_is_asked_for_before_the_score(self):
        """Generation is autoregressive, so field order inside the object is causal:
        writing the comparison before the number is what stops the number collapsing
        onto the match verdict the model just emitted."""
        system = self._prompt()["system"]
        self.assertLess(system.index('"mismatch"'), system.index('"score"'))

    def test_match_indices_is_still_requested_first(self):
        """The mirror constraint: membership must generate BEFORE the ratings, or the
        ratings start driving what matches and Lana's output moves."""
        system = self._prompt()["system"]
        self.assertLess(system.index('"match_indices"'), system.index('"ratings"'))

    def test_the_membership_rules_are_unchanged_from_before_scoring(self):
        """Reproducing the model's old judgement means reproducing the old words. These
        sentences are verbatim from e76bcc9; paraphrasing them is what moved membership
        once already, so pin them rather than trusting review to notice."""
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

    def test_the_prompt_decouples_the_score_from_the_match(self):
        """The model emits match_indices first, so without being told otherwise it will
        restate that verdict as the score — which is exactly what the live eval showed."""
        system = self._prompt()["system"]
        self.assertIn("judge EVERY event you were shown", system)
        self.assertIn("answered independently of what you just matched", system)
        self.assertIn(
            "an event you matched may score low, and an event you did NOT match may "
            "score 0.5 or higher",
            system,
        )
        self.assertIn("never use only the ends of the scale", system)

    def test_the_prompt_forbids_skipping_a_rating(self):
        """The exact failure: the model skipped the empty mismatch for a perfect match."""
        self.assertIn("never skip one, not even for a perfect match", self._prompt()["system"])


if __name__ == "__main__":
    unittest.main()
