import unittest
from unittest.mock import MagicMock, patch

import app.place_local_signal as mod


def _sb(rows):
    sb = MagicMock()
    sb.rpc.return_value.execute.return_value.data = rows
    return sb


_ROW = {
    "google_place_id": "g-mizu",
    "place_id": "11111111-1111-1111-1111-111111111111",
    "member_count": 3,
    "is_member": True,
    "activity_labels": ["Sushi Making Classes", "Hibachi"],
}


class TestStampLocalSignal(unittest.TestCase):
    """A Google row for a spot neighbours actually belong to must say so — that fact is
    weaker than a posted tip but stronger than a stranger's rating, and we already hold it."""

    def _stamp(self, places, rows=(_ROW,)):
        with patch.object(mod, "service_client", return_value=_sb(list(rows))):
            mod.stamp_local_signal(places, user_id="u1")
        return places

    def test_matching_row_is_stamped(self):
        places = [{"name": "Mizu", "place_id": "g-mizu"}]
        self._stamp(places)
        self.assertEqual(places[0]["community"]["member_count"], 3)
        self.assertTrue(places[0]["community"]["is_member"])
        self.assertEqual(
            places[0]["community"]["activity_labels"], ["Sushi Making Classes", "Hibachi"]
        )

    def test_unknown_place_is_left_alone(self):
        places = [{"name": "Somewhere", "place_id": "g-other"}]
        self._stamp(places)
        self.assertNotIn("community", places[0])

    def test_no_prose_in_the_payload(self):
        # Structured only — a baked English badge here would be untranslatable on the wire.
        places = [{"name": "Mizu", "place_id": "g-mizu"}]
        self._stamp(places)
        self.assertEqual(
            set(places[0]["community"]),
            {"place_id", "member_count", "is_member", "activity_labels"},
        )

    def test_lookup_failure_never_breaks_the_recommendation(self):
        places = [{"name": "Mizu", "place_id": "g-mizu"}]
        with patch.object(mod, "service_client", side_effect=RuntimeError("db down")):
            mod.stamp_local_signal(places, user_id="u1")
        self.assertNotIn("community", places[0])

    def test_anonymous_caller_skips_the_lookup(self):
        # The RPC is scoped to a caller (blocked members are excluded per user), so there
        # is nothing honest to compute without one.
        sb = _sb([_ROW])
        with patch.object(mod, "service_client", return_value=sb):
            mod.stamp_local_signal([{"name": "Mizu", "place_id": "g-mizu"}], user_id=None)
        sb.rpc.assert_not_called()



class TestCommunitiesForRequest(unittest.TestCase):
    """Stamping can only mark what Google returned. Asked for a reading spot it returned
    three coffee shops, so the library neighbours actually read at was never in the list
    to be marked (prod 2026-08-19). This is the finder, not the labeller."""

    _RPC_ROW = {
        "place_id": "22222222-2222-2222-2222-222222222222",
        "google_place_id": "g-opl",
        "name": "Orlando Public Library",
        "address": "101 E Central Blvd",
        "member_count": 2,
        "is_member": False,
        "matched_label": "Weekly reading session",
        "activity_labels": ["Author talk", "Weekly reading session"],
    }

    def _find(self, rows=(_RPC_ROW,)):
        with patch.object(mod, "service_client", return_value=_sb(list(rows))), patch(
            "app.layer1_handlers._embed_attr_filter", return_value=[0.1] * 768
        ):
            return mod.communities_for_request("place to read with kids", user_id="u1")

    def test_a_community_is_shaped_like_a_place_row(self):
        [row] = self._find()
        self.assertEqual(row["name"], "Orlando Public Library")
        self.assertEqual(row["place_id"], "g-opl")  # google id: the maps link needs it
        self.assertEqual(row["community"]["matched_label"], "Weekly reading session")
        self.assertEqual(row["community"]["member_count"], 2)

    def test_no_embedding_means_no_guessing(self):
        with patch("app.layer1_handlers._embed_attr_filter", return_value=None):
            self.assertEqual(mod.communities_for_request("x", user_id="u1"), [])

    def test_rpc_failure_never_breaks_the_turn(self):
        with patch.object(mod, "service_client", side_effect=RuntimeError("no such function")), \
             patch("app.layer1_handlers._embed_attr_filter", return_value=[0.1] * 768):
            self.assertEqual(mod.communities_for_request("x", user_id="u1"), [])


class TestMergeCommunitiesFirst(unittest.TestCase):
    def test_communities_lead_and_google_fills(self):
        merged = mod.merge_communities_first(
            [{"name": "Cafe", "place_id": "g1"}],
            [{"name": "Library", "place_id": "g9"}],
        )
        self.assertEqual([r["name"] for r in merged], ["Library", "Cafe"])

    def test_the_same_spot_is_never_listed_twice(self):
        # Google returned it too — the community row wins, it carries the proof line.
        merged = mod.merge_communities_first(
            [{"name": "Mizu", "place_id": "g2"}, {"name": "Cafe", "place_id": "g1"}],
            [{"name": "Mizu Sushi", "place_id": "g2", "community": {"member_count": 1}}],
        )
        self.assertEqual([r["name"] for r in merged], ["Mizu Sushi", "Cafe"])

    def test_no_communities_leaves_google_untouched(self):
        rows = [{"name": "Cafe", "place_id": "g1"}]
        self.assertEqual(mod.merge_communities_first(rows, []), rows)

if __name__ == "__main__":
    unittest.main()
