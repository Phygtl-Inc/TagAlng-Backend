import unittest

from app.lana_ui import sanitize_assistant_message


class TestSanitizeAssistantMessage(unittest.TestCase):
    def test_strips_ui_metadata_line(self) -> None:
        raw = "INTEREST · question\nI found 3 neighbors near 32827:"
        self.assertEqual(
            sanitize_assistant_message(raw),
            "I found 3 neighbors near 32827:",
        )

    def test_keeps_normal_reply(self) -> None:
        text = "I found 2 neighbors on your block."
        self.assertEqual(sanitize_assistant_message(text), text)
