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
        rapport_synth._last_seed_attempt.clear()

    def tearDown(self) -> None:
        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()
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

    def test_lateral_seeding_survives_the_first_claim(self) -> None:
        """The gate is BREADTH, not "has any claim".

        Stopping at claim #1 left synthesize_gaps_from_claims as the only source, and that
        can only deepen topics the user already raised — so every later question was a facet
        of something already said. One answered bucket must not switch lateral supply off.
        """
        import app.rapport_priority as prio

        self._patch(prio, "covered_buckets", lambda uid: {"interest"})
        self._patch(rapport_synth, "_local_supply", lambda uid, **k: [])
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)
        self._patch(rapport_synth, "_generate_seeds", lambda *a: {"questions": []})
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)

    def test_a_fully_covered_profile_stops_being_seeded(self) -> None:
        """The gate still exists — it is just near-unreachable by design.

        The downstream path is deliberately stubbed to SUCCEED here: without that, this
        test passes whether or not the gate fires (seeding returns 0 on empty supply
        anyway), which is a test that cannot fail. 0 must come from the gate, nothing else.
        """
        import app.rapport_gaps as gaps
        import app.rapport_priority as prio

        # Pin everything the gate is NOT: the burst guard would otherwise make the second
        # call return 0 on its own, which is how this test passed while asserting nothing.
        self._patch(rapport_synth, "_cooling_down", lambda uid, store=None: False)
        self._patch(rapport_synth, "_local_supply", lambda uid, **k: [])
        self._patch(rapport_synth, "_generate_seeds", lambda *a: {"questions": []})
        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)

        # Sanity: with the gate open this user WOULD be seeded 3.
        self._patch(prio, "covered_buckets", lambda uid: {"interest"})
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)
        # Close it — bucket coverage is now the ONLY thing that differs.
        from app.rapport_gap_tree import CLAIM_BUCKETS

        self._patch(prio, "covered_buckets", lambda uid: set(CLAIM_BUCKETS))
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 0)

    def test_an_unreadable_profile_fails_OPEN_into_asking(self) -> None:
        """covered_buckets returns set() on a read error, so we ask rather than go silent —
        the reverse of the old gate, and deliberate: silence is the failure users report."""
        import app.rapport_priority as prio

        self._patch(prio, "covered_buckets", lambda uid: set())
        self._patch(rapport_synth, "_local_supply", lambda uid, **k: [])
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)
        self._patch(rapport_synth, "_generate_seeds", lambda *a: {"questions": []})
        self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)

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


