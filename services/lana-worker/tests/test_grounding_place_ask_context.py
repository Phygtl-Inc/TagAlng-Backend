"""A place-grounding ask must be flagged to the router as the one rapport question
whose answer becomes a Places QUERY.

Why this exists: the extractor captures solo hobbies as circles ("I play guitar
every weekend" -> {hobby, guitar_group}), so grounding now asks place-or-solo. The
"on my own" answer has nothing to look up — fed to Google it returns arbitrary
nearby spots and offers them as the user's own place (the 2026-08-03 bug class).
For an ORDINARY rapport question the same words are a plain answer ("I usually run
alone"), so the instruction has to be scoped to this ask and cleared with the rest
of the rapport keys.
"""

from __future__ import annotations

import unittest


class TestPlaceAskCaptureLine(unittest.TestCase):
    def _capture(self, ctx: dict) -> str:
        from app.discovery_slots import _active_capture_context

        return _active_capture_context(ctx)

    def test_place_ask_tells_the_router_the_answer_becomes_a_search(self) -> None:
        line = self._capture(
            {
                "rapport_active": True,
                "rapport_place_ask": True,
                "rapport_followup_question": "Do you play guitar anywhere in "
                "particular, or mostly on your own?",
            }
        )
        self.assertTrue(line.startswith("rapport"))
        self.assertIn("map search", line)
        # The no-place answer must be named as an ANSWER that closes the thread.
        self.assertIn("abandon=true", line)

    def test_ordinary_rapport_question_gets_no_place_instruction(self) -> None:
        # Without the flag "I usually run alone" stays a normal answer, which the
        # base rapport line already spells out — adding the place rule here would
        # make every shrug read as an abandon.
        line = self._capture(
            {
                "rapport_active": True,
                "rapport_followup_question": "Mornings or weekends for running?",
            }
        )
        self.assertTrue(line.startswith("rapport"))
        self.assertNotIn("map search", line)

    def test_flag_is_cleared_with_the_other_rapport_keys(self) -> None:
        # Set to None rather than popped: the {**old, **new} session merge
        # resurrects popped keys, which would leave the ask armed forever.
        from app.lana_unified_pipeline import _reset_rapport_state

        ctx = {"rapport_active": True, "rapport_place_ask": True}
        _reset_rapport_state(ctx)
        self.assertIn("rapport_place_ask", ctx)
        self.assertIsNone(ctx["rapport_place_ask"])


if __name__ == "__main__":
    unittest.main()
