"""The other half of "Tell her once. She keeps looking."

The matcher always found these matches the second the other side posted, and queued rows
into match_notifications at strength >= 0.75 — but nothing in the worker ever read that
queue (grep for match_notifications in app/ returned nothing). So the person waiting only
found out if they opened the radar themselves. These tests cover the drain + delivery that
now runs inside the turn which CREATED the match, so no scheduler is involved.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app import signal_match_notify as smn


def _row(**over):
    row = {
        "notification_id": "n1",
        "recipient_user_id": "seeker-1",
        "recipient_ask": "good doctor",
        "recipient_intent": "tip_seek",
        "match_detail": "Dr. Patel on Narcoossee — great with toddlers",
        "match_intent": "tip_share",
        "match_strength": 0.84,
        "block_id": "block-a",
    }
    row.update(over)
    return row


class TestDelivery(unittest.TestCase):
    def setUp(self) -> None:
        self.notify = mock.patch("app.notifications.notify_user").start()
        mock.patch("app.notifications.recipient_langs", return_value={}).start()
        self.addCleanup(mock.patch.stopall)

    def test_the_neighbors_own_words_are_the_body(self) -> None:
        sent = smn._deliver([_row()])

        self.assertEqual(sent, 1)
        self.notify.assert_called_once()
        args, kwargs = self.notify.call_args
        self.assertEqual(args[0], "seeker-1")
        # The tip text is the whole value of this notification, and it is tied back to what
        # THEY asked for so it doesn't read as a random broadcast.
        self.assertIn("Dr. Patel", kwargs["body"])
        self.assertIn("good doctor", kwargs["body"])
        self.assertIn("answered your ask", kwargs["title"])
        self.assertEqual(kwargs["url"], "/chat?panel=radar")
        # Email too — the channel that actually lands, since a verified user always has an
        # address while a push subscription is opt-in and rare.
        self.assertIn("Dr. Patel", kwargs["email_html"])
        # Its subject is emoji-free (deliverability); the emoji stays on the push title.
        self.assertEqual(kwargs["email_subject"], "A neighbor answered your ask")
        self.assertNotIn("💬", kwargs["email_subject"])

    def test_title_follows_the_recipients_own_ask(self) -> None:
        smn._deliver([_row(recipient_intent="swap_seek", match_detail="rain boots 3T")])
        self.assertIn("has what you were after", self.notify.call_args.kwargs["title"])

        self.notify.reset_mock()
        smn._deliver([_row(recipient_intent="meet_seek", match_detail="Sunday badminton")])
        self.assertIn("up for it", self.notify.call_args.kwargs["title"])

    def test_missing_detail_falls_back_without_inventing_one(self) -> None:
        smn._deliver([_row(match_detail="", recipient_ask="")])
        body = self.notify.call_args.kwargs["body"]
        self.assertIn("posted something that fits", body)

    def test_one_bad_recipient_does_not_stop_the_batch(self) -> None:
        self.notify.side_effect = [RuntimeError("push exploded"), None]
        sent = smn._deliver([_row(recipient_user_id="a"), _row(recipient_user_id="b")])
        self.assertEqual(sent, 1)
        self.assertEqual(self.notify.call_count, 2)

    def test_delivered_matches_are_tracked_for_the_funnel(self) -> None:
        """"Connected" in the funnel is one Amplitude event per neighbor actually rung —
        keyed to the recipient, so it stitches to their browser timeline by user_id."""
        with mock.patch("app.analytics.track") as track:
            smn._deliver([_row(), _row(notification_id="n2", recipient_user_id="seeker-2")])

        self.assertEqual([c.args[0] for c in track.call_args_list], ["match_notified"] * 2)
        self.assertEqual(track.call_args_list[0].kwargs["user_id"], "seeker-1")
        self.assertEqual(
            track.call_args_list[0].kwargs["event_properties"]["recipient_intent"], "tip_seek"
        )

    def test_rows_without_a_recipient_are_dropped(self) -> None:
        self.assertEqual(smn._deliver([{"match_detail": "x"}]), 0)
        self.notify.assert_not_called()


class TestDrain(unittest.TestCase):
    def test_drain_delivers_what_the_rpc_returns(self) -> None:
        with mock.patch("app.supabase_rpc.call_rpc", return_value=[_row()]) as rpc, mock.patch.object(
            smn, "_deliver", return_value=1
        ) as deliver:
            smn._drain_and_deliver("jwt", "sig-1")

        rpc.assert_called_once_with(
            "jwt", "drain_signal_match_notifications", {"p_signal_id": "sig-1"}
        )
        deliver.assert_called_once()

    def test_pre_migration_or_rpc_failure_is_silent(self) -> None:
        """The RPC is absent until the migration lands — that must not surface anywhere."""
        with mock.patch(
            "app.supabase_rpc.call_rpc", side_effect=RuntimeError("PGRST202")
        ), mock.patch.object(smn, "_deliver") as deliver:
            smn._drain_and_deliver("jwt", "sig-1")
        deliver.assert_not_called()

    def test_no_matches_sends_nothing(self) -> None:
        with mock.patch("app.supabase_rpc.call_rpc", return_value=[]), mock.patch.object(
            smn, "_deliver"
        ) as deliver:
            smn._drain_and_deliver("jwt", "sig-1")
        deliver.assert_not_called()

    def test_spawn_requires_a_signal_id(self) -> None:
        with mock.patch("threading.Thread") as thread:
            smn.notify_new_signal_matches("jwt", signal_id=None)
            smn.notify_new_signal_matches("jwt", signal_id="")
            thread.assert_not_called()
            smn.notify_new_signal_matches("jwt", signal_id="sig-1")
            thread.assert_called_once()
            # Daemon: the poster's turn never waits on somebody else's push.
            self.assertTrue(thread.call_args.kwargs["daemon"])


class TestWiredIntoTheSave(unittest.TestCase):
    """One wiring point — inside save_local_signal, so no call site can forget it."""

    def setUp(self) -> None:
        self.notify = mock.patch(
            "app.signal_match_notify.notify_new_signal_matches"
        ).start()
        self.addCleanup(mock.patch.stopall)

    def _save(self, rpc_result):
        from app.local_signals import save_local_signal

        with mock.patch("app.local_signals.call_rpc", return_value=rpc_result):
            return save_local_signal(
                "jwt", intent="tip_share", detail_text="Dr. Patel is great", block_id="block-a"
            )

    def test_a_created_match_notifies_the_other_side(self) -> None:
        self._save({"signal_id": "sig-1", "matches_created": 2})
        self.notify.assert_called_once_with("jwt", signal_id="sig-1")

    def test_no_match_notifies_nobody(self) -> None:
        self._save({"signal_id": "sig-1", "matches_created": 0})
        self.notify.assert_not_called()

    def test_a_reused_posting_notifies_nobody(self) -> None:
        """Dedupe returns the existing row with matches_created 0 — re-tapping a chip must
        not re-ping the neighbors who were already told."""
        self._save({"signal_id": "sig-1", "matches_created": 0, "reused": True})
        self.notify.assert_not_called()

    def test_a_broken_notifier_never_breaks_the_save(self) -> None:
        self.notify.side_effect = RuntimeError("boom")
        out = self._save({"signal_id": "sig-1", "matches_created": 1})
        self.assertEqual(out["signal_id"], "sig-1")


class TestSweeper(unittest.TestCase):
    def test_sweeps_and_delivers(self) -> None:
        sb = mock.MagicMock()
        sb.rpc.return_value.execute.return_value = mock.MagicMock(data=[_row()])
        with mock.patch("app.auth.service_client", return_value=sb), mock.patch.object(
            smn, "_deliver", return_value=1
        ) as deliver:
            self.assertEqual(smn.sweep_stale_signal_matches(older_than_minutes=10), 1)
        deliver.assert_called_once()
        self.assertEqual(sb.rpc.call_args[0][0], "drain_stale_match_notifications")

    def test_failure_returns_zero(self) -> None:
        sb = mock.MagicMock()
        sb.rpc.side_effect = RuntimeError("nope")
        with mock.patch("app.auth.service_client", return_value=sb):
            self.assertEqual(smn.sweep_stale_signal_matches(), 0)

    def test_no_service_client_returns_zero(self) -> None:
        with mock.patch("app.auth.service_client", return_value=None):
            self.assertEqual(smn.sweep_stale_signal_matches(), 0)


class TestSweepEndpointAuth(unittest.TestCase):
    """It fans out to OTHER people's recipients, so it is an operator action."""

    def test_refuses_without_a_configured_token(self) -> None:
        from fastapi import HTTPException

        from app.main import hook_signal_matches

        with mock.patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": ""}, clear=False):
            with self.assertRaises(HTTPException) as caught:
                hook_signal_matches(x_sweep_token="anything")
        self.assertEqual(caught.exception.status_code, 403)

    def test_refuses_a_wrong_token(self) -> None:
        from fastapi import HTTPException

        from app.main import hook_signal_matches

        with mock.patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": "right"}, clear=False):
            with self.assertRaises(HTTPException):
                hook_signal_matches(x_sweep_token="wrong")

    def test_runs_with_the_right_token(self) -> None:
        from app.main import hook_signal_matches

        with mock.patch.dict("os.environ", {"SIGNAL_SWEEP_TOKEN": "right"}, clear=False):
            with mock.patch(
                "app.signal_match_notify.sweep_stale_signal_matches", return_value=3
            ):
                out = hook_signal_matches(x_sweep_token="right")
        self.assertEqual(out, {"ok": True, "notified": 3})


if __name__ == "__main__":
    unittest.main()
