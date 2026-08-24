"""The nudge notification hook: who gets told, and who cannot make it lie.

The client only sends a nudge_id, so every decision here comes off the DB row. These
tests are about the three ways that could go wrong: telling the wrong side, letting a
stranger trigger a fan-out, and mailing somebody about a decline.
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.main import hook_nudge
from app.models import NudgeHookRequest

SENDER, RECIPIENT, STRANGER = "u-sender", "u-recipient", "u-stranger"


def _sb(row):
    chain = MagicMock()
    for method in ("select", "eq", "single"):
        getattr(chain, method).return_value = chain
    chain.execute.return_value = MagicMock(data=row)
    sb = MagicMock()
    sb.table.return_value = chain
    return sb


INITIATOR, CANDIDATE = "u-initiator", "u-candidate"


def _auth(user_id):
    return MagicMock(user_id=user_id, is_anonymous=False)


class TestNudgeHook(unittest.TestCase):
    def _call(self, *, row, caller, intro_id=None, nudge_id="n1"):
        with (
            patch("app.main.verify_auth", return_value=_auth(caller)),
            patch("app.auth.service_client", return_value=_sb(row)),
            patch("app.notifications._user_contact", return_value=("x@y.com", "Sofia")),
            patch("app.main.recipient_lang", return_value=None),
            patch("app.main.notify_user") as notify,
        ):
            out = hook_nudge(
                NudgeHookRequest(nudge_id=None if intro_id else nudge_id, intro_id=intro_id),
                authorization="Bearer t",
            )
        return out, notify

    def _row(self, status, **over):
        return {
            "sender_id": SENDER,
            "recipient_id": RECIPIENT,
            "status": status,
            "context_message": None,
            **over,
        }

    def test_a_fresh_nudge_tells_the_person_who_was_nudged(self) -> None:
        out, notify = self._call(row=self._row("pending"), caller=SENDER)
        self.assertEqual(out["notified"], "recipient")
        self.assertEqual(notify.call_args[0][0], RECIPIENT)
        self.assertIn("Sofia wants to connect", notify.call_args.kwargs["email_subject"])

    def test_the_senders_own_words_travel_with_it(self) -> None:
        row = self._row("pending", context_message="I run the 5am loop too")
        _, notify = self._call(row=row, caller=SENDER)
        # The message is the body of the push and a quoted fact row in the mail.
        self.assertEqual(notify.call_args.kwargs["body"], "I run the 5am loop too")
        self.assertIn("I run the 5am loop too", notify.call_args.kwargs["email_html"])

    def test_an_acceptance_goes_back_to_whoever_sent_it(self) -> None:
        out, notify = self._call(row=self._row("accepted"), caller=RECIPIENT)
        self.assertEqual(out["notified"], "sender")
        self.assertEqual(notify.call_args[0][0], SENDER)
        self.assertIn("said yes", notify.call_args.kwargs["email_subject"])

    def test_a_decline_notifies_nobody(self) -> None:
        out, notify = self._call(row=self._row("declined"), caller=RECIPIENT)
        self.assertIsNone(out["notified"])
        notify.assert_not_called()

    def test_a_stranger_cannot_trigger_the_fan_out(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(row=self._row("pending"), caller=STRANGER)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_the_recipient_cannot_announce_a_nudge_they_only_received(self) -> None:
        # Only the sender's own call notifies on 'pending' — otherwise a recipient could
        # re-ring their own inbox by replaying the hook.
        out, notify = self._call(row=self._row("pending"), caller=RECIPIENT)
        self.assertIsNone(out["notified"])
        notify.assert_not_called()

    def test_the_sender_cannot_fake_their_own_acceptance(self) -> None:
        out, notify = self._call(row=self._row("accepted"), caller=SENDER)
        self.assertIsNone(out["notified"])
        notify.assert_not_called()

    def test_an_unknown_nudge_is_a_no_op_not_a_403(self) -> None:
        # A deleted nudge is not an authorization problem — a hook the client fires and
        # forgets should not turn a stale id into a client error.
        out, notify = self._call(row={}, caller=SENDER)
        self.assertEqual(out, {"ok": False})
        notify.assert_not_called()


class TestAcceptedIntroHook(unittest.TestCase):
    """propose_intro writes an intros row AND a nudges row at the same instant, and the
    Chats drawer accepts the INTRO — so the reply has to work off the intro id too. This
    is the exact hole that shipped: the initiator heard nothing back."""

    def _call(self, *, row, caller):
        with (
            patch("app.main.verify_auth", return_value=_auth(caller)),
            patch("app.auth.service_client", return_value=_sb(row)),
            patch("app.notifications._user_contact", return_value=("x@y.com", "Asjid")),
            patch("app.main.recipient_lang", return_value=None),
            patch("app.main.notify_user") as notify,
        ):
            out = hook_nudge(
                NudgeHookRequest(intro_id="i1"), authorization="Bearer t"
            )
        return out, notify

    def _row(self, status):
        return {"initiator_id": INITIATOR, "candidate_id": CANDIDATE, "status": status}

    def test_an_accepted_intro_tells_whoever_started_it(self) -> None:
        out, notify = self._call(row=self._row("accepted"), caller=CANDIDATE)
        self.assertEqual(out["notified"], "sender")
        self.assertEqual(notify.call_args[0][0], INITIATOR)
        self.assertIn("Asjid said yes", notify.call_args.kwargs["email_subject"])

    def test_a_proposed_intro_is_not_an_acceptance(self) -> None:
        out, notify = self._call(row=self._row("proposed"), caller=CANDIDATE)
        self.assertIsNone(out["notified"])
        notify.assert_not_called()

    def test_the_initiator_cannot_accept_on_the_candidates_behalf(self) -> None:
        out, notify = self._call(row=self._row("accepted"), caller=INITIATOR)
        self.assertIsNone(out["notified"])
        notify.assert_not_called()

    def test_a_stranger_is_refused(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(row=self._row("accepted"), caller=STRANGER)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_a_call_with_no_id_at_all_is_a_no_op(self) -> None:
        with (
            patch("app.main.verify_auth", return_value=_auth(CANDIDATE)),
            patch("app.auth.service_client", return_value=_sb({})),
            patch("app.main.notify_user") as notify,
        ):
            out = hook_nudge(NudgeHookRequest(), authorization="Bearer t")
        self.assertEqual(out, {"ok": False})
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
