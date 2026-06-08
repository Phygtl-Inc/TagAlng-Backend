import unittest

from app.profile_intake import (
    _clamp_assistant_message,
    assistant_message_looks_truncated,
)


class TestProfileTruncation(unittest.TestCase):
    def test_detects_mid_sentence_cutoff(self) -> None:
        self.assertTrue(
            assistant_message_looks_truncated(
                'That\'s wonderful to know you\'re a "brazilian mom"! We\'ve got a'
            )
        )

    def test_complete_sentence_ok(self) -> None:
        self.assertFalse(
            assistant_message_looks_truncated(
                "Love that — what should neighbors call you on the block?"
            )
        )

    def test_clamp_on_long_message(self) -> None:
        long = "word " * 200
        out = _clamp_assistant_message(long)
        self.assertLessEqual(len(out), 321)
        self.assertTrue(out.endswith("."))


if __name__ == "__main__":
    unittest.main()
