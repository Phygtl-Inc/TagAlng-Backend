"""Stretch offer picker and facts (product name: Rapport Reply).

pick_stretch reads the matcher's per-event judgement and nothing else; stretch_facts
turns one candidate into writer facts built only from the event row and the matcher's
own difference phrase. Pure functions — no model, no DB.
"""

import unittest
from unittest.mock import patch

from app.activity_browse import _STRETCH_BAND
from app.stretch_offer import StretchCandidate, pick_stretch, stretch_facts


def _row(eid, score, mismatch="asked for violin, this is a jam night", **kw):
    row = {
        "id": eid,
        "title": f"Event {eid}",
        "starts_at": "2026-09-27T18:00:00+00:00",
        "has_time": True,
        "topic_score": score,
        "topic_mismatch": mismatch,
    }
    row.update(kw)
    return row


class PickStretchTests(unittest.TestCase):
    def setUp(self):
        # Pure function, but the rule is "every new test patches llm_configured".
        p = patch("app.orchestrator.llm.llm_configured", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_every_score_in_the_band_qualifies(self):
        for score in (0.6, 0.7, 0.8):
            cand = pick_stretch([_row("e1", score)], _STRETCH_BAND)
            self.assertIsNotNone(cand, score)
            self.assertEqual(cand.score, score)

    def test_scores_outside_the_band_never_qualify(self):
        """0.5 is "loosely related"; 0.9+ is "same thing", rejected for a date, time or
        host constraint — not a topic stretch."""
        for score in (0.0, 0.3, 0.5, 0.9, 1.0):
            self.assertIsNone(pick_stretch([_row("e1", score)], _STRETCH_BAND), score)

    def test_the_highest_score_wins(self):
        rows = [_row("e1", 0.6), _row("e2", 0.8), _row("e3", 0.7)]
        self.assertEqual(pick_stretch(rows, _STRETCH_BAND).event_id, "e2")

    def test_ties_keep_input_order(self):
        rows = [_row("e1", 0.7), _row("e2", 0.7)]
        self.assertEqual(pick_stretch(rows, _STRETCH_BAND).event_id, "e1")

    def test_an_empty_difference_phrase_disqualifies(self):
        """No phrase means no reason Lana is allowed to give."""
        for mismatch in ("", "   ", None):
            self.assertIsNone(pick_stretch([_row("e1", 0.7, mismatch)], _STRETCH_BAND))

    def test_an_unchecked_row_never_qualifies(self):
        self.assertIsNone(
            pick_stretch([_row("e1", 0.7, topic_unchecked=True)], _STRETCH_BAND)
        )

    def test_rows_with_no_usable_score_are_skipped(self):
        rows = [_row("e1", None), _row("e2", "0.7"), _row("e3", True)]
        self.assertIsNone(pick_stretch(rows, _STRETCH_BAND))

    def test_a_row_without_an_id_or_title_cannot_be_carded(self):
        self.assertIsNone(pick_stretch([_row("", 0.7)], _STRETCH_BAND))
        self.assertIsNone(pick_stretch([_row("e1", 0.7, title="  ")], _STRETCH_BAND))

    def test_a_float_just_off_the_edge_is_read_as_its_rounded_score(self):
        self.assertIsNotNone(pick_stretch([_row("e1", 0.6000000001)], _STRETCH_BAND))

    def test_nothing_in_nothing_out(self):
        self.assertIsNone(pick_stretch([], _STRETCH_BAND))
        self.assertIsNone(pick_stretch(None, _STRETCH_BAND))

    def test_the_candidate_keeps_the_row_and_the_phrase(self):
        row = _row("e1", 0.7, mismatch="  asked for violin,   this is a jam night ")
        cand = pick_stretch([row], _STRETCH_BAND)
        self.assertIs(cand.event, row)
        self.assertEqual(cand.mismatch, "asked for violin, this is a jam night")


class StretchFactsTests(unittest.TestCase):
    def setUp(self):
        p = patch("app.orchestrator.llm.llm_configured", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def _cand(self, **kw):
        row = _row("e1", 0.7, title="Sunday jam night",
                   description="Bring an instrument, all levels.",
                   cohort_tags=["lifestyle_social"], **kw)
        return StretchCandidate(event=row, score=0.7, mismatch=row["topic_mismatch"])

    def test_the_phrase_is_carried_verbatim(self):
        facts = "\n".join(stretch_facts(self._cand()))
        self.assertIn('"asked for violin, this is a jam night"', facts)
        self.assertIn("ONLY reason", facts)

    def test_only_fields_of_the_event_row_appear(self):
        """Every piece of content in the facts traces to the row: title, date,
        description, tags, phrase. Venue, host and distance are not in the contract."""
        cand = self._cand(venue_name="Secret Venue", host_name="Asjid",
                          distance_meters=1234.0)
        facts = "\n".join(stretch_facts(cand))
        self.assertIn('"Sunday jam night"', facts)
        self.assertIn("Bring an instrument, all levels.", facts)
        self.assertIn("lifestyle_social", facts)
        self.assertIn("2026-09-27", facts)
        for absent in ("Secret Venue", "Asjid", "1234"):
            self.assertNotIn(absent, facts)

    def test_it_is_called_the_closest_thing_never_a_match(self):
        facts = "\n".join(stretch_facts(self._cand()))
        self.assertIn("closest thing, never a match", facts)
        self.assertIn("Add nothing about who will be there", facts)

    def test_the_description_is_capped(self):
        cand = self._cand()
        cand.event["description"] = "x" * 500
        desc_fact = [f for f in stretch_facts(cand) if f.startswith("Its own description")][0]
        self.assertEqual(desc_fact.count("x"), 200)

    def test_missing_optional_fields_leave_no_empty_sentences(self):
        row = {"id": "e1", "title": "Sunday jam night", "topic_score": 0.7,
               "topic_mismatch": "asked for violin, this is a jam night"}
        facts = stretch_facts(StretchCandidate(event=row, score=0.7, mismatch=row["topic_mismatch"]))
        self.assertFalse(any("description" in f.lower() and '""' in f for f in facts))
        self.assertFalse(any(f.startswith("Its tags") for f in facts))


if __name__ == "__main__":
    unittest.main()
