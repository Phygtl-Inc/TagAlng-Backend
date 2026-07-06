"""Rapport (Ring C) unit tests — gap tree integrity, reconciliation, ranker gating.

The Supabase client is faked (no network). We patch `service_client` in each module and a
couple of ranker helpers so we can exercise the decision logic in isolation.
"""

import unittest
from unittest.mock import patch

from app import rapport_gaps, rapport_ranker
from app.rapport_gap_tree import (
    CLAIM_BUCKETS,
    GAP_TREE,
    concept_is_valid,
    render_why_frame,
)


# ── a tiny fake Supabase client (fluent chain → recorded ops) ────────────────
class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
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

    # all filters are no-ops for the fake
    def eq(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def or_(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._op == "select":
            return _Result(list(self.store["selects"].get(self.table, [])))
        if self._op == "insert":
            self.store["inserts"].append((self.table, self._payload))
            return _Result([self._payload])
        if self._op == "update":
            self.store["updates"].append((self.table, self._payload))
            return _Result([])
        return _Result([])


class _Supabase:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(name, self.store)

    def rpc(self, fn, params):
        self.store["rpcs"].append((fn, params))
        return _Query("__rpc__", self.store)


def _store(claims=None, gaps=None):
    return {
        "selects": {
            "user_identity_claims": claims or [],
            "rapport_gaps": gaps or [],
        },
        "inserts": [],
        "updates": [],
        "rpcs": [],
    }


# ── gap tree integrity ───────────────────────────────────────────────────────
class TestGapTree(unittest.TestCase):
    def test_covers_concept_matches_claim_check(self):
        # Every covered concept must satisfy user_identity_claims' concept-format CHECK,
        # or the close/suppress join can never match (the whole spec-reconciliation point).
        for gap_id, gap in GAP_TREE.items():
            self.assertTrue(
                concept_is_valid(gap["covers_concept"]),
                f"{gap_id}: covers_concept {gap['covers_concept']!r} violates concept CHECK",
            )

    def test_parent_bucket_is_a_real_claim_bucket(self):
        for gap_id, gap in GAP_TREE.items():
            self.assertIn(gap["parent_bucket"], CLAIM_BUCKETS, gap_id)

    def test_sensitivity_tier_values(self):
        for gap_id, gap in GAP_TREE.items():
            self.assertIn(gap["sensitivity_tier"], {"LOW", "MED", "HIGH"}, gap_id)

    def test_every_gap_has_a_question(self):
        for gap_id, gap in GAP_TREE.items():
            self.assertTrue(str(gap.get("question") or "").strip(), f"{gap_id} missing question")

    def test_why_frame_interpolates_label(self):
        gap = {"why_frame_template": "about your {label}…"}
        self.assertEqual(render_why_frame(gap, "Morning Run"), "about your morning run…")

    def test_why_frame_without_label_is_graceful(self):
        gap = {"why_frame_template": "about your {label}…"}
        self.assertEqual(render_why_frame(gap, None), "about that…")

    def test_static_why_frame_passthrough(self):
        gap = {"why_frame_template": "about your family's traditions…"}
        self.assertEqual(
            render_why_frame(gap, "ignored"), "about your family's traditions…"
        )


# ── reconciliation: open / suppress / close ──────────────────────────────────
class TestReconcile(unittest.TestCase):
    def test_opens_gaps_for_a_captured_claim(self):
        claims = [
            {"id": "c1", "concept": "running_enthusiast", "label": "Morning Run",
             "bucket": "activity", "confidence": 0.9},
        ]
        store = _store(claims=claims, gaps=[])
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1", "m1")
        opened = [row for (tbl, row) in store["inserts"] if tbl == "rapport_gaps"]
        gap_ids = {r["gap_id"] for r in opened}
        # activity bucket unlocks both activity gaps, tile copy sourced from the claim label.
        self.assertIn("activity_social_pref", gap_ids)
        self.assertIn("activity_frequency", gap_ids)
        social = next(r for r in opened if r["gap_id"] == "activity_social_pref")
        self.assertIn("morning run", social["why_frame"])
        self.assertEqual(social["status"], "open")
        self.assertEqual(social["opened_from_message_id"], "m1")

    def test_suppresses_gap_already_known(self):
        # She already has the 'activity_social_pref' concept → don't ask it again.
        claims = [
            {"id": "c1", "concept": "running_enthusiast", "label": "Morning Run",
             "bucket": "activity", "confidence": 0.9},
            {"id": "c2", "concept": "activity_social_pref", "label": "Prefers Group",
             "bucket": "activity", "confidence": 0.8},
        ]
        store = _store(claims=claims, gaps=[])
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1")
        gap_ids = {r["gap_id"] for (_t, r) in store["inserts"]}
        self.assertNotIn("activity_social_pref", gap_ids)
        self.assertIn("activity_frequency", gap_ids)  # the other one still opens

    def test_does_not_reopen_existing_gap(self):
        claims = [
            {"id": "c1", "concept": "running_enthusiast", "label": "Morning Run",
             "bucket": "activity", "confidence": 0.9},
        ]
        gaps = [
            {"gap_row_id": "g1", "gap_id": "activity_social_pref", "status": "skipped",
             "covers_concept": "activity_social_pref"},
        ]
        store = _store(claims=claims, gaps=gaps)
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1")
        opened = {r["gap_id"] for (_t, r) in store["inserts"]}
        self.assertNotIn("activity_social_pref", opened)  # already has a row

    def test_kids_gap_gated_off_bare_stage_claim(self):
        # "Married 10 years" (stage) alone must NOT open kids_ages — no kids mentioned.
        claims = [
            {"id": "c1", "concept": "married_ten_years", "label": "Married 10 years",
             "bucket": "stage", "confidence": 0.9},
        ]
        store = _store(claims=claims, gaps=[])
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1")
        opened = {r["gap_id"] for (_t, r) in store["inserts"]}
        self.assertNotIn("kids_ages", opened)
        self.assertIn("daily_rhythm", opened)  # ungated stage gap still opens

    def test_kids_gap_opens_when_kids_mentioned(self):
        claims = [
            {"id": "c1", "concept": "mom_of_two", "label": "Mom of 2 kids",
             "bucket": "stage", "confidence": 0.9},
        ]
        store = _store(claims=claims, gaps=[])
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1")
        opened = {r["gap_id"] for (_t, r) in store["inserts"]}
        self.assertIn("kids_ages", opened)

    def test_closes_gap_when_covered_concept_appears(self):
        claims = [
            {"id": "c9", "concept": "kids_ages", "label": "2 and 5",
             "bucket": "stage", "confidence": 0.95},
        ]
        gaps = [
            {"gap_row_id": "g1", "gap_id": "kids_ages", "status": "asked",
             "covers_concept": "kids_ages"},
        ]
        store = _store(claims=claims, gaps=gaps)
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1")
        closed = [row for (_t, row) in store["updates"] if row.get("status") == "answered"]
        self.assertTrue(closed)
        self.assertEqual(closed[0]["answer_claim_id"], "c9")


# ── skip / mute ───────────────────────────────────────────────────────────────
class TestSkipMute(unittest.TestCase):
    def test_record_skip_calls_rpc(self):
        store = _store()
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.record_skip("g1")
        self.assertEqual(store["rpcs"], [("increment_skip_and_reopen", {"p_gap_row_id": "g1"})])

    def test_mute_inserts_muted_row_when_none_exists(self):
        store = _store()
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.mute_gap("u1", "kids_ages")
        muted = [r for (_t, r) in store["inserts"] if r.get("status") == "muted_by_user"]
        self.assertEqual(len(muted), 1)
        self.assertEqual(muted[0]["gap_id"], "kids_ages")

    def test_mute_unknown_gap_is_noop(self):
        store = _store()
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.mute_gap("u1", "not_a_real_gap")
        self.assertEqual(store["inserts"], [])


# ── ranker: freq cap, tier gate, scoring ──────────────────────────────────────
class TestRanker(unittest.TestCase):
    def _run(self, rows, *, recently_asked=False, tier_rank=0, pending=None):
        store = _store(gaps=rows)
        with patch.object(rapport_ranker, "service_client", return_value=_Supabase(store)), \
             patch.object(rapport_ranker, "_pending_ask", return_value=pending), \
             patch.object(rapport_ranker, "_recently_asked", return_value=recently_asked), \
             patch.object(rapport_ranker, "_max_tier_rank", return_value=tier_rank), \
             patch.object(rapport_ranker, "track") as trk:
            ask = rapport_ranker.next_ask("u1")
        return ask, store, trk

    def _row(self, gap_id, **over):
        base = {
            "gap_row_id": f"row_{gap_id}", "gap_id": gap_id, "parent_bucket": "activity",
            "why_frame": "about that…", "unlock_score": 0.6, "skipped_count": 0,
            "opened_at": "2026-07-01T00:00:00Z", "status": "open",
        }
        base.update(over)
        return base

    def test_freq_cap_returns_none(self):
        ask, _s, _t = self._run([self._row("activity_social_pref")], recently_asked=True)
        self.assertIsNone(ask)

    def test_no_candidates_returns_none(self):
        ask, _s, _t = self._run([])
        self.assertIsNone(ask)

    def test_high_sensitivity_gated_for_stranger(self):
        # support_need is HIGH; a stranger (rank 0) must not see it, but a LOW gap can show.
        rows = [self._row("support_need", parent_bucket="general"),
                self._row("free_windows", parent_bucket="interest")]
        ask, _s, trk = self._run(rows, tier_rank=0)
        self.assertIsNotNone(ask)
        self.assertEqual(ask["gap_id"], "free_windows")
        trk.assert_called_once()

    def test_high_sensitivity_allowed_for_acquaintance(self):
        rows = [self._row("support_need", parent_bucket="general", unlock_score=0.9)]
        ask, _s, _t = self._run(rows, tier_rank=2)  # acquaintance
        self.assertIsNotNone(ask)
        self.assertEqual(ask["gap_id"], "support_need")

    def test_skip_decay_changes_winner(self):
        # A higher base score loses once it's been skipped enough.
        rows = [
            self._row("activity_social_pref", unlock_score=0.65, skipped_count=2),  # 0.65*0.6=0.39
            self._row("activity_frequency", unlock_score=0.55, skipped_count=0),     # 0.55
        ]
        ask, _s, _t = self._run(rows, tier_rank=0)
        self.assertEqual(ask["gap_id"], "activity_frequency")

    def test_marks_gap_asked_and_returns_contract(self):
        ask, store, _t = self._run([self._row("free_windows", parent_bucket="interest")])
        self.assertEqual(ask["chip_color_token"], "--d-interest")
        self.assertEqual(ask["sensitivity_tier"], "LOW")
        marked = [r for (_t2, r) in store["updates"] if r.get("status") == "asked"]
        self.assertTrue(marked)


if __name__ == "__main__":
    unittest.main()
