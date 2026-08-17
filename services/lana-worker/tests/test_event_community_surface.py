"""The community a meet was created for, on the surfaces that show the meet
(20261015120000). The RPC side is SQL; these cover the python that formats and
fans it out."""

import unittest
from unittest.mock import MagicMock, patch

from app.discovery_route import activity_previews_from_events
from app.event_place import community_label, community_line, event_community

FITNESS = {
    "place_ref": "p1",
    "name": "Fitness CF",
    "emoji": "🏋️",
    "circle_type": "fitness",
    "detail": "Zumba",
}


class TestCommunityLine(unittest.TestCase):
    def test_emoji_and_name(self) -> None:
        self.assertEqual(community_line(FITNESS), "🏋️ Fitness CF")

    def test_name_only_when_no_glyph(self) -> None:
        self.assertEqual(community_line({**FITNESS, "emoji": None}), "Fitness CF")

    def test_no_community_no_line(self) -> None:
        self.assertIsNone(community_line(None))
        # A row with no name is not a community we can honestly name.
        self.assertIsNone(community_line({"place_ref": "p1", "name": ""}))


class TestEventCommunity(unittest.TestCase):
    @patch("app.event_place.service_client")
    def test_plain_neighborhood_meet_skips_the_rpc(self, sb) -> None:
        self.assertIsNone(event_community(None))
        self.assertIsNone(event_community("  "))
        sb.assert_not_called()

    @patch("app.event_place.service_client")
    def test_resolved_from_the_rpc(self, sb) -> None:
        sb.return_value.rpc.return_value.execute.return_value = MagicMock(data=FITNESS)
        self.assertEqual(community_label("p1", "host"), "🏋️ Fitness CF")
        sb.return_value.rpc.assert_called_once_with(
            "event_community", {"p_place_ref": "p1", "p_host_id": "host"}
        )

    @patch("app.event_place.service_client")
    def test_rpc_failure_just_drops_the_tag(self, sb) -> None:
        sb.return_value.rpc.side_effect = RuntimeError("boom")
        self.assertIsNone(event_community("p1"))


class TestBrowseRows(unittest.TestCase):
    @patch("app.event_place.event_community")
    def test_rows_carry_the_community_resolved_once_per_place(self, resolve) -> None:
        resolve.return_value = FITNESS
        rows = activity_previews_from_events(
            [
                {"id": "e1", "title": "Zumba", "host_id": "h", "circle_place_ref": "p1"},
                {"id": "e2", "title": "Coffee", "host_id": "h", "circle_place_ref": "p1"},
                {"id": "e3", "title": "Walk", "host_id": "h"},
            ]
        )
        self.assertEqual([r["community"] for r in rows], [FITNESS, FITNESS, None])
        # Two rows, one community → one lookup.
        self.assertEqual(resolve.call_count, 1)


class TestNotificationNote(unittest.TestCase):
    def test_note_only_when_there_is_a_community(self) -> None:
        from app.main import _community_note

        self.assertEqual(
            _community_note("🏋️ Fitness CF", "en"), "Community · 🏋️ Fitness CF"
        )
        self.assertIsNone(_community_note(None, "en"))


if __name__ == "__main__":
    unittest.main()
