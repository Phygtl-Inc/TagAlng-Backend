import unittest

from app.profile_intake import apply_profile_stop_rules, collect_profile_buckets


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


if __name__ == "__main__":
    unittest.main()