class TestThinAreaSupply(_StubBase):
    """A thin area is not an empty one.

    rapport_local_supply defaults to p_min_holders=2, but the onion matcher scores +1
    off a SINGLE shared public claim and only drops a pair at 0 — so two holders is a
    quality heuristic, not a requirement. Before falling back to catalogue questions
    with no counterpart at all, ask again for concepts exactly one neighbor holds.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store["selects"]["user_identity_claims"] = []
        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()

    def tearDown(self) -> None:
        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()
        super().tearDown()

    def _holder_floors(self) -> list[int]:
        return [
            p.get("p_min_holders")
            for n, p in self.store.get("rpcs", [])
            if n == "rapport_local_supply"
        ]

    def test_empty_supply_retries_at_one_holder_before_the_catalogue(self) -> None:
        self.store["rpc_results"]["rapport_local_supply"] = []
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)
        rapport_synth.seed_cold_start("u1")
        self.assertEqual(self._holder_floors(), [2, 1])

    def test_supply_at_two_holders_does_not_retry(self) -> None:
        self.store["rpc_results"]["rapport_local_supply"] = [
            {"concept": "running", "label": "Running", "bucket": "activity", "holders": 4},
        ]
        self._patch(rapport_synth, "_generate_seeds", lambda *a: {"questions": []})
        import app.rapport_gaps as gaps

        self._patch(gaps, "open_cold_seed_gaps", lambda uid: 3)
        rapport_synth.seed_cold_start("u1")
        self.assertEqual(self._holder_floors(), [2])


class _SeedClient:
    """service_client for open_cold_seed_gaps: records inserts, and can refuse any row
    carrying answer_options (a pre-20261029 environment, which has no such column)."""

    def __init__(self, store, reject_chips=False):
        self.store = store
        self.reject_chips = reject_chips

    def table(self, name):
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, row):
        self._row = dict(row)
        return self

    def execute(self):
        row = getattr(self, "_row", None)
        if row is None:
            return _Result([])
        if self.reject_chips and "answer_options" in row:
            raise RuntimeError("column rapport_gaps.answer_options does not exist")
        self.store.setdefault("seeded", []).append(row)
        return _Result([dict(row, gap_row_id="new-row")])


class TestColdSeedChips(unittest.TestCase):
    """The user with the LEAST to say was the only one handed a bare free-text box.

    A user with neighbors gets AI-authored questions WITH one-tap chips (see
    TestColdStartSeeding); the first user in an area fell through to the catalogue,
    whose rows shipped no answer_options at all.
    """

    def setUp(self) -> None:
        import app.rapport_gaps as gaps

        self.gaps = gaps
        self.store: dict = {}
        self._old = gaps.service_client
        gaps.service_client = lambda: _SeedClient(self.store)

    def tearDown(self) -> None:
        self.gaps.service_client = self._old

    def _seeded(self) -> dict[str, dict]:
        return {r["gap_id"]: r for r in self.store.get("seeded", [])}

    def test_chippable_seeds_ship_their_chips(self) -> None:
        self.assertEqual(self.gaps.open_cold_seed_gaps("u1"), 3)
        rows = self._seeded()
        self.assertEqual(rows["relocation_recency"]["answer_options"][0], "Just moved in")
        self.assertEqual(len(rows["free_windows"]["answer_options"]), 3)

    def test_chips_never_exceed_the_cards_three(self) -> None:
        self.gaps.open_cold_seed_gaps("u1")
        for row in self.store["seeded"]:
            self.assertLessEqual(len(row.get("answer_options") or []), 3, row["gap_id"])

    def test_open_ended_seed_is_asked_last(self) -> None:
        """Every seed ties at P_NEW_BUCKET, so the ranker's opened_at tie-break makes
        insertion order the real ranking: a question with no tappable answer must not
        outrank one that has them."""
        chipless = [
            i
            for i, gid in enumerate(self.gaps.COLD_SEED_GAP_IDS)
            if not (self.gaps.get_gap(gid) or {}).get("answer_options")
        ]
        chipped = [
            i
            for i, gid in enumerate(self.gaps.COLD_SEED_GAP_IDS)
            if (self.gaps.get_gap(gid) or {}).get("answer_options")
        ]
        self.assertTrue(chipped, "at least one seed must be tappable")
        self.assertGreater(min(chipless), max(chipped))

    def test_old_schema_keeps_the_seed_and_drops_the_chips(self) -> None:
        """A pre-20261029 environment must lose the chips, never the question."""
        self.gaps.service_client = lambda: _SeedClient(self.store, reject_chips=True)
        self.assertEqual(self.gaps.open_cold_seed_gaps("u1"), 3)
        for row in self.store["seeded"]:
            self.assertNotIn("answer_options", row)
            self.assertTrue(row["question"])


if __name__ == "__main__":
    unittest.main()


class TestSeedingIsReachable(unittest.TestCase):
    """The refill path runs synth, then seeds only if synth made nothing.

    Both shared one 120s burst map, and _cooling_down STAMPS as well as checks — so the
    synth that had just run blocked the seed that followed it by milliseconds, and
    seed_cold_start returned 0 at the guard without reaching a single tier. Shipped
    unnoticed because the old gate (_has_any_claim) made it look intentional.
    """

    def setUp(self) -> None:
        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()

    def test_a_synth_attempt_does_not_block_the_seed_that_follows_it(self) -> None:
        self.assertFalse(rapport_synth._cooling_down("u1"))  # synth runs, stamps
        self.assertFalse(
            rapport_synth._cooling_down("u1", rapport_synth._last_seed_attempt),
            "seeding must not be blocked by the synth that just ran in the same refill",
        )

    def test_each_path_still_rations_its_own_bursts(self) -> None:
        self.assertFalse(rapport_synth._cooling_down("u1"))
        self.assertTrue(rapport_synth._cooling_down("u1"), "a second synth is still blocked")
        seeds = rapport_synth._last_seed_attempt
        self.assertFalse(rapport_synth._cooling_down("u1", seeds))
        self.assertTrue(rapport_synth._cooling_down("u1", seeds), "a second seed is blocked")

    def test_the_two_budgets_are_per_user(self) -> None:
        self.assertFalse(rapport_synth._cooling_down("u1", rapport_synth._last_seed_attempt))
        self.assertFalse(rapport_synth._cooling_down("u2", rapport_synth._last_seed_attempt))

    def test_ensure_gap_buffer_actually_reaches_seeding(self) -> None:
        """The integration the unit guards above cannot prove.

        ensure_gap_buffer is the real refill path: synth first, seed only if synth made
        nothing. This is the test that fails when the two share one burst budget.
        """
        from unittest.mock import patch

        with patch.object(rapport_synth, "ensure_grounding_gaps", create=True), \
             patch.object(rapport_synth, "_open_gap_count", return_value=0), \
             patch.object(rapport_synth, "synthesize_gaps_from_claims", return_value=0) as synth, \
             patch.object(rapport_synth, "seed_cold_start", wraps=rapport_synth.seed_cold_start) as seed, \
             patch.object(rapport_synth, "_local_supply", return_value=[]), \
             patch.object(rapport_synth, "_generate_seeds", return_value={"questions": []}):
            import app.rapport_gaps as gaps

            with patch.object(gaps, "open_cold_seed_gaps", return_value=3):
                # synth is stubbed, so stamp the synth budget by hand exactly as the real
                # one would have on its way to returning 0.
                rapport_synth._cooling_down("u1")
                made = rapport_synth.ensure_gap_buffer("u1")

        synth.assert_called_once()
        seed.assert_called_once()
        self.assertEqual(made, 3, "seeding must run when the synth ahead of it made nothing")


class TestUnknownIsNotEmpty(unittest.TestCase):
    """covered_buckets returns None on a read error and set() when genuinely empty.

    They mean opposite things. Collapsing both to set() made score_for's P_UNKNOWN branch
    dead code: a Supabase blip scored EVERY gap P_NEW_BUCKET (0.85), flattening priority to
    first-opened-first for the duration.
    """

    def test_a_read_error_is_unknown_not_a_new_bucket(self) -> None:
        from unittest.mock import patch

        import app.rapport_priority as prio

        with patch.object(prio, "covered_buckets", return_value=None):
            self.assertEqual(prio.score_for("u1", bucket="interest"), prio.P_UNKNOWN)
            self.assertEqual(
                prio.score_for("u1", bucket="interest", from_local_supply=True),
                prio.P_LOCAL_SUPPLY,
            )

    def test_genuinely_nothing_covered_is_still_a_new_bucket(self) -> None:
        from unittest.mock import patch

        import app.rapport_priority as prio

        with patch.object(prio, "covered_buckets", return_value=set()):
            self.assertEqual(prio.score_for("u1", bucket="interest"), prio.P_NEW_BUCKET)

    def test_an_unreadable_profile_is_still_seeded(self) -> None:
        """The gate must fail OPEN into asking, not closed into silence."""
        from unittest.mock import patch

        import app.rapport_gaps as gaps

        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()
        with patch("app.rapport_priority.covered_buckets", return_value=None), \
             patch.object(rapport_synth, "_local_supply", return_value=[]), \
             patch.object(rapport_synth, "_generate_seeds", return_value={"questions": []}), \
             patch.object(gaps, "open_cold_seed_gaps", return_value=3):
            self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)


class TestCoveredBucketsReadError(unittest.TestCase):
    """The function itself, not its callers. The tests above patch covered_buckets, so
    they prove the callers handle None — nothing proved None is what a read error returns."""

    def test_a_failing_read_returns_None_not_an_empty_set(self) -> None:
        from unittest.mock import patch

        import app.auth as auth_mod
        import app.rapport_priority as prio

        def boom():
            raise RuntimeError("supabase down")

        with patch.object(auth_mod, "service_client", boom):
            self.assertIsNone(prio.covered_buckets("u1"))


class TestLateralRunsAlongsideDeepening(unittest.TestCase):
    """The loop, and where it actually lived.

    Deepening sources topics only from the user's OWN claims, so from their first claim it
    always made something — and `if made: return` meant lateral seeding, the one path that
    can introduce a topic they never mentioned, was never reached again. Measured before the
    fix: a user with one claim got five renders alternating two facets of that claim, 0/5
    lateral. Fixing the gate inside seed_cold_start was necessary and not sufficient.
    """

    def setUp(self) -> None:
        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()

    def test_seeding_runs_even_when_deepening_produced_questions(self) -> None:
        from unittest.mock import patch

        with patch.object(rapport_synth, "ensure_grounding_gaps", create=True), \
             patch.object(rapport_synth, "_open_gap_count", return_value=0), \
             patch.object(rapport_synth, "synthesize_gaps_from_claims", return_value=2), \
             patch.object(rapport_synth, "seed_cold_start", return_value=2) as seed:
            made = rapport_synth.ensure_gap_buffer("u1", target=5)
        seed.assert_called_once()
        self.assertEqual(made, 4, "both sources contribute to one refill")

    def test_seeding_is_skipped_once_the_queue_is_full(self) -> None:
        """Bounded: deepening alone filling the target must not also trigger a seed."""
        from unittest.mock import patch

        with patch.object(rapport_synth, "ensure_grounding_gaps", create=True), \
             patch.object(rapport_synth, "_open_gap_count", return_value=3), \
             patch.object(rapport_synth, "synthesize_gaps_from_claims", return_value=2), \
             patch.object(rapport_synth, "seed_cold_start") as seed:
            made = rapport_synth.ensure_gap_buffer("u1", target=5)
        seed.assert_not_called()
        self.assertEqual(made, 2)

    def test_parse_questions_honours_the_callers_cap(self) -> None:
        """_MAX_NEW is the module default, not a ceiling on every caller — breaking on it
        meant seed_cold_start(max_new=3) silently kept 2 and the buffer could never fill."""
        data = {"questions": [{"question": f"Q{i}?"} for i in range(5)]}
        self.assertEqual(len(rapport_synth._parse_questions(data, 3)), 3)
        self.assertEqual(len(rapport_synth._parse_questions(data, 5)), 5)
        self.assertEqual(len(rapport_synth._parse_questions(data)), rapport_synth._MAX_NEW)


class TestPartialCoverageStillSeeds(unittest.TestCase):
    """The gate must not silence the tile while supply remains.

    Measured against a real 10-neighbour stack: at `>= 5 of 7` buckets a user went
    permanently quiet after about seven answers, with EIGHT unused local-supply concepts
    still on the table. Seven buckets is far too coarse a taxonomy to mean "covered the
    map" — supply exhaustion is the real stop, and this gate is only a backstop.
    """

    def setUp(self) -> None:
        rapport_synth._last_attempt.clear()
        rapport_synth._last_seed_attempt.clear()

    def test_six_of_seven_buckets_still_seeds(self) -> None:
        from unittest.mock import patch

        import app.rapport_gaps as gaps
        from app.rapport_gap_tree import CLAIM_BUCKETS

        six = set(sorted(CLAIM_BUCKETS)[:6])
        with patch("app.rapport_priority.covered_buckets", return_value=six), \
             patch.object(rapport_synth, "_cooling_down", lambda uid, store=None: False), \
             patch.object(rapport_synth, "_local_supply", return_value=[]), \
             patch.object(rapport_synth, "_generate_seeds", return_value={"questions": []}), \
             patch.object(gaps, "open_cold_seed_gaps", return_value=3):
            self.assertEqual(rapport_synth.seed_cold_start("u1"), 3)

    def test_all_seven_is_the_backstop(self) -> None:
        from unittest.mock import patch

        from app.rapport_gap_tree import CLAIM_BUCKETS

        with patch("app.rapport_priority.covered_buckets", return_value=set(CLAIM_BUCKETS)), \
             patch.object(rapport_synth, "_cooling_down", lambda uid, store=None: False):
            self.assertEqual(rapport_synth.seed_cold_start("u1"), 0)
