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


# ── semantic gap open + reconcile close ──────────────────────────────────────
class TestGapLifecycle(unittest.TestCase):
    def test_open_semantic_gap_stores_contextual_question(self):
        store = _store()
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.open_semantic_gap(
                "u1", "m1",
                "Nice — online with a squad, or solo career mode?",
                label="FIFA", bucket="interest",
            )
        opened = [r for (t, r) in store["inserts"] if t == "rapport_gaps"]
        self.assertEqual(len(opened), 1)
        row = opened[0]
        self.assertEqual(row["question"], "Nice — online with a squad, or solo career mode?")
        self.assertEqual(row["gap_id"], "deepen:fifa")
        self.assertEqual(row["parent_bucket"], "interest")
        self.assertIn("fifa", row["why_frame"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["opened_from_message_id"], "m1")

    def test_open_semantic_gap_ignores_blank_question(self):
        store = _store()
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.open_semantic_gap("u1", "m1", "   ", label="FIFA")
        self.assertEqual(store["inserts"], [])

    def test_open_semantic_gap_generic_frame_without_label(self):
        store = _store()
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.open_semantic_gap("u1", None, "What do you enjoy most about it?")
        row = [r for (_t, r) in store["inserts"]][0]
        self.assertEqual(row["parent_bucket"], "general")
        self.assertEqual(row["why_frame"], "one quick thing…")

    def test_reconcile_closes_gap_when_concept_stated(self):
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

    def test_reconcile_leaves_semantic_gap_open(self):
        # Semantic gaps use a synthetic covers_concept that never matches a real claim.
        claims = [
            {"id": "c1", "concept": "running_enthusiast", "label": "Run",
             "bucket": "activity", "confidence": 0.9},
        ]
        gaps = [
            {"gap_row_id": "g1", "gap_id": "deepen:run", "status": "open",
             "covers_concept": "deepen_run"},
        ]
        store = _store(claims=claims, gaps=gaps)
        with patch.object(rapport_gaps, "service_client", return_value=_Supabase(store)):
            rapport_gaps.reconcile_gaps("u1")
        self.assertEqual(store["updates"], [])  # nothing closed


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

    def test_cycle_retires_pending_and_returns_another_bypassing_cap(self):
        # User tapped refresh: skip the current pending ask, hand over a different one,
        # even though the 24h cap would normally block a new ask.
        store = _store(gaps=[self._row("free_windows", parent_bucket="interest")])
        pending = {"gap_row_id": "row_pending", "gap_id": "kids_ages"}
        with patch.object(rapport_ranker, "service_client", return_value=_Supabase(store)), \
             patch.object(rapport_ranker, "_pending_ask", return_value=pending), \
             patch.object(rapport_ranker, "_recently_asked", return_value=True), \
             patch.object(rapport_ranker, "_max_tier_rank", return_value=0), \
             patch.object(rapport_ranker, "track"):
            ask = rapport_ranker.next_ask("u1", cycle=True)
        # retired the pending one via the skip RPC
        self.assertIn(
            ("increment_skip_and_reopen", {"p_gap_row_id": "row_pending"}), store["rpcs"]
        )
        # returned a fresh, different gap despite the cap
        self.assertIsNotNone(ask)
        self.assertEqual(ask["gap_id"], "free_windows")

    def test_dynamic_semantic_gap_served_with_stored_question(self):
        # A gap not in the static tree (gap_id "deepen:…") is served with its stored
        # question and treated as LOW sensitivity — the whole point of the redesign.
        row = self._row("deepen:fifa", parent_bucket="interest")
        row["question"] = "Online with a squad, or solo career mode?"
        ask, _s, _t = self._run([row], tier_rank=0)
        self.assertIsNotNone(ask)
        self.assertEqual(ask["gap_id"], "deepen:fifa")
        self.assertEqual(ask["question"], "Online with a squad, or solo career mode?")
        self.assertEqual(ask["sensitivity_tier"], "LOW")

    def test_marks_gap_asked_and_returns_contract(self):
        ask, store, _t = self._run([self._row("free_windows", parent_bucket="interest")])
        self.assertEqual(ask["chip_color_token"], "--d-interest")
        self.assertEqual(ask["sensitivity_tier"], "LOW")
        marked = [r for (_t2, r) in store["updates"] if r.get("status") == "asked"]
        self.assertTrue(marked)


if __name__ == "__main__":
    unittest.main()
