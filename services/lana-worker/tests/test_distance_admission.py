"""Distance admission (C3): the one inequality that replaces the two-pass widening.

Two properties are load-bearing. The floor RISES with distance and is CLAMPED below, so a
far meet must be more on-topic than a near one and a near meet still has to be on-topic
at all. And a meet with no similarity is admitted rather than hidden — its topic is
unknown, not bad — but sorts after every meet that earned its place.

Everything here is arithmetic over plain dicts: no database, no model, no app graph.
"""

import copy
import math
import unittest

from app.distance_admission import _admission_floor, _admit

# The defaults the module ships with, spelled out so a test that pins a number says which
# knobs produced it. If these move, the expected values below move with them.
_FLOOR_BASE = 0.55
_K = 0.08
_RADIUS_BASE_M = 8000.0
_CLAMP_RATIO = 0.9
_CLAMP = _FLOOR_BASE * _CLAMP_RATIO  # 0.495


def _row(eid, *, similarity="absent", distance_meters="absent", **kw):
    """A candidate row. Pass similarity=None for an unembedded meet; leave it out for a
    row that never carried the key at all (a fallback fetch, a bare fixture)."""
    row = {"id": eid, "title": f"meet {eid}"}
    if similarity != "absent":
        row["similarity"] = similarity
    if distance_meters != "absent":
        row["distance_meters"] = distance_meters
    row.update(kw)
    return row


class AdmissionFloorTests(unittest.TestCase):
    def test_at_the_base_radius_the_floor_is_the_base(self) -> None:
        self.assertAlmostEqual(_admission_floor(8000.0), _FLOOR_BASE, places=9)

    def test_at_five_times_the_base_radius(self) -> None:
        expected = _FLOOR_BASE + _K * math.log(5)
        self.assertAlmostEqual(_admission_floor(40000.0), expected, places=9)
        self.assertAlmostEqual(_admission_floor(40000.0), 0.679, places=3)

    def test_at_the_sql_ceiling(self) -> None:
        # 200 km is what Postgres clamps the radius to (20261202120000:108); the floor
        # there is the most a meet is ever asked to score.
        expected = _FLOOR_BASE + _K * math.log(25)
        self.assertAlmostEqual(_admission_floor(200000.0), expected, places=9)
        self.assertAlmostEqual(_admission_floor(200000.0), 0.808, places=3)

    def test_close_in_the_floor_is_clamped_not_the_raw_log(self) -> None:
        raw = _FLOOR_BASE + _K * math.log(1000.0 / _RADIUS_BASE_M)
        self.assertAlmostEqual(raw, 0.384, places=3)  # what the clamp is protecting against
        self.assertAlmostEqual(_admission_floor(1000.0), _CLAMP, places=9)

    def test_zero_distance_returns_the_clamp_and_raises_nothing(self) -> None:
        self.assertAlmostEqual(_admission_floor(0.0), _CLAMP, places=9)
        self.assertAlmostEqual(_admission_floor(0), _CLAMP, places=9)

    def test_negative_distance_raises_nothing(self) -> None:
        self.assertAlmostEqual(_admission_floor(-5.0), _CLAMP, places=9)
        self.assertAlmostEqual(_admission_floor(-1e9), _CLAMP, places=9)

    def test_the_floor_rises_with_distance(self) -> None:
        floors = [_admission_floor(d) for d in (8000.0, 16000.0, 40000.0, 200000.0)]
        self.assertEqual(floors, sorted(floors))
        self.assertLess(floors[0], floors[-1])

    def test_tunables_come_through_kwargs(self) -> None:
        # Each knob moves the answer on its own; the defaults are defaults, not constants.
        self.assertAlmostEqual(_admission_floor(8000.0, floor_base=0.7), 0.7, places=9)
        self.assertAlmostEqual(
            _admission_floor(40000.0, k=0.16), _FLOOR_BASE + 0.16 * math.log(5), places=9
        )
        self.assertAlmostEqual(_admission_floor(40000.0, radius_base_m=40000.0), _FLOOR_BASE, places=9)
        self.assertAlmostEqual(_admission_floor(0.0, clamp_ratio=0.5), _FLOOR_BASE * 0.5, places=9)


