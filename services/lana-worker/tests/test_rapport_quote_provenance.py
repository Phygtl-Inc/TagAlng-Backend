"""Claim quotes reach the question writer whole, and with what they were said about.

Prod 2026-09-28: a Pausa recommendation said the chef is "always present a Da Vinci of the
Italian cuisine". The extractor stored that claim's quote cut at 160 chars, the question
writer cut it again at 120 — after "a Da Vinci" — and the tile asked the user about their
favourite dish "at Da Vinci's", a restaurant that does not exist.
"""

import unittest

from app import rapport_synth
from app.vertex_extract import _parse_claims

# Verbatim from prod (user_identity_claims 04bff8dc…, local_signals 578c1d40…). The
# description really has a double space before "a Da Vinci".
PAUSA_QUOTE = (
    "contemporary but original authentic Italian cuisine. The chef and owner Andrea is "
    "hands-on and always present a Da Vinci of the Italian cuisine. All appetizer p"
)
PAUSA_DESCRIPTION = (
    "authentic Italian, homemade dishes, imported ingredients, Neapolitan pizza, great wine "
    "· So the restaurant Pausa in San Mateo is one of the best restaurants when it comes to "
    "a contemporary but original authentic Italian cuisine. The chef and owner Andrea is "
    "hands-on and always present  a Da Vinci of the Italian cuisine. All appetizer pizzas, "
    "pastas, desserts are homemade."
)


def _claim(**over):
    base = {
        "concept": "italian_cuisine_enthusiast",
        "label": "Enthusiast of authentic Italian cuisine",
        "bucket": "interest",
        "source_quote": PAUSA_QUOTE,
    }
    base.update(over)
    return base


class _Chain:
    """Just enough of the supabase query builder for one local_signals read."""

    def __init__(self, rows, fail=False):
        self._rows = rows
        self._fail = fail
        self.not_ = self
        self.filters = []

    def table(self, name):
        self.filters.append(("table", name))
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self._fail:
            raise RuntimeError("db down")
        return type("R", (), {"data": self._rows})()


class _Patched(unittest.TestCase):
    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)

    def _db(self, rows, fail=False):
        chain = _Chain(rows, fail=fail)
        self._patch(rapport_synth, "service_client", lambda: chain)
        return chain


class ExtractorKeepsWholeQuote(unittest.TestCase):
    def test_long_quote_is_not_sliced(self):
        quote = PAUSA_DESCRIPTION  # well past the old 160-char cut
        claims = _parse_claims(
            {"claims": [{"concept": "italian_food", "label": "Italian food",
                         "source_quote": quote, "bucket": "interest"}]}
        )
        self.assertEqual(claims[0].source_quote, quote)


class UncoveredBlockKeepsWholeQuote(unittest.TestCase):
    def test_metaphor_survives_to_the_prompt(self):
        block = rapport_synth._uncovered_block([_claim()])
        # The old [:120] ended the line on "a Da Vinci" and dropped what made it praise.
        self.assertIn("a Da Vinci of the Italian cuisine", block)

    def test_subject_note_rendered(self):
        block = rapport_synth._uncovered_block(
            [_claim(about_name="Pausa", about_category="restaurant")]
        )
        self.assertIn("(said about Pausa, a restaurant they recommended)", block)

    def test_no_note_without_subject(self):
        self.assertNotIn("said about", rapport_synth._uncovered_block([_claim()]))


class AttachRecoSubjects(_Patched):
    def test_prod_pausa_quote_resolves_to_pausa(self):
        chain = self._db([{"reco_name": "Pausa", "category": "restaurant",
                           "reco_description": PAUSA_DESCRIPTION, "detail_text": None}])
        claims = [_claim()]
        rapport_synth._attach_reco_subjects("u1", claims)
        self.assertEqual(claims[0].get("about_name"), "Pausa")
        self.assertEqual(claims[0].get("about_category"), "restaurant")
        # Only the user's OWN recos are searched.
        self.assertIn(("eq", "user_id", "u1"), chain.filters)

    def test_matches_detail_text_too(self):
        self._db([{"reco_name": "Pausa", "category": "restaurant",
                   "reco_description": None, "detail_text": "Pausa · " + PAUSA_DESCRIPTION}])
        claims = [_claim()]
        rapport_synth._attach_reco_subjects("u1", claims)
        self.assertEqual(claims[0].get("about_name"), "Pausa")

    def test_quote_from_chat_gets_no_subject(self):
        self._db([{"reco_name": "Pausa", "category": "restaurant",
                   "reco_description": PAUSA_DESCRIPTION, "detail_text": None}])
        claims = [_claim(source_quote="I cook Italian at home every Sunday")]
        rapport_synth._attach_reco_subjects("u1", claims)
        self.assertNotIn("about_name", claims[0])

    def test_read_failure_leaves_threads_untouched(self):
        self._db([], fail=True)
        claims = [_claim()]
        rapport_synth._attach_reco_subjects("u1", claims)  # must not raise
        self.assertNotIn("about_name", claims[0])

    def test_quoteless_threads_skip_the_read(self):
        chain = self._db([])
        rapport_synth._attach_reco_subjects("u1", [_claim(source_quote="")])
        self.assertEqual(chain.filters, [])


class SynthSendsTheSubject(_Patched):
    """The wiring: what synthesize_gaps_from_claims actually hands the model."""

    def test_prompt_payload_names_pausa(self):
        self._db([{"reco_name": "Pausa", "category": "restaurant",
                   "reco_description": PAUSA_DESCRIPTION, "detail_text": None}])
        seen = {}

        def fake_generate(uncovered, asked, max_new):
            seen["uncovered"] = uncovered
            return {"questions": []}

        self._patch(rapport_synth, "_cooling_down", lambda uid, store=None: False)
        self._patch(rapport_synth, "_backfill_question_embeddings", lambda uid: None)
        self._patch(rapport_synth, "_uncovered_claims", lambda uid: [_claim()])
        self._patch(rapport_synth, "_language_thread_needed", lambda uid: False)
        self._patch(rapport_synth, "recent_gap_questions", lambda uid, limit=60: [])
        self._patch(rapport_synth, "_generate", fake_generate)
        rapport_synth.synthesize_gaps_from_claims("u1")
        self.assertIn("said about Pausa, a restaurant", seen["uncovered"])
        self.assertIn("a Da Vinci of the Italian cuisine", seen["uncovered"])


if __name__ == "__main__":
    unittest.main()
