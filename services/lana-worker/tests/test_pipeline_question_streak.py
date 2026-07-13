import unittest

from app.orchestrator.pipeline import _apply_question_streak_guard


def _routing(outcome="A", **extra):
    r = {"outcome": outcome}
    r.update(extra)
    return r


class TestQuestionStreakGuard(unittest.TestCase):
    def test_first_a_increments(self):
        routing = _routing("A")
        ctx = {}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(ctx["question_streak"], 1)
        self.assertEqual(routing["outcome"], "A")
        self.assertNotIn("enforce_notes", routing)

    def test_second_a_increments(self):
        routing = _routing("A")
        ctx = {"question_streak": 1}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(ctx["question_streak"], 2)
        self.assertEqual(routing["outcome"], "A")

    def test_third_a_downgrades(self):
        routing = _routing("A")
        ctx = {"question_streak": 2}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(routing["outcome"], "R")
        self.assertEqual(ctx["question_streak"], 0)
        self.assertIn("question_streak_downgrade", routing["enforce_notes"])

    def test_non_a_resets_streak(self):
        routing = _routing("T")
        ctx = {"question_streak": 2}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(ctx["question_streak"], 0)
        self.assertEqual(routing["outcome"], "T")

    def test_r_outcome_resets_streak(self):
        routing = _routing("R")
        ctx = {"question_streak": 2}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(ctx["question_streak"], 0)
        self.assertEqual(routing["outcome"], "R")

    def test_a_t_a_no_false_trigger(self):
        ctx = {}
        # Turn 1: A, streak 0 → 1
        r1 = _routing("A")
        _apply_question_streak_guard(r1, ctx, "lana")
        self.assertEqual(r1["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 1)
        # Turn 2: T, streak 1 → 0
        r2 = _routing("T")
        _apply_question_streak_guard(r2, ctx, "lana")
        self.assertEqual(r2["outcome"], "T")
        self.assertEqual(ctx["question_streak"], 0)
        # Turn 3: A, streak 0 → 1 (no downgrade)
        r3 = _routing("A")
        _apply_question_streak_guard(r3, ctx, "lana")
        self.assertEqual(r3["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 1)

    def test_downgrade_then_recycle(self):
        ctx = {"question_streak": 2}
        # Downgrade fires
        r = _routing("A")
        _apply_question_streak_guard(r, ctx, "lana")
        self.assertEqual(r["outcome"], "R")
        self.assertEqual(ctx["question_streak"], 0)
        # Three more A turns: 0→1, 1→2, 2→downgrade
        r1 = _routing("A")
        _apply_question_streak_guard(r1, ctx, "lana")
        self.assertEqual(r1["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 1)
        r2 = _routing("A")
        _apply_question_streak_guard(r2, ctx, "lana")
        self.assertEqual(r2["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 2)
        r3 = _routing("A")
        _apply_question_streak_guard(r3, ctx, "lana")
        self.assertEqual(r3["outcome"], "R")
        self.assertIn("question_streak_downgrade", r3["enforce_notes"])

    def test_purpose_scoping_skips_guard(self):
        routing = _routing("A")
        ctx = {"question_streak": 2}
        _apply_question_streak_guard(routing, ctx, "event_draft")
        self.assertEqual(routing["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 2)

    def test_flow_suppression_event_host(self):
        routing = _routing("A")
        ctx = {"question_streak": 2, "event_host_active": True}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(routing["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 2)

    def test_flow_suppression_other_flags(self):
        for flag in ("pass_along_active", "tip_share_active", "activity_browse_active"):
            with self.subTest(flag=flag):
                routing = _routing("A")
                ctx = {"question_streak": 2, flag: True}
                _apply_question_streak_guard(routing, ctx, "lana")
                self.assertEqual(routing["outcome"], "A")
                self.assertEqual(ctx["question_streak"], 2)

    def test_corrupt_streak_string(self):
        routing = _routing("A")
        ctx = {"question_streak": "2"}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(routing["outcome"], "R")
        self.assertEqual(ctx["question_streak"], 0)

    def test_none_streak(self):
        routing = _routing("A")
        ctx = {"question_streak": None}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(routing["outcome"], "A")
        self.assertEqual(ctx["question_streak"], 1)

    def test_enforce_notes_preserved(self):
        routing = _routing("A", enforce_notes=["foo"])
        ctx = {"question_streak": 2}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(routing["enforce_notes"], ["foo", "question_streak_downgrade"])

    def test_enforce_notes_idempotent(self):
        routing = _routing("A", enforce_notes=["question_streak_downgrade"])
        ctx = {"question_streak": 2}
        _apply_question_streak_guard(routing, ctx, "lana")
        self.assertEqual(routing["enforce_notes"].count("question_streak_downgrade"), 1)


if __name__ == "__main__":
    unittest.main()
