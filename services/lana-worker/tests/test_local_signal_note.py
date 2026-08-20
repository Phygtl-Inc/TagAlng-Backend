import unittest

from app.discovery_route import _local_signal_note


class TestLocalSignalNote(unittest.TestCase):
    """"From Google — not a neighbor vouch" was printed over the sushi place the asker is
    a member of (prod 2026-08-19). Nobody recommended it, so the vouch line is true — but
    staying silent about the membership hides the better fact."""

    def test_the_users_own_community_is_named(self):
        note = _local_signal_note(
            [{"name": "Mizu Sushi", "community": {"member_count": 1, "is_member": True,
                                                  "activity_labels": ["Sushi Making Classes"]}}]
        )
        self.assertIn("Mizu Sushi", note)
        self.assertIn("sushi making classes", note)

    def test_other_members_are_counted_never_named(self):
        note = _local_signal_note(
            [{"name": "Florida Game Rooms", "community": {"member_count": 3, "is_member": False,
                                                          "activity_labels": ["Snooker"]}}]
        )
        self.assertIn("3 neighbors go to Florida Game Rooms", note)

    def test_one_member_reads_singular(self):
        note = _local_signal_note(
            [{"name": "The Y", "community": {"member_count": 1, "is_member": False,
                                             "activity_labels": []}}]
        )
        self.assertIn("1 neighbor goes to The Y", note)

    def test_only_the_first_match_is_named(self):
        # Two clauses stacked would bury the answer; the cards carry the rest.
        note = _local_signal_note([
            {"name": "A", "community": {"member_count": 2, "is_member": False, "activity_labels": []}},
            {"name": "B", "community": {"member_count": 5, "is_member": False, "activity_labels": []}},
        ])
        self.assertIn("A", note)
        self.assertNotIn("B", note)

    def test_plain_google_rows_add_nothing(self):
        self.assertEqual(_local_signal_note([{"name": "Kingdom Sushi"}]), "")
        self.assertEqual(_local_signal_note([]), "")
        self.assertEqual(
            _local_signal_note([{"name": "X", "community": {"member_count": 0}}]), ""
        )


if __name__ == "__main__":
    unittest.main()
