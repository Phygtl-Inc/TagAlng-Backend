import unittest

from app.profile_intake import (
    apply_profile_stop_rules,
    collect_profile_buckets,
    needs_kids_followup,
    profile_intake_gaps,
)


class TestProfileStopRules(unittest.TestCase):
    def test_forces_complete_after_three_user_turns(self) -> None:
        history = [
            {"role": "user", "content": "Mexican American"},
            {"role": "assistant", "content": "Nice!"},
            {"role": "user", "content": "software engineer"},
            {"role": "assistant", "content": "Cool!"},
            {"role": "user", "content": "looking for running buddies"},
        ]
        ui = {
            "highlights": [
                {"text": "Mexican American", "bucket": "heritage"},
                {"text": "running buddies", "bucket": "activity"},
            ]
        }
        msg, status = apply_profile_stop_rules(
            "continue",
            "What else should neighbors know?",
            history=history,
            ui=ui,
            topics_covered=["heritage", "activity"],
        )
        self.assertEqual(status, "ready_to_complete")
        self.assertIn("Complete", msg)

    def test_two_turns_with_heritage_and_interest(self) -> None:
        history = [
            {"role": "user", "content": "Puerto Rican, new to the block"},
            {"role": "assistant", "content": "Welcome!"},
            {"role": "user", "content": "want coffee meetups"},
        ]
        ui = {
            "highlights": [
                {"text": "Puerto Rican", "bucket": "heritage"},
                {"text": "coffee meetups", "bucket": "interest"},
            ]
        }
        _, status = apply_profile_stop_rules(
            "continue",
            "Love that!",
            history=history,
            ui=ui,
            topics_covered=["heritage"],
        )
        self.assertEqual(status, "ready_to_complete")

    def test_stays_continue_on_first_answer(self) -> None:
        history = [{"role": "user", "content": "Indian American"}]
        ui = {"highlights": [{"text": "Indian American", "bucket": "heritage"}]}
        _, status = apply_profile_stop_rules(
            "continue",
            "What do you enjoy on the block?",
            history=history,
            ui=ui,
            topics_covered=["heritage"],
        )
        self.assertEqual(status, "continue")

    def test_collect_buckets_from_history_metadata(self) -> None:
        history = [
            {
                "role": "assistant",
                "content": "hi",
                "metadata": {
                    "ui": {"highlights": [{"text": "Cuban", "bucket": "heritage"}]}
                },
            }
        ]
        buckets = collect_profile_buckets(history=history, ui={}, topics_covered=[])
        self.assertIn("heritage", buckets)


    def test_waits_for_display_name_before_complete(self) -> None:
        history = [
            {"role": "user", "content": "Brazilian mom looking for friends"},
            {"role": "assistant", "content": "Love it!"},
            {"role": "user", "content": "coffee meetups"},
        ]
        ui = {
            "highlights": [
                {"text": "Brazilian", "bucket": "heritage"},
                {"text": "coffee meetups", "bucket": "interest"},
            ]
        }
        _, status = apply_profile_stop_rules(
            "ready_to_complete",
            "Tap Complete!",
            history=history,
            ui=ui,
            topics_covered=["heritage", "interest"],
            profile_gaps={"needs_display_name": True},
            display_name_saved=False,
        )
        self.assertEqual(status, "continue")

    def test_completes_after_name_patch(self) -> None:
        history = [
            {"role": "user", "content": "Brazilian, love coffee on the block"},
            {"role": "user", "content": "Sara"},
        ]
        ui = {
            "highlights": [
                {"text": "Brazilian", "bucket": "heritage"},
                {"text": "friends", "bucket": "interest"},
            ]
        }
        _, status = apply_profile_stop_rules(
            "continue",
            "Great!",
            history=history,
            ui=ui,
            topics_covered=["heritage", "interest"],
            profile_gaps={"needs_display_name": True},
            profile_patch={"nickname": "Sara"},
        )
        self.assertEqual(status, "ready_to_complete")

    def test_kids_followup_for_mom_without_detail(self) -> None:
        self.assertTrue(
            needs_kids_followup(
                history=[{"role": "user", "content": "I am a new mom"}],
                ui={},
                topics_covered=[],
            )
        )
        self.assertFalse(
            needs_kids_followup(
                history=[{"role": "user", "content": "mom of two toddlers ages 2 and 4"}],
                ui={},
                topics_covered=[],
            )
        )

    def test_profile_gaps_when_no_name(self) -> None:
        gaps = profile_intake_gaps({"nickname": None, "full_name": None})
        self.assertTrue(gaps["needs_display_name"])


if __name__ == "__main__":
    unittest.main()
