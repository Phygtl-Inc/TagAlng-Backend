import unittest
from unittest.mock import MagicMock, patch

from app.event_place import invite_suggestions


def _chain(data=None):
    m = MagicMock()
    for method in ("select", "eq", "neq", "is_", "in_", "limit", "insert", "update"):
        getattr(m, method).return_value = m
    m.execute.return_value = MagicMock(data=data or [])
    return m


class TestInviteSuggestions(unittest.TestCase):
    def _sb(self, event, members=None, users=None):
        sb = MagicMock()
        tables = {
            "events": _chain([event] if event else []),
            "circle_affiliations": _chain(members or []),
            "users": _chain(users or []),
        }
        sb.table.side_effect = lambda name: tables[name]
        return sb

    @patch("app.event_place.service_client")
    def test_unknown_event(self, sb) -> None:
        sb.return_value = self._sb(None)
        with self.assertRaises(ValueError) as ctx:
            invite_suggestions("u1", "e1")
        self.assertEqual(str(ctx.exception), "event_not_found")

    @patch("app.event_place.service_client")
    def test_host_only(self, sb) -> None:
        sb.return_value = self._sb({"id": "e1", "host_id": "other", "place_ref": "p1"})
        with self.assertRaises(ValueError) as ctx:
            invite_suggestions("u1", "e1")
        self.assertEqual(str(ctx.exception), "not_event_host")

    @patch("app.event_place.service_client")
    def test_place_free_event(self, sb) -> None:
        sb.return_value = self._sb({"id": "e1", "host_id": "u1", "place_ref": None})
        with self.assertRaises(ValueError) as ctx:
            invite_suggestions("u1", "e1")
        self.assertEqual(str(ctx.exception), "event_has_no_place")

    @patch("app.event_place.service_client")
    def test_members_with_nicknames(self, sb) -> None:
        sb.return_value = self._sb(
            {"id": "e1", "host_id": "u1", "place_ref": "p1"},
            members=[{"user_id": "m1"}, {"user_id": "m2"}, {"user_id": "m1"}],
            users=[{"id": "m1", "nickname": "Ada"}, {"id": "m2", "nickname": None}],
        )
        out = invite_suggestions("u1", "e1")
        self.assertEqual(out["count"], 2)  # deduped
        self.assertEqual(out["members"][0]["nickname"], "Ada")
        self.assertEqual(out["members"][1]["nickname"], "A neighbor")


if __name__ == "__main__":
    unittest.main()
