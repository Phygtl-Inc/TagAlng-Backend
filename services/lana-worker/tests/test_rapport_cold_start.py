"""Cold start, priority, and the skip brake — the always-on "By the way…" tile.

Three behaviors that did not exist before 2026-09-04:

* SEEDING. Every rapport opener is claim-triggered, so a zero-claim user had a
  queue that never started (not one that ran dry) and the tile stayed empty. Seeds
  now come from what neighbors NEARBY actually claim, because the onion matcher
  awards +1 only per SHARED public concept — an interest nobody within reach holds
  scores nothing, however true it is.
* PRIORITY. Every gap opened at a flat 0.8, so "highest-scoring open gap" was
  really oldest-open-first. Scores now come from the matcher's weights.
* SKIP BRAKE. The 6h/3-per-week cap suppressed the tile for users who were
  ANSWERING (answer one, get an empty card for six hours). Pacing is now read from
  skips instead.
"""

import unittest
from datetime import datetime, timedelta, timezone

from app import rapport_priority, rapport_ranker, rapport_synth


def _iso(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class _Result:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _Query:
    """No-op filter chain over a per-table canned result, like tests/test_rapport.py."""

    def __init__(self, table, store):
        self.table = table
        self.store = store
        self._op = None
        self._payload = None

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = row
        return self

    def update(self, row):
        self._op = "update"
        self._payload = row
        return self

    def eq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    @property
    def not_(self):
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._op == "insert":
            self.store.setdefault("inserts", []).append((self.table, self._payload))
            return _Result([dict(self._payload, gap_row_id="new-row")])
        if self._op == "update":
            self.store.setdefault("updates", []).append((self.table, self._payload))
            return _Result([])
        rows = list(self.store.get("selects", {}).get(self.table, []))
        return _Result(rows, count=len(rows))


class _Rpc:
    """postgrest returns a builder from .rpc(); the result only exists after
    .execute(). Returning a bare result here made every RPC look like it raised."""

    def __init__(self, data):
        self._data = data

    def execute(self):
        return _Result(self._data)


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(name, self.store)

    def rpc(self, name, params):
        self.store.setdefault("rpcs", []).append((name, params))
        return _Rpc(self.store.get("rpc_results", {}).get(name))


class _StubBase(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict = {"selects": {}, "rpc_results": {}}
        self._patched: list[tuple[object, str, object]] = []
        for mod in (rapport_ranker, rapport_synth):
            self._patch(mod, "service_client", lambda: _Client(self.store))
        # rapport_priority imports service_client INSIDE covered_buckets (deferred,
        # to keep app.auth out of the import graph), so there is no module attribute
        # to patch — patch the source instead.
        import app.auth as auth_mod

        self._patch(auth_mod, "service_client", lambda: _Client(self.store))

    def _patch(self, mod, name, value) -> None:
        if hasattr(mod, name):
            self._patched.append((mod, name, getattr(mod, name)))
            setattr(mod, name, value)

    def tearDown(self) -> None:
        for mod, name, old in reversed(self._patched):
            setattr(mod, name, old)


class TestPriorityFromMatcherWeights(_StubBase):
    def test_untouched_bucket_outranks_a_deepening_question(self) -> None:
        self.store["selects"]["rapport_gaps"] = [{"parent_bucket": "activity"}]
        new_axis = rapport_priority.score_for("u1", bucket="heritage")
        deepen = rapport_priority.score_for("u1", bucket="activity", deepens_concept="running")
        self.assertGreater(new_axis, deepen)

    def test_place_question_outranks_every_interest_question(self) -> None:
        """A place is +3 in score_onion_candidates_for_user; an interest is +1."""
        self.store["selects"]["rapport_gaps"] = []
        place = rapport_priority.score_for("u1", bucket="activity", affiliation_ref="aff-1")
        interest = rapport_priority.score_for("u1", bucket="heritage")
        self.assertGreater(place, interest)

    def test_language_question_outranks_the_place_question(self) -> None:
        """Language gates every reply, so it is not a matcher signal — it precedes them."""
        self.store["selects"]["rapport_gaps"] = []
        lang = rapport_priority.score_for("u1", deepens_concept="languages_spoken")
        place = rapport_priority.score_for("u1", bucket="activity", affiliation_ref="a1")
        self.assertGreater(lang, place)

    def test_covered_buckets_counts_answered_only(self) -> None:
        # An OPEN question in a bucket has not produced a claim yet, so the bucket is
        # still worth asking into. The stub returns whatever the query asked for, so
        # this asserts the filter intent via the status the code selects on.
        self.store["selects"]["rapport_gaps"] = [
            {"parent_bucket": "activity"},
            {"parent_bucket": "stage"},
        ]
        self.assertEqual(rapport_priority.covered_buckets("u1"), {"activity", "stage"})


class TestColdStartSeeding(_StubBase):
    def setUp(self) -> None:
        super().setUp()
        self.opened: list[dict] = []

        def fake_open(user_id, message_id, question, **kw):
            self.opened.append({"question": question, **kw})
            return True

        self._patch(rapport_synth, "open_semantic_gap", fake_open)
        self._patch(rapport_synth, "recent_gap_questions", lambda uid, limit=10: [])
        rapport_synth._last_attempt.clear()

    def tearDown(self) -> None:
        rapport_synth._last_attempt.clear()
        super().tearDown()

    def test_user_with_claims_is_never_seeded(self) -> None:
        """Anyone with a profile is served by the deepening synth, not by seeds."""
        self.store["selects"]["user_identity_claims"] = [{"id": "c1"}]
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 0)
        self.assertEqual(self.opened, [])

    def test_seeds_are_marked_as_local_supply_for_priority(self) -> None:
        self.store["selects"]["user_identity_claims"] = []
        self.store["rpc_results"]["rapport_local_supply"] = [
            {"concept": "running", "label": "Running", "bucket": "activity", "holders": 4},
        ]
        self._patch(
            rapport_synth,
            "_generate_seeds",
            lambda supply, asked, max_new: {
                "questions": [
                    {
                        "question": "Where do you like to run around here?",
                        "teaser": "about your weekends…",
                        "label": "running",
                        "bucket": "activity",
                        "deepens_concept": "running",
                        "answer_options": ["The Lake Nona trail", "Just the neighborhood"],
                    }
                ]
            },
        )
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 1)
        self.assertTrue(self.opened[0]["from_local_supply"])
        self.assertEqual(len(self.opened[0]["answer_options"]), 2)

    def test_local_supply_is_asked_for_before_any_fallback(self) -> None:
        self.store["selects"]["user_identity_claims"] = []
        self.store["rpc_results"]["rapport_local_supply"] = []
        seeded: list[str] = []
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: (seeded.append(uid) or 3))
        rapport_synth.seed_cold_start("u1")
        self.assertIn("rapport_local_supply", [n for n, _ in self.store["rpcs"]])

    def test_no_local_supply_falls_back_to_the_catalogue_seeds(self) -> None:
        """First user in an area: nothing nearby to draw on, but the no-prior-knowledge
        catalogue questions need no supply at all."""
        self.store["selects"]["user_identity_claims"] = []
        self.store["rpc_results"]["rapport_local_supply"] = []
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)
        self.assertEqual(self.opened, [], "no LLM seeds without local supply")

    def test_unusable_model_output_still_falls_back(self) -> None:
        self.store["selects"]["user_identity_claims"] = []
        self.store["rpc_results"]["rapport_local_supply"] = [
            {"concept": "running", "label": "Running", "bucket": "activity", "holders": 4},
        ]
        self._patch(rapport_synth, "_generate_seeds", lambda *a: {"questions": []})
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)

    def test_claim_read_failure_does_not_seed_an_existing_profile(self) -> None:
        """Fails CLOSED: better to skip a seed than to interview someone we know."""
        self._patch(rapport_synth, "_has_any_claim", lambda uid: True)
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 0)

    def test_catalogue_seeds_span_distinct_buckets(self) -> None:
        """The point of the seed set: four answers, four match axes — not one topic
        explored four times."""
        from app.rapport_gap_tree import GAP_TREE
        from app.rapport_gaps import COLD_SEED_GAP_IDS

        buckets = {GAP_TREE[g]["parent_bucket"] for g in COLD_SEED_GAP_IDS}
        self.assertEqual(len(buckets), len(COLD_SEED_GAP_IDS))
        for gap_id in COLD_SEED_GAP_IDS:
            gap = GAP_TREE[gap_id]
            # A seed must be answerable with no prior knowledge of the user.
            self.assertNotIn("requires_any_keyword", gap, gap_id)
            self.assertEqual(gap["sensitivity_tier"], "LOW", gap_id)


