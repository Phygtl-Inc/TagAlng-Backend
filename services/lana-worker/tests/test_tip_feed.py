import unittest
from unittest.mock import patch

import app.tip_feed as mod

_RPC_ROW = {
    "signal_id": "11111111-1111-1111-1111-111111111111",
    "reco_name": "Dr. Sarah",
    "category": "pediatric dentist",
    "reco_type": "professional",
    "reco_place": "Lake Nona",
    "reco_description": "So gentle — quick appointments",
    "reco_fields": [
        {"field": "profession", "label": "Profession", "question": "What do they do?",
         "kind": "text", "answer": "Pediatric dentist"},
        {"field": "helped_with", "label": "  ", "answer": "a chipped tooth"},
        {"field": "stood_out", "label": "Stood out", "answer": "   "},
    ],
    "detail_text": "Dr. Sarah · pediatric dentist · so gentle · Lake Nona",
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
    "helpful_count": 5,
    "unhelpful_count": 1,
    "i_marked_helpful": False,
    "i_marked_unhelpful": True,
}


class TestRecentTips(unittest.TestCase):
    """The browse companion to asking. Until this existed the only way to read a
    neighbour's tip was to ask for one, so the Recent-tips pill had nothing to open."""

    def _tips(self, rows=(_RPC_ROW,), tab="recent"):
        with patch.object(mod, "call_rpc", return_value=list(rows)) as rpc:
            out = mod.recent_tips("jwt", tab=tab)
        return out, rpc

    def test_the_card_head_is_fields_not_a_sentence(self):
        # The whole point of 20261120120000: a card renders name / category / place /
        # description without splitting detail_text on " · " and hoping.
        [row], _ = self._tips()
        self.assertEqual(row["name"], "Dr. Sarah")
        self.assertEqual(row["category"], "pediatric dentist")
        self.assertEqual(row["reco_type"], "professional")
        self.assertEqual(row["place"], "Lake Nona")
        self.assertEqual(row["description"], "So gentle — quick appointments")
        self.assertEqual(row["nickname"], "coral88")

    def test_answered_steps_come_back_labelled_and_blanks_are_dropped(self):
        [row], _ = self._tips()
        self.assertEqual(row["fields"], [
            {"field": "profession", "label": "Profession", "question": "What do they do?",
             "kind": "text", "answer": "Pediatric dentist"},
        ])

    def test_place_falls_back_to_the_where_answer(self):
        # Only when the author named no locality — their own words win.
        rows = [{**_RPC_ROW, "reco_place": None, "reco_fields": [
            {"field": "where", "label": "Location", "answer": "Laureate Park"},
        ]}]
        [row], _ = self._tips(rows=rows)
        self.assertEqual(row["place"], "Laureate Park")

    def test_both_helpful_directions_are_carried(self):
        [row], _ = self._tips()
        self.assertEqual(row["helpful_count"], 5)
        self.assertEqual(row["unhelpful_count"], 1)
        self.assertFalse(row["i_marked_helpful"])
        self.assertTrue(row["i_marked_unhelpful"])
        # Retired with the vouch: no card reads either any more.
        self.assertNotIn("vouch_count", row)
        self.assertNotIn("i_vouched", row)
        self.assertNotIn("trait_tags", row)

    def test_shared_circle_labels_the_card(self):
        # "My circles" labels each row with the circle it came through — malformed
        # entries are dropped, never rendered as a blank label.
        [row], _ = self._tips()
        self.assertEqual(row["shared_circles"], [
            {"place_id": "pl-mary", "name": "St Mary's Church", "circle_type": "faith"}
        ])

    def test_a_legacy_row_still_renders_off_detail_text(self):
        # Tips captured before the fields existed: name was backfilled out of the
        # sentence, the sentence itself stays as the description of last resort.
        rows = [{k: v for k, v in _RPC_ROW.items()
                 if k not in ("reco_place", "reco_description", "reco_fields", "reco_type")}]
        [row], _ = self._tips(rows=rows)
        self.assertEqual(row["name"], "Dr. Sarah")
        self.assertIsNone(row["description"])
        self.assertEqual(row["fields"], [])
        self.assertTrue(row["detail_text"])

    def test_tab_is_passed_through_and_unknown_falls_back(self):
        _, rpc = self._tips(tab="circles")
        self.assertEqual(rpc.call_args[0][2]["p_filter"], "circles")
        _, rpc = self._tips(tab="whatever-the-client-shipped")
        self.assertEqual(rpc.call_args[0][2]["p_filter"], "recent")

    def test_a_row_with_nothing_to_title_it_is_dropped(self):
        out, _ = self._tips(rows=[{**_RPC_ROW, "reco_name": " ", "detail_text": "  "}])
        self.assertEqual(out, [])

    def test_a_failed_read_is_an_empty_feed_not_an_error(self):
        with patch.object(mod, "call_rpc", side_effect=RuntimeError("postgrest down")):
            self.assertEqual(mod.recent_tips("jwt"), [])


class TestFeedback(unittest.TestCase):
    _COUNTS = {
        "helpful_count": 6,
        "unhelpful_count": 2,
        "i_marked_helpful": True,
        "i_marked_unhelpful": False,
    }

    def test_a_vote_sends_its_direction_and_returns_both_counts(self):
        with patch.object(mod, "call_rpc", return_value=dict(self._COUNTS)) as rpc:
            out = mod.set_helpful("jwt", signal_id="s1")
        self.assertEqual(rpc.call_args[0][1], "set_tip_helpful")
        self.assertEqual(rpc.call_args[0][2],
                         {"p_signal_id": "s1", "p_on": True, "p_helpful": True})
        self.assertEqual(out, self._COUNTS)

    def test_unhelpful_is_the_same_call_pointed_the_other_way(self):
        with patch.object(mod, "call_rpc", return_value={}) as rpc:
            out = mod.set_helpful("jwt", signal_id="s1", helpful=False)
        self.assertIs(rpc.call_args[0][2]["p_helpful"], False)
        self.assertIs(rpc.call_args[0][2]["p_on"], True)
        self.assertEqual(out["helpful_count"], 0)

    def test_clearing_is_desired_state_not_a_toggle(self):
        # A retried tap must not invert what the reader chose.
        with patch.object(mod, "call_rpc", return_value={}) as rpc:
            mod.set_helpful("jwt", signal_id="s1", on=False)
        self.assertIs(rpc.call_args[0][2]["p_on"], False)


if __name__ == "__main__":
    unittest.main()
