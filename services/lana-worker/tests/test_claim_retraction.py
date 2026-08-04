"""A claim the user walks back must actually leave the profile.

QA 2026-08-03: "honestly i dont really have a favorite color like i wouldnt say
my favoirte color is blue anymore". Lana agreed warmly — "our favorites can
shift as we go" — and the claim stayed exactly where it was. Retraction existed
only for heritage, and only behind an explicit verb: dismiss_claims_from_edit_
message needs "remove/delete/drop/clear" AND a word from a hardcoded nationality
list, so a walked-back preference could never reach it.

Worse, _merge_into_existing is corroboration-only ("confidence only rises"), so a
turn that re-mentioned the concept while denying it could STRENGTHEN the claim.
Hence the precedence test below.
"""

import unittest
from unittest.mock import patch

from app.claims_persist import (
    ExtractedClaim, dismiss_retracted_concepts, drop_retracted, parse_retracted_concepts,
)


class _Res:
    def __init__(self, rows):
        self.data = rows


class _Table:
    def __init__(self, rows, updated):
        self._rows = rows
        self._updated = updated

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def is_(self, *_a, **_k):
        return self

    def update(self, patch):
        self._updated["patch"] = patch
        return self

    def in_(self, _col, ids):
        self._updated["ids"] = list(ids)
        return self

    def execute(self):
        return _Res(self._rows)


class _Client:
    def __init__(self, rows, updated):
        self._rows = rows
        self._updated = updated

    def table(self, *_a, **_k):
        return _Table(self._rows, self._updated)


_ROWS = [
    {"id": "c1", "concept": "favorite_color_blue"},
    {"id": "c2", "concept": "steak_lover"},
    {"id": "c3", "concept": "runner"},
]


def _dismiss(concepts, rows=_ROWS):
    updated: dict = {}
    with patch("app.claims_persist.service_client", return_value=_Client(rows, updated)):
        n = dismiss_retracted_concepts("u1", concepts)
    return n, updated


class TestDismissRetractedConcepts(unittest.TestCase):
    def test_walked_back_claim_is_dismissed(self) -> None:
        n, updated = _dismiss(["favorite_color_blue"])
        self.assertEqual(n, 1)
        self.assertEqual(updated["ids"], ["c1"])
        self.assertIn("dismissed_at", updated["patch"])

    def test_works_in_any_bucket_not_just_heritage(self) -> None:
        """The old path only ever touched heritage rows."""
        n, updated = _dismiss(["runner"])
        self.assertEqual(n, 1)
        self.assertEqual(updated["ids"], ["c3"])

    def test_untouched_claims_survive(self) -> None:
        _, updated = _dismiss(["favorite_color_blue"])
        self.assertNotIn("c2", updated["ids"])
        self.assertNotIn("c3", updated["ids"])

    def test_case_insensitive_match(self) -> None:
        n, _ = _dismiss(["Favorite_Color_Blue"])
        self.assertEqual(n, 1)

    def test_unknown_concept_is_a_no_op(self) -> None:
        n, updated = _dismiss(["scuba_diver"])
        self.assertEqual(n, 0)
        self.assertNotIn("ids", updated)

    def test_empty_input_never_touches_the_table(self) -> None:
        for concepts in ([], None, [""], ["  "]):
            n, updated = _dismiss(concepts)
            self.assertEqual(n, 0)
            self.assertEqual(updated, {})

    def test_db_failure_is_swallowed(self) -> None:
        """Bookkeeping must never take down the turn."""
        with patch("app.claims_persist.service_client", side_effect=RuntimeError("down")):
            self.assertEqual(dismiss_retracted_concepts("u1", ["runner"]), 0)


class TestParseRetractedConcepts(unittest.TestCase):
    def test_reads_the_list(self) -> None:
        self.assertEqual(
            parse_retracted_concepts({"retracted_concepts": ["runner", "steak_lover"]}),
            ["runner", "steak_lover"],
        )

    def test_lowercases_and_dedupes(self) -> None:
        self.assertEqual(
            parse_retracted_concepts({"retracted_concepts": ["Runner", "runner"]}),
            ["runner"],
        )

    def test_rejects_non_slugs(self) -> None:
        """The model must echo a stored slug, not free text — anything else could
        dismiss the wrong row."""
        parsed = parse_retracted_concepts(
            {"retracted_concepts": ["not a slug", "9lives", "", None, "ok_slug"]}
        )
        self.assertEqual(parsed, ["ok_slug"])

    def test_missing_or_malformed_field(self) -> None:
        for data in ({}, {"retracted_concepts": None}, {"retracted_concepts": "runner"}, None):
            self.assertEqual(parse_retracted_concepts(data), [])


class TestRetractionBeatsCorroboration(unittest.TestCase):
    """A retraction must win over anything re-emitted for the same concept in the
    same turn — otherwise "I'm not a runner anymore" could bump runner toward 1.0."""

    def test_same_turn_claim_for_a_retracted_concept_is_dropped(self) -> None:
        kept = drop_retracted(
            [
                ExtractedClaim(concept="runner", label="Runner", confidence=0.9),
                ExtractedClaim(concept="nurse", label="Nurse", confidence=0.9),
            ],
            ["runner"],
        )
        self.assertEqual([c.concept for c in kept], ["nurse"])

    def test_a_correction_keeps_the_new_truth(self) -> None:
        """"actually I'm not a teacher, I'm a nurse" retracts one and states another."""
        kept = drop_retracted(
            [
                ExtractedClaim(concept="teacher", label="Teacher", confidence=0.9),
                ExtractedClaim(concept="nurse", label="Nurse", confidence=0.95),
            ],
            ["teacher"],
        )
        self.assertEqual([c.concept for c in kept], ["nurse"])

    def test_nothing_retracted_leaves_the_batch_alone(self) -> None:
        claims = [ExtractedClaim(concept="runner", label="Runner", confidence=0.9)]
        self.assertEqual(drop_retracted(claims, []), claims)


if __name__ == "__main__":
    unittest.main()