class AdmitTests(unittest.TestCase):
    def test_a_near_row_just_under_the_clamp_is_rejected(self) -> None:
        self.assertEqual(_admit([_row("a", similarity=0.48, distance_meters=2000.0)]), [])

    def test_a_near_row_at_the_clamp_is_admitted(self) -> None:
        rows = [_row("a", similarity=0.50, distance_meters=2000.0)]
        self.assertEqual([r["id"] for r in _admit(rows)], ["a"])

    def test_a_far_strong_match_beats_a_near_weak_one_in_the_same_call(self) -> None:
        # The topic-dominance case: the violin meet 30 km out clears its (higher) floor,
        # the guitar meet two km away does not clear its (lower) one.
        rows = [
            _row("guitar", similarity=0.48, distance_meters=2000.0),
            _row("violin", similarity=0.90, distance_meters=30000.0),
        ]
        self.assertEqual([r["id"] for r in _admit(rows)], ["violin"])

    def test_admitted_embedded_rows_keep_their_input_order(self) -> None:
        rows = [
            _row("first", similarity=0.9, distance_meters=1000.0),
            _row("dropped", similarity=0.1, distance_meters=1000.0),
            _row("second", similarity=0.6, distance_meters=8000.0),
            _row("third", similarity=0.95, distance_meters=100000.0),
        ]
        self.assertEqual([r["id"] for r in _admit(rows)], ["first", "second", "third"])

    def test_an_unembedded_row_is_admitted_and_sorts_after_embedded_ones(self) -> None:
        rows = [
            _row("fresh", similarity=None, distance_meters=500.0),
            _row("scored", similarity=0.9, distance_meters=1000.0),
        ]
        self.assertEqual([r["id"] for r in _admit(rows)], ["scored", "fresh"])

    def test_a_missing_similarity_key_behaves_like_none(self) -> None:
        rows = [
            _row("nokey", distance_meters=500.0),
            _row("scored", similarity=0.9, distance_meters=1000.0),
        ]
        self.assertEqual([r["id"] for r in _admit(rows)], ["scored", "nokey"])
        # And it is admitted regardless of distance — there is no floor for an unknown.
        self.assertEqual([r["id"] for r in _admit([_row("far", distance_meters=199000.0)])], ["far"])

    def test_a_missing_distance_reads_as_zero(self) -> None:
        # Zero distance → the clamp; the row is judged, not skipped.
        self.assertEqual(_admit([_row("a", similarity=0.48)]), [])
        self.assertEqual([r["id"] for r in _admit([_row("b", similarity=0.50)])], ["b"])

    def test_tunables_come_through_kwargs(self) -> None:
        row = _row("a", similarity=0.60, distance_meters=8000.0)
        self.assertEqual(_admit([row]), [row])
        self.assertEqual(_admit([row], floor_base=0.65), [])

    def test_input_rows_are_unchanged(self) -> None:
        rows = [
            _row("a", similarity=0.9, distance_meters=1000.0),
            _row("b", similarity=0.1, distance_meters=1000.0),
            _row("c", similarity=None, distance_meters=500.0),
            _row("d"),
        ]
        before = copy.deepcopy(rows)
        out = _admit(rows)
        self.assertEqual(rows, before)
        self.assertEqual([r["id"] for r in rows], ["a", "b", "c", "d"])
        # The caller gets its own objects back, not copies — nothing was stamped on them.
        self.assertTrue(all(any(r is src for src in rows) for r in out))

    def test_non_dict_entries_are_skipped(self) -> None:
        rows = [None, "junk", _row("a", similarity=0.9, distance_meters=1000.0)]
        self.assertEqual([r["id"] for r in _admit(rows)], ["a"])

    def test_empty_input_is_empty_output(self) -> None:
        self.assertEqual(_admit([]), [])


if __name__ == "__main__":
    unittest.main()
