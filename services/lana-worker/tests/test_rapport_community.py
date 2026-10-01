"""The "By the way…" tile inside a community (app/rapport_community.py).

Order: the creator's first question → a queued question that fits → a new one written from
the community → the normal queue. Community rows are never served outside their community,
and the personal queue is neither grown nor lost.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import rapport_community as rc
from app.community_scope import CTX_KEY

PID = "10000000-0000-0000-0000-00000000beef"
FACTS = {
    "place_id": PID, "name": "Beast Bros", "kind": "creator community",
    "about": "Fans of MrBeast's big stunts and giveaways",
    "members_help": "Video ideas and premiere watch-alongs",
    "creator_wants": "What's your all-time favourite MrBeast video?",
    "creator": "Jimmy", "at": datetime.now(timezone.utc).isoformat(),
}


def _scope(facts=FACTS):
    return {CTX_KEY: {"place_id": PID, "name": "Beast Bros"}, "_active_community_facts": dict(facts)}


class _Queue:
    """In-memory rapport_gaps for one user, driven through the module's own seams."""

    def __init__(self, personal=None):
        self.rows = list(personal or [])
        self.statuses: list[tuple[str, str]] = []

    def community(self, _u, pid):
        return [r for r in self.rows if rc.is_community_row(r, pid)]

    def open_gap(self, _u, _m, question, *, gap_id, place_ref, **kw):
        if any(r["gap_id"] == gap_id for r in self.rows):
            return False
        self.rows.append({"gap_row_id": f"row-{len(self.rows)}", "gap_id": gap_id,
                          "question": question, "status": "open", "place_ref": place_ref})
        return True

    def set_status(self, row_id, status):
        self.statuses.append((row_id, status))
        for r in self.rows:
            if r["gap_row_id"] == row_id:
                r["status"] = status
        return True

    def personal_open(self, _u):
        return [r for r in self.rows if not rc.is_community_row(r) and r["status"] == "open"]

    def personal_pending(self, _u):
        return next((r for r in self.rows if not rc.is_community_row(r) and r["status"] == "asked"), None)


def _run(q, *, scope=None, pick=None, made=None, cycle=False, allow_personal=True):
    with patch.object(rc, "_rows", side_effect=q.community), patch(
        "app.rapport_gaps.open_semantic_gap", side_effect=q.open_gap
    ), patch.object(rc, "_set_status", side_effect=q.set_status), patch.object(
        rc, "_personal_open_rows", side_effect=q.personal_open
    ), patch.object(rc, "_personal_pending", side_effect=q.personal_pending), patch.object(
        rc, "_pick_relevant", return_value=pick
    ) as picker, patch.object(rc, "_generate", return_value=made), patch.object(
        rc, "_serve", side_effect=lambda u, row, **kw: {"q": row["question"], "kind": kw["kind"]}
    ):
        out = rc.next_ask_in_community(
            "u1", _scope() if scope is None else scope, lang="en", cycle=cycle,
            allow_personal_queue=allow_personal,
        )
    return out, picker


class OrderTests(unittest.TestCase):
    def test_first_visit_asks_the_creators_own_question_verbatim(self) -> None:
        q = _Queue()
        (handled, ask), _ = _run(q)
        self.assertTrue(handled)
        self.assertEqual(ask, {"q": FACTS["creator_wants"], "kind": "community_first"})
        row = q.community("u1", PID)[0]
        self.assertEqual(row["gap_id"], f"community:{PID}:first")
        # The answer's claim inherits the community (tag_claim_place_from_gap).
        self.assertEqual(row["place_ref"], PID)

    def test_a_reload_reshows_the_same_question(self) -> None:
        q = _Queue()
        _run(q)
        (handled, ask), _ = _run(q)
        self.assertEqual(ask["kind"], "community_pending")
        self.assertEqual(len(q.community("u1", PID)), 1)

    def test_answered_first_question_never_returns_then_a_fitting_queued_one(self) -> None:
        tri = {"gap_row_id": "p1", "gap_id": "deepen:triathlon", "question": "Long course triathlon?", "status": "open"}
        q = _Queue([tri])
        _run(q)
        q.set_status(q.community("u1", PID)[0]["gap_row_id"], "answered")
        (handled, ask), _ = _run(q, pick=tri)
        self.assertEqual(ask, {"q": "Long course triathlon?", "kind": "community_relevant"})
        # Asked here instead of later: nothing was added to the personal queue.
        self.assertEqual([r for r in q.rows if not rc.is_community_row(r)], [tri])

    def test_a_different_pending_personal_ask_is_put_back_not_lost(self) -> None:
        other = {"gap_row_id": "p0", "gap_id": "deepen:pizza", "question": "Pizza?", "status": "asked"}
        fits = {"gap_row_id": "p1", "gap_id": "deepen:videos", "question": "Videos?", "status": "open"}
        q = _Queue([other, fits])
        facts = dict(FACTS, creator_wants=None)
        _run(q, scope=_scope(facts), pick=fits)
        self.assertEqual(other["status"], "open")
        self.assertEqual(fits["status"], "asked")

    def test_nothing_fits_so_one_is_written_from_the_community(self) -> None:
        q = _Queue()
        facts = dict(FACTS, creator_wants=None)
        (handled, ask), _ = _run(q, scope=_scope(facts), made={"question": "Which challenge would you try?", "teaser": "about you"})
        self.assertEqual(ask["kind"], "community_generated")
        self.assertTrue(q.community("u1", PID)[0]["gap_id"].startswith(f"community:{PID}:gen-"))

    def test_generation_is_capped_per_community(self) -> None:
        q = _Queue([
            {"gap_row_id": f"g{i}", "gap_id": f"community:{PID}:gen-{i}", "question": f"q{i}", "status": "answered"}
            for i in range(rc._MAX_GENERATED)
        ])
        facts = dict(FACTS, creator_wants=None)
        (handled, ask), _ = _run(q, scope=_scope(facts), made={"question": "another?", "teaser": "x"})
        # Nothing new was written: falls through to the normal queue.
        self.assertFalse(handled)
        self.assertIsNone(ask)

    def test_nothing_community_shaped_falls_back_to_the_normal_queue(self) -> None:
        q = _Queue()
        facts = dict(FACTS, creator_wants=None, about=None, members_help=None)
        (handled, ask), _ = _run(q, scope=_scope(facts), made=None)
        self.assertFalse(handled)

    def test_cycle_skips_the_pending_community_question(self) -> None:
        q = _Queue()
        _run(q)
        first = q.community("u1", PID)[0]
        _run(q, cycle=True, made=None)
        self.assertEqual(first["status"], "skipped")


