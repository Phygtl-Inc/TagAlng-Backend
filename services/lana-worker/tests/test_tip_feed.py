import unittest
from unittest.mock import patch

import app.tip_feed as mod

_RPC_ROW = {
    "signal_id": "11111111-1111-1111-1111-111111111111",
    "detail_text": "Dr. Sarah — so gentle, quick appointments",
    "category": "professionals",
    "affinity_tags": ["gentle", "  ", "quick appts"],
    "created_at": "2026-08-25T10:00:00+00:00",
    "peer_user_id": "22222222-2222-2222-2222-222222222222",
    "neighbor_label": "coral88",
    "avatar_url": "https://…/a.png",
    "distance_meters": 640.0,
    "distance_text": "0.4 mi away",
    "shared_circles": [
        {"place_id": "pl-mary", "name": "St Mary's Church", "circle_type": "faith"},
        {"name": "no id"},
    ],
    "same_block": False,
    "vouch_count": 2,
    "helpful_count": 5,
    "i_vouched": True,
    "i_marked_helpful": False,
}


class TestRecentTips(unittest.TestCase):
    """The browse companion to asking. Until this existed the only way to read a
    neighbour's tip was to ask for one, so the Recent-tips pill had nothing to open."""

    def _tips(self, rows=(_RPC_ROW,), tab="recent"):
        with patch.object(mod, "call_rpc", return_value=list(rows)) as rpc:
            out = mod.recent_tips("jwt", tab=tab)
        return out, rpc

    def test_row_carries_the_card_fields_and_both_counts(self):
        [row], _ = self._tips()
        self.assertEqual(row["nickname"], "coral88")
        self.assertEqual(row["vouch_count"], 2)
        self.assertEqual(row["helpful_count"], 5)
        self.assertTrue(row["i_vouched"])
        self.assertFalse(row["i_marked_helpful"])
        # Blank tags dropped rather than rendered as empty chips.
        self.assertEqual(row["trait_tags"], ["gentle", "quick appts"])

    def test_shared_circle_labels_the_card(self):
        # "My circles" labels each row with the circle it came through — malformed
        # entries are dropped, never rendered as a blank label.
        [row], _ = self._tips()
        self.assertEqual(row["shared_circles"], [
            {"place_id": "pl-mary", "name": "St Mary's Church", "circle_type": "faith"}
        ])

    def test_tab_is_passed_through_and_unknown_falls_back(self):
        _, rpc = self._tips(tab="circles")
        self.assertEqual(rpc.call_args[0][2]["p_filter"], "circles")
        _, rpc = self._tips(tab="whatever-the-client-shipped")
        self.assertEqual(rpc.call_args[0][2]["p_filter"], "recent")

    def test_a_row_with_no_text_is_dropped(self):
        out, _ = self._tips(rows=[{**_RPC_ROW, "detail_text": "  "}])
        self.assertEqual(out, [])

    def test_a_failed_read_is_an_empty_feed_not_an_error(self):
        with patch.object(mod, "call_rpc", side_effect=RuntimeError("postgrest down")):
            self.assertEqual(mod.recent_tips("jwt"), [])


class TestFeedback(unittest.TestCase):
    def test_vouch_sends_the_desired_state_and_returns_the_count(self):
        with patch.object(mod, "call_rpc", return_value=3) as rpc:
            self.assertEqual(mod.set_vouch("jwt", signal_id="s1", on=True), 3)
        self.assertEqual(rpc.call_args[0][2], {"p_signal_id": "s1", "p_on": True})

    def test_unvouch_is_the_same_call_with_false(self):
        # Desired state, not a toggle: a retried tap cannot invert the user's choice.
        with patch.object(mod, "call_rpc", return_value=2) as rpc:
            self.assertEqual(mod.set_vouch("jwt", signal_id="s1", on=False), 2)
        self.assertIs(rpc.call_args[0][2]["p_on"], False)

    def test_helpful_is_a_separate_counter(self):
        with patch.object(mod, "call_rpc", return_value=6) as rpc:
            self.assertEqual(mod.set_helpful("jwt", signal_id="s1"), 6)
        self.assertEqual(rpc.call_args[0][1], "set_tip_helpful")


if __name__ == "__main__":
    unittest.main()
