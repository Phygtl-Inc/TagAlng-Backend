import unittest

from app.event_context import (
    EVENT_HISTORY_MAX,
    format_chat_history,
    format_event_draft_context,
    host_display_name,
)


class TestEventMinimalContext(unittest.TestCase):
    def test_host_display_name_prefers_nickname(self) -> None:
        self.assertEqual(host_display_name({"nickname": "Amanda"}), "Amanda")

    def test_host_display_name_falls_back_to_first_name(self) -> None:
        self.assertEqual(host_display_name({"full_name": "Amanda Lee"}), "Amanda")

    def test_format_event_draft_context_uses_host_name(self) -> None:
        ctx = {
            "nickname": "Amanda",
            "block_display_name": "Lake Nona",
            "event_purpose_ids": ["coffee_stroller"],
        }
        block = format_event_draft_context(ctx)
        self.assertIn("Host name (use in greeting): Amanda", block)

    def test_format_event_draft_context_is_minimal(self) -> None:
        ctx = {
            "nickname": "Alex",
            "block_display_name": "Lake Nona",
            "event_purpose_ids": ["coffee_stroller", "running_fitness"],
        }
        block = format_event_draft_context(ctx)
        self.assertIn("Alex", block)
        self.assertIn("Lake Nona", block)
        self.assertIn("coffee_stroller", block)
        self.assertNotIn("Vector similarity", block)
        self.assertNotIn("Neighbor hints", block)
        self.assertNotIn("profile threads", block)

    def test_trim_history_keeps_recent_messages(self) -> None:
        history = [
            {"role": "assistant", "content": f"msg{i}"}
            for i in range(EVENT_HISTORY_MAX + 4)
        ]
        text = format_chat_history(history, max_messages=EVENT_HISTORY_MAX)
        self.assertIn(f"msg{EVENT_HISTORY_MAX + 3}", text)
        self.assertNotIn("msg0", text)


if __name__ == "__main__":
    unittest.main()
