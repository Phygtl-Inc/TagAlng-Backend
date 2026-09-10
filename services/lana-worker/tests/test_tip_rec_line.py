"""The authored "why Lana sees a fit" block on a recent-recommendation row (§43).

Same mechanism as the fellows line (test_peer_rec_line), different evidence: the reader's
own claims against the recommendation's own fields. What must hold: two readers with
different claims get different lines for the SAME recommendation, a reader with no overlap
gets no line and no invented sentence, a reload serves the cached text with no LLM call,
and the For-you tab is ordered by the same overlap it writes the line from.

The Supabase client is faked (no network), same pattern as test_peer_rec_line.
"""

import json
import unittest
from unittest.mock import patch

from app import peer_rec_line, tip_rec_line
from app.tip_rec_line import _score, attach_fit
from tests.test_peer_rec_line import _Supabase


def _tip(n=1, *, name="Little Kickers", category="toddler soccer", author="p-1", **extra):
    row = {
        "signal_id": f"s-{n}",
        "name": name,
        "category": category,
        "reco_type": "activity",
        "place": "Lake Nona",
        "description": "Saturday mornings, small groups",
        "fields": [{"label": "Ages", "answer": "2 to 3 years"}],
        "detail_text": None,
        "created_at": f"2026-09-0{n}T10:00:00+00:00",
        "peer_user_id": author,
        "shared_circles": [],
        "same_block": False,
    }
    row.update(extra)
    return row


class TestAttachFit(unittest.TestCase):
    def _run(self, rows, claims, *, cached=None, llm=None, tab="recent", limit=20):
        store = {"rows": cached or [], "upserts": []}
        data = {"lines": llm} if llm is not None else {}
        with (
            patch.object(peer_rec_line, "service_client", return_value=_Supabase(store)),
            patch("app.claims_persist.fetch_active_claim_labels", return_value=claims),
            patch("app.lang_pref.get_user_preferred_language", return_value="en"),
            patch("app.orchestrator.llm.llm_configured", return_value=True),
            patch("app.orchestrator.llm.composer_model", return_value="test-model"),
            patch("app.orchestrator.llm.llm_json", return_value=data) as call,
        ):
            attach_fit("u-1", rows, tab=tab, limit=limit)
        return store, call

    def test_two_readers_get_different_lines_for_the_same_recommendation(self):
        # The whole point of the block: the evidence is the READER's claims, so the same
        # signal_id cannot author one line for everybody.
        soccer, dance = _tip(), _tip()
        _, a = self._run([soccer], ["Toddler soccer on weekends"], llm=["Your Saturday."])
        _, b = self._run([dance], ["Ballet dancer"], llm=["Something else."])
        self.assertEqual(soccer["rec_line"], "Your Saturday.")
        # The second reader's claim touches nothing in the tip, so nothing is authored
        # for her at all — she is not handed the first reader's sentence.
        self.assertIsNone(dance["rec_line"])
        self.assertEqual(a.call_count, 1)
        self.assertEqual(b.call_count, 0)

    def test_no_overlap_means_no_line_and_no_invented_sentence(self):
        row = _tip()
        _, call = self._run([row], ["Collects vinyl"], llm=["A fit!"])
        self.assertIsNone(row["rec_line"])
        self.assertEqual(row["rec_chips"], [])
        self.assertIsNone(row["rec_id"])
        self.assertEqual(call.call_count, 0)

    def test_the_line_is_written_from_the_tip_fields_and_the_matched_claim_only(self):
        row = _tip(shared_circles=[{"place_id": "pl-1", "name": "CF Fitness"}])
        _, call = self._run(
            [row], ["Collects vinyl", "Toddler at home", "Soccer on Saturdays"], llm=["x."]
        )
        (basis,) = json.loads(call.call_args.kwargs["user_payload"])
        # The untouched claim never reaches the prompt, and the claims that DO are led by
        # the one the recommendation covers most of.
        self.assertEqual(basis["you_said"], ["Soccer on Saturdays", "Toddler at home"])
        self.assertEqual(basis["recommendation"]["name"], "Little Kickers")
        self.assertIn("Ages: 2 to 3 years", basis["recommendation"]["details"])
        self.assertEqual(basis["from_neighbor"]["shared_places"], ["CF Fitness"])

    def test_a_reload_serves_the_cached_text_with_no_llm_call(self):
        first = _tip()
        store, _ = self._run([first], ["Soccer on Saturdays"], llm=["Right after class."])
        (payload,) = store["upserts"]
        cached = [{**payload[0], "id": "rec-1"}]
        again = _tip()
        _, call = self._run([again], ["Soccer on Saturdays"], cached=cached)
        self.assertEqual(again["rec_line"], first["rec_line"])
        self.assertEqual(again["rec_id"], "rec-1")
        self.assertEqual(call.call_count, 0)

    def test_foryou_orders_by_fit_and_slices_back_to_the_page(self):
        # The tab is a fit ORDER over a wider fetch, so the best row can come from below
        # the fold of the recent page — and the page still comes back `limit` long.
        rows = [
            _tip(1, name="Vinyl Corner", category="record shop"),
            _tip(2, name="Little Kickers", category="toddler soccer"),
            _tip(3, name="Quiet Cafe", category="cafe"),
        ]
        self._run(rows, ["Soccer on Saturdays"], tab="foryou", limit=2, llm=["Fits."])
        self.assertEqual([r["name"] for r in rows], ["Little Kickers", "Quiet Cafe"])
        self.assertGreater(rows[0]["match_strength"], rows[1]["match_strength"])

    def test_shared_circle_nudges_the_score_but_never_carries_it(self):
        # A shared community is context, not a fit: on its own it must not outrank a row
        # the reader's own claims actually touch.
        self.assertEqual(_score([], {"shared_circles": [{"name": "CF"}]}), 0.1)
        self.assertGreater(_score([("Soccer", 1.0)], {}), 0.1)

    def test_every_row_carries_the_fields_even_when_nothing_is_authored(self):
        # The client widens its schema by three nullish fields; a missing key is a parse
        # error, not an absent block.
        rows = [_tip()]
        with patch("app.claims_persist.fetch_active_claim_labels", return_value=[]):
            attach_fit("u-1", rows)
        self.assertEqual(
            {"rec_line": None, "rec_chips": [], "rec_id": None, "match_strength": 0.0},
            {k: rows[0][k] for k in ("rec_line", "rec_chips", "rec_id", "match_strength")},
        )
        self.assertNotIn("_fit_basis", rows[0])


if __name__ == "__main__":
    unittest.main()