class GuestTests(unittest.TestCase):
    def test_guest_gets_the_community_question(self) -> None:
        (handled, ask), _ = _run(_Queue(), allow_personal=False)
        self.assertEqual(ask["kind"], "community_first")

    def test_guest_never_reaches_the_personal_queue(self) -> None:
        tri = {"gap_row_id": "p1", "gap_id": "deepen:t", "question": "T?", "status": "open"}
        facts = dict(FACTS, creator_wants=None, about=None, members_help=None)
        (handled, ask), picker = _run(_Queue([tri]), scope=_scope(facts), pick=tri, allow_personal=False)
        picker.assert_not_called()
        self.assertTrue(handled)
        self.assertIsNone(ask)


class NotInsideACommunityTests(unittest.TestCase):
    def test_no_community_means_not_handled(self) -> None:
        (handled, ask), _ = _run(_Queue(), scope={})
        self.assertFalse(handled)


class RankerIsolationTests(unittest.TestCase):
    """Outside its community a community question is never served."""

    def test_open_community_rows_are_dropped_from_the_personal_candidates(self) -> None:
        from app import rapport_ranker as rr

        class _Q:
            def __getattr__(self, _n):
                return lambda *a, **k: self

            @property
            def not_(self):
                return self

            def execute(self):
                class R:
                    data = [
                        {"gap_row_id": "a", "gap_id": "deepen:x", "status": "open"},
                        {"gap_row_id": "b", "gap_id": f"community:{PID}:first", "status": "open"},
                    ]
                return R()

        class _C:
            def table(self, _n):
                return _Q()

        with patch.object(rr, "service_client", return_value=_C()), patch.object(
            rr, "_apply_repetition_windows", side_effect=lambda rows: rows
        ):
            rows = rr._load_open_rows("u1")
        self.assertEqual([r["gap_row_id"] for r in rows], ["a"])


if __name__ == "__main__":
    unittest.main()


class CardLabelTests(unittest.TestCase):
    """The expanded card read "TOMMASO · PINNED" and promised "neighbors nearby" over a
    question asked inside Tommaso's community (2026-10-01)."""

    def _extras(self, gap_id):
        from app import rapport_ranker as rr

        class _Q:
            def __getattr__(self, _n):
                return lambda *a, **k: self

            def execute(self):
                class R:
                    data = [{"name": "Tommaso", "place_type": "creator"}]
                return R()

        class _C:
            def table(self, _n):
                return _Q()

        with patch.object(rr, "service_client", return_value=_C()):
            return rr._place_extras({"gap_id": gap_id, "place_ref": PID, "gap_row_id": "r"}), \
                rr._community_name_for({"gap_id": gap_id, "place_ref": PID})

    def test_community_question_is_kind_community_not_pinned(self) -> None:
        extras, name = self._extras(f"community:{PID}:gen-1")
        self.assertEqual(extras["kind"], "community")
        self.assertEqual(name, "Tommaso")

    def test_a_pinned_place_enrichment_ask_is_unchanged(self) -> None:
        extras, name = self._extras("deepen:gym")
        self.assertEqual(extras["kind"], "place_affinity")
        self.assertIsNone(name)

    def test_reason_writer_is_told_the_community(self) -> None:
        from app import rapport_reasons as rs

        seen = {}
        with patch("app.orchestrator.llm.llm_configured", return_value=True), patch(
            "app.orchestrator.llm.llm_json", side_effect=lambda **kw: seen.update(kw) or {"reason": "so I can connect you"}
        ):
            rs.compose_ask_reason("What are you building?", community="Tommaso")
        import json
        self.assertEqual(json.loads(seen["user_payload"])["community"], "Tommaso")
        self.assertIn("never \"neighbors\"", seen["system"])
