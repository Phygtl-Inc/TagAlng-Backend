import unittest

from app.turn_timing import TurnTimer


class TestTurnTimer(unittest.TestCase):
    def test_stage_accumulates(self) -> None:
        timer = TurnTimer()
        with timer.stage("a"):
            pass
        timer.add("b", 10)
        self.assertIn("a", timer.ms)
        self.assertEqual(timer.ms["b"], 10)
        self.assertGreaterEqual(timer.to_dict()["total_ms"], 10)


if __name__ == "__main__":
    unittest.main()
