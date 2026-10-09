"""Far cards (prod, 2026-10-07, pill = Islamabad): "any informative meet nearby" led with a
garden volunteer day over a language exchange, and said "2,422 miles away" — measured
from her Orlando home area, while the copy read "nearby"."""

import unittest
from unittest.mock import patch

from app.activity_browse import _compose_empty_seek_offer, _rank_far_matches


def _row(title, score, dist):
    return {"title": title, "topic_score": score, "distance_meters": dist}


class FarRankTests(unittest.TestCase):
    def test_near_misses_rank_by_topic_not_by_a_few_metres(self):
        rows = [
            _row("Alumni Garden Volunteer Day", 0.62, 3_897_000),
            _row("Career Networking Mixer", 0.70, 3_898_000),
            _row("Language Exchange Club", 0.78, 3_899_000),
        ]
        self.assertEqual(
            [r["title"] for r in _rank_far_matches(rows)],
            ["Language Exchange Club", "Career Networking Mixer", "Alumni Garden Volunteer Day"],
        )

    def test_exact_matches_still_lead_nearest_first(self):
        rows = [
            _row("Near miss", 0.85, 10_000),
            _row("Exact far", 0.95, 900_000),
            _row("Exact near", 0.92, 50_000),
        ]
        self.assertEqual(
            [r["title"] for r in _rank_far_matches(rows)],
            ["Exact near", "Exact far", "Near miss"],
        )

    def test_equal_topic_falls_back_to_distance(self):
        rows = [_row("Far", 0.7, 9_000), _row("Near", 0.7, 1_000)]
        self.assertEqual([r["title"] for r in _rank_far_matches(rows)], ["Near", "Far"])


class FarOriginTests(unittest.TestCase):
    def _payload(self, **kw):
        captured: dict = {}

        def _llm_json(**k):
            captured.update(k)
            return {"message": "ok"}

        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", _llm_json
        ), patch("app.orchestrator.llm.synthesizer_model", return_value="m"):
            _compose_empty_seek_offer("informative", lang="en", **kw)
        return captured["user_payload"]

    _LEAD = {"title": "Language Exchange Club", "miles": 2422, "area_label": "San Jose (95112)"}

    def test_distance_is_said_to_be_from_the_searched_area(self):
        payload = self._payload(far_facts=["x"], far_lead=self._LEAD, place="your home area")
        self.assertIn("measured from your home area", payload)
        self.assertIn("never call anything 'nearby'", payload)

    def test_no_origin_line_when_searching_where_they_are(self):
        payload = self._payload(far_facts=["x"], far_lead=self._LEAD)
        self.assertNotIn("measured from", payload)


if __name__ == "__main__":
    unittest.main()
