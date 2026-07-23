"""The need-ZIP loop fix (QA 2026-07-23): "I don't want to enter my ZIP right
now" re-asked the same canned ZIP question verbatim, forever.

Root cause: the decline regex matched, but the off-ramp was suppressed because
the classifier still reported a discovery goal (peers) — the regex-vs-AI-goal
race. Fix: the classifier now emits declined_slot='zip' and _zip_ask_declined
treats that verdict as authoritative regardless of goal; the regex remains the
fallback-only backstop with its goal suppression intact."""

import unittest

from app.discovery_route import _zip_ask_declined
from app.discovery_slots import _empty_slots


class TestZipAskDeclined(unittest.TestCase):
    def test_ai_declined_slot_fires_even_with_active_goal(self) -> None:
        # The transcript bug: goal stays "peers" (they still want neighbors),
        # but the AI read the refusal — off-ramp must fire.
        slots = {"goal": "peers", "declined_slot": "zip"}
        self.assertTrue(
            _zip_ask_declined(slots, "I don't want to enter my ZIP right now")
        )

    def test_ai_declined_slot_fires_without_regex_match(self) -> None:
        # Phrasings the regex can't know ("that's private") — AI verdict alone.
        slots = {"goal": "continue", "declined_slot": "zip"}
        self.assertTrue(_zip_ask_declined(slots, "that's private, sorry"))

    def test_other_declined_slot_does_not_fire_zip_off_ramp(self) -> None:
        slots = {"goal": "peers", "declined_slot": "display_name"}
        self.assertFalse(_zip_ask_declined(slots, "I'd rather not say my name"))

    def test_regex_backstop_suppressed_while_goal_active(self) -> None:
        # Legacy behavior preserved: "find me people, stop asking questions"
        # style turns (regex hit, active goal, no AI decline) keep discovering.
        slots = {"goal": "peers", "declined_slot": None}
        self.assertFalse(
            _zip_ask_declined(slots, "I don't want to answer, just find me people")
        )

    def test_regex_backstop_fires_when_goal_inactive(self) -> None:
        slots = {"goal": "chat", "declined_slot": None}
        self.assertTrue(_zip_ask_declined(slots, "maybe later"))

    def test_plain_answer_never_off_ramps(self) -> None:
        slots = {"goal": "peers", "declined_slot": None}
        self.assertFalse(_zip_ask_declined(slots, "I'm in Lake Nona"))

    def test_no_slots_at_all_uses_regex_only(self) -> None:
        self.assertTrue(_zip_ask_declined(None, "rather not"))
        self.assertFalse(_zip_ask_declined(None, "32827"))


class TestSlotsSchema(unittest.TestCase):
    def test_empty_slots_carry_declined_slot(self) -> None:
        self.assertIn("declined_slot", _empty_slots())
        self.assertIsNone(_empty_slots()["declined_slot"])


if __name__ == "__main__":
    unittest.main()
