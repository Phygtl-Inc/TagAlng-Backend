import unittest
from unittest.mock import MagicMock, patch

from app.event_place import (
    invite_suggestions,
    stamp_event_community,
    stamp_event_community_async,
)


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


class TestStampEventCommunity(unittest.TestCase):
    """The community tag on a published meet (setup card 2/5). The picked id comes from
    the client, so a non-member must neither tag the community nor mail it."""

    def _sb(self, affiliations):
        events, places = _chain(), _chain([{"name": "Lake Nona YMCA"}])
        tables = {
            "circle_affiliations": _chain(affiliations),
            "events": events,
            "places": places,
        }
        sb = MagicMock()
        sb.table.side_effect = lambda name: tables[name]
        return sb, events

    @patch("app.notifications.send_email")
    @patch("app.notifications.recipient_langs", return_value={})
    @patch("app.notifications._user_contact", return_value=("m@x.com", "Ada"))
    @patch("app.event_place.service_client")
    def test_non_member_cannot_tag_or_mail(self, sb, _contact, _langs, mail) -> None:
        client, events = self._sb([])
        sb.return_value = client
        self.assertEqual(stamp_event_community("e1", "p1", "u1", "Coffee walk"), 0)
        events.update.assert_not_called()
        mail.assert_not_called()

    @patch("app.notifications.send_email")
    @patch("app.notifications.recipient_langs", return_value={})
    @patch("app.notifications._user_contact", return_value=("m@x.com", "Ada"))
    @patch("app.event_place.service_client")
    def test_member_tags_and_mails_roster(self, sb, _contact, _langs, mail) -> None:
        client, events = self._sb([{"user_id": "m1"}, {"user_id": "m2"}, {"user_id": "m1"}])
        sb.return_value = client
        sent = stamp_event_community("e1", "p1", "u1", "Coffee walk")
        events.update.assert_called_once_with({"circle_place_ref": "p1"})
        self.assertEqual(sent, 2)  # deduped
        self.assertIn("Lake Nona YMCA", mail.call_args.kwargs["subject"])

    @patch("app.notifications.send_email")
    @patch("app.event_place.service_client")
    def test_no_community_picked_is_a_no_op(self, sb, mail) -> None:
        stamp_event_community_async("e1", None, "u1", "Coffee walk")
        sb.assert_not_called()
        mail.assert_not_called()


if __name__ == "__main__":
    unittest.main()