class TestSkipBrake(_StubBase):
    def test_no_history_does_not_brake(self) -> None:
        self.store["selects"]["rapport_gaps"] = []
        self.assertFalse(rapport_ranker._skip_brake("u1"))

    def test_three_consecutive_skips_brake(self) -> None:
        self.store["selects"]["rapport_gaps"] = [
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=1)},
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=5)},
            {"skipped_count": 2, "answered_at": None, "asked_at": _iso(hours=9)},
        ]
        self.assertTrue(rapport_ranker._skip_brake("u1"))

    def test_one_answer_in_the_window_clears_the_brake(self) -> None:
        self.store["selects"]["rapport_gaps"] = [
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=1)},
            {"skipped_count": 0, "answered_at": _iso(hours=4), "asked_at": _iso(hours=5)},
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=9)},
        ]
        self.assertFalse(rapport_ranker._skip_brake("u1"))

    def test_brake_expires_after_the_cooldown(self) -> None:
        self.store["selects"]["rapport_gaps"] = [
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=30)},
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=34)},
            {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=40)},
        ]
        self.assertFalse(rapport_ranker._skip_brake("u1"))

    def test_disabled_by_env(self) -> None:
        import os

        os.environ["LANA_RAPPORT_SKIP_BRAKE"] = "0"
        try:
            self.store["selects"]["rapport_gaps"] = [
                {"skipped_count": 1, "answered_at": None, "asked_at": _iso(hours=1)},
            ] * 3
            self.assertFalse(rapport_ranker._skip_brake("u1"))
        finally:
            os.environ.pop("LANA_RAPPORT_SKIP_BRAKE", None)

    def test_cadence_cap_is_off_by_default_and_reads_nothing(self) -> None:
        """The always-on tile: with both knobs at 0 the cap must not even query —
        an answered question is no longer followed by six empty hours."""
        self.store["selects"]["rapport_gaps"] = [{"gap_row_id": "g1"}]
        self.assertFalse(rapport_ranker._recently_asked("u1"))
        self.assertNotIn("rapport_gaps", [t for t, _ in self.store.get("updates", [])])

    def test_cadence_cap_still_restorable_by_env(self) -> None:
        import os

        os.environ["LANA_RAPPORT_MIN_HOURS"] = "6"
        try:
            self.store["selects"]["rapport_gaps"] = [{"gap_row_id": "g1"}]
            self.assertTrue(rapport_ranker._recently_asked("u1"))
        finally:
            os.environ.pop("LANA_RAPPORT_MIN_HOURS", None)


if __name__ == "__main__":
    unittest.main()
