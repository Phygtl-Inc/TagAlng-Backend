import unittest
from unittest.mock import MagicMock, patch

from app.zip_unlock import _unlock_snapshot, area_progress, recount_zip


def _rpc_result(data):
    m = MagicMock()
    m.rpc.return_value.execute.return_value = MagicMock(data=data)
    return m


_STATE = {
    "zip5": "32827",
    "state": "warming",
    "previous_state": "warming",
    "count": 7,
    "threshold": 10,
    "opened": False,
}


class TestRecountZip(unittest.TestCase):
    @patch("app.zip_unlock._notify_zip_opened_async")
    @patch("app.zip_unlock.service_client")
    def test_no_push_without_transition(self, sb, notify) -> None:
        sb.return_value = _rpc_result(dict(_STATE))
        state = recount_zip("32827")
        self.assertEqual(state["count"], 7)
        notify.assert_not_called()

    @patch("app.zip_unlock._notify_zip_opened_async")
    @patch("app.zip_unlock.service_client")
    def test_open_transition_fires_push(self, sb, notify) -> None:
        sb.return_value = _rpc_result(
            {**_STATE, "state": "open", "count": 10, "opened": True}
        )
        recount_zip("32827")
        notify.assert_called_once_with("32827")

    @patch("app.zip_unlock._notify_zip_opened_async")
    @patch("app.zip_unlock.service_client")
    def test_suppressed_transition_sends_nothing(self, sb, notify) -> None:
        sb.return_value = _rpc_result(
            {**_STATE, "state": "open", "count": 10, "opened": True}
        )
        state = recount_zip("32827", notify_on_open=False)
        # The transition still happened — only the broadcast is withheld.
        self.assertTrue(state["opened"])
        notify.assert_not_called()

    @patch("app.zip_unlock._notify_zip_opened_async")
    @patch("app.zip_unlock.service_client")
    def test_rpc_failure_returns_none(self, sb, notify) -> None:
        sb.return_value.rpc.side_effect = RuntimeError("boom")
        self.assertIsNone(recount_zip("32827"))
        notify.assert_not_called()

    def test_empty_zip(self) -> None:
        self.assertIsNone(recount_zip(""))


class TestAreaProgress(unittest.TestCase):
    @patch("app.zip_unlock._has_confirmed_thing", return_value=True)
    @patch("app.zip_unlock.recount_zip", return_value=dict(_STATE))
    @patch(
        "app.zip_unlock._user_home_zip",
        return_value={
            "home_zip": "32827",
            "founding_area": None,
            "founding_earned_at": None,
            "phone_verified_at": "2026-07-01T00:00:00Z",
        },
    )
    def test_warming_verified_confirmed_is_founding_eligible(
        self, _profile, _recount, _thing
    ) -> None:
        out = area_progress("u1")
        self.assertEqual(out["state"], "warming")
        self.assertEqual(out["count"], 7)
        self.assertTrue(out["is_founding_eligible"])
        self.assertFalse(out["founding_earned"])

    @patch("app.zip_unlock._has_confirmed_thing", return_value=True)
    @patch(
        "app.zip_unlock.recount_zip",
        return_value={**_STATE, "state": "open", "count": 12},
    )
    @patch(
        "app.zip_unlock._user_home_zip",
        return_value={
            "home_zip": "32827",
            "founding_area": None,
            "founding_earned_at": None,
            "phone_verified_at": "2026-07-01T00:00:00Z",
        },
    )
    def test_open_area_not_newly_eligible(self, _profile, _recount, _thing) -> None:
        out = area_progress("u1")
        self.assertFalse(out["is_founding_eligible"])

    @patch("app.zip_unlock._has_confirmed_thing", return_value=False)
    @patch("app.zip_unlock.recount_zip", return_value=dict(_STATE))
    @patch(
        "app.zip_unlock._user_home_zip",
        return_value={
            "home_zip": "32827",
            "founding_area": "32827",
            "founding_earned_at": "2026-07-20T00:00:00Z",
            "phone_verified_at": "2026-07-01T00:00:00Z",
        },
    )
    def test_earned_founding_sticks(self, _profile, _recount, _thing) -> None:
        out = area_progress("u1")
        self.assertTrue(out["founding_earned"])
        self.assertTrue(out["is_founding_eligible"])

    @patch("app.zip_unlock._user_home_zip", return_value={"home_zip": None})
    def test_no_zip_graceful(self, _profile) -> None:
        out = area_progress("u1")
        self.assertIsNone(out["state"])
        self.assertFalse(out["is_founding_eligible"])


class TestReadPathsNeverBroadcast(unittest.TestCase):
    """A read must never fan a push out to a whole ZIP, even on the transition.

    These assert the *call*, not the effect: the guard is the notify_on_open=False
    each read path passes, so reinstating the default is what has to fail here.
    """

    _OPENED = {**_STATE, "state": "open", "count": 10, "opened": True}

    @patch("app.zip_unlock._has_confirmed_thing", return_value=True)
    @patch("app.zip_unlock._notify_zip_opened_async")
    @patch("app.zip_unlock.service_client")
    @patch(
        "app.zip_unlock._user_home_zip",
        return_value={
            "home_zip": "32827",
            "founding_area": None,
            "founding_earned_at": None,
            "phone_verified_at": "2026-07-01T00:00:00Z",
        },
    )
    def test_area_progress_read_sends_no_push(
        self, _profile, sb, notify, _thing
    ) -> None:
        sb.return_value = _rpc_result(dict(self._OPENED))
        out = area_progress("u1")
        self.assertEqual(out["state"], "open")
        notify.assert_not_called()

    @patch("app.zip_unlock._notify_zip_opened_async")
    @patch("app.zip_unlock.service_client")
    def test_unlock_snapshot_read_repair_sends_no_push(self, sb, notify) -> None:
        client = _rpc_result(dict(self._OPENED))
        # No stored zip_unlock row -> _unlock_snapshot falls through to a recount.
        client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(
            data=[]
        )
        sb.return_value = client
        snap = _unlock_snapshot("32827")
        self.assertEqual(snap["state"], "open")
        notify.assert_not_called()

    # The third call site (circle_invites.redeem_invite) is asserted where its
    # harness already lives: tests/test_circle_invites.py.


if __name__ == "__main__":
    unittest.main()
