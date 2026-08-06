import unittest
from unittest.mock import MagicMock, patch

from app.circle_invites import (
    contribution_count,
    mint_invite,
    redeem_invite,
    self_confirm,
)


def _chain(data=None, count=None):
    m = MagicMock()
    for method in (
        "select", "eq", "is_", "not_", "gte", "limit", "insert", "update", "or_",
    ):
        getattr(m, method).return_value = m
    m.not_.is_.return_value = m
    m.execute.return_value = MagicMock(data=data or [], count=count)
    return m


_INVITE = {
    "id": "inv1",
    "owner_user_id": "owner",
    "circle_type": "faith",
    "circle_key": "church",
    "place_ref": "p1",
    "revoked_at": None,
}


class TestMint(unittest.TestCase):
    @patch("app.circle_invites._invite_url", return_value="https://x/i/tok")
    @patch("app.circle_invites.service_client")
    def test_mint_unlabeled(self, sb, _url) -> None:
        table = _chain()
        sb.return_value.table.return_value = table
        out = mint_invite("u1")
        self.assertIn("token", out)
        row = table.insert.call_args[0][0]
        self.assertIsNone(row["circle_type"])
        self.assertEqual(row["owner_user_id"], "u1")

    @patch("app.circle_invites._invite_url", return_value="https://x/i/tok")
    @patch("app.circle_invites.service_client")
    def test_mint_labeled_carries_type_and_place(self, sb, _url) -> None:
        table = _chain([{"circle_type": "faith", "place_ref": "p1"}])
        sb.return_value.table.return_value = table
        mint_invite("u1", circle_key="church")
        row = table.insert.call_args[0][0]
        self.assertEqual(row["circle_type"], "faith")
        self.assertEqual(row["place_ref"], "p1")

    @patch("app.circle_invites.service_client")
    def test_mint_unknown_circle_raises(self, sb) -> None:
        table = _chain([])
        sb.return_value.table.return_value = table
        with self.assertRaises(ValueError):
            mint_invite("u1", circle_key="nope")


class TestRedeem(unittest.TestCase):
    @patch("app.circle_invites._active_invite", return_value=None)
    def test_unknown_token_raises(self, _inv) -> None:
        with self.assertRaises(ValueError):
            redeem_invite("u1", "tok")

    @patch("app.circle_invites._active_invite", return_value=dict(_INVITE))
    def test_self_redeem_is_noop(self, _inv) -> None:
        out = redeem_invite("owner", "tok")
        self.assertFalse(out["confirm_prompt"])

    @patch("app.circle_invites._rate_limited", return_value=True)
    @patch("app.circle_invites._active_invite", return_value=dict(_INVITE))
    def test_rate_limited(self, _inv, _rl) -> None:
        with self.assertRaises(ValueError) as ctx:
            redeem_invite("u2", "tok")
        self.assertEqual(str(ctx.exception), "invite_rate_limited")

    @patch("app.zip_unlock.recount_zip")
    @patch("app.circle_invites._rate_limited", return_value=False)
    @patch("app.circle_invites._active_invite", return_value=dict(_INVITE))
    @patch("app.circle_invites.service_client")
    def test_redeem_sets_invited_by_once_and_recounts(
        self, sb, _inv, _rl, recount
    ) -> None:
        users = _chain([{"invited_by": None, "home_zip": "32827"}])
        redemptions = _chain()
        sb.return_value.table.side_effect = lambda name: {
            "circle_invite_redemptions": redemptions,
            "users": users,
        }[name]
        out = redeem_invite("u2", "tok")
        self.assertTrue(out["confirm_prompt"])
        self.assertEqual(out["circle_type"], "faith")
        self.assertEqual(users.update.call_args[0][0], {"invited_by": "owner"})
        # Suppressed: attribution counts, but a redemption must not broadcast the
        # open transition to a whole ZIP. Announcing needs a deliberate owner.
        recount.assert_called_once_with("32827", notify_on_open=False)

    @patch("app.zip_unlock.recount_zip")
    @patch("app.circle_invites._rate_limited", return_value=False)
    @patch("app.circle_invites._active_invite", return_value=dict(_INVITE))
    @patch("app.circle_invites.service_client")
    def test_first_inviter_wins(self, sb, _inv, _rl, _recount) -> None:
        users = _chain([{"invited_by": "someone_else", "home_zip": None}])
        redemptions = _chain()
        sb.return_value.table.side_effect = lambda name: {
            "circle_invite_redemptions": redemptions,
            "users": users,
        }[name]
        redeem_invite("u2", "tok")
        users.update.assert_not_called()

    @patch("app.circle_invites._rate_limited", return_value=False)
    @patch(
        "app.circle_invites._active_invite",
        return_value={**_INVITE, "circle_type": None},
    )
    @patch("app.circle_invites.service_client")
    def test_unlabeled_invite_no_prompt(self, sb, _inv, _rl) -> None:
        users = _chain([{"invited_by": None, "home_zip": None}])
        sb.return_value.table.side_effect = lambda name: {
            "circle_invite_redemptions": _chain(),
            "users": users,
        }[name]
        out = redeem_invite("u2", "tok")
        self.assertFalse(out["confirm_prompt"])
        self.assertIsNone(out["circle_type"])


class TestSelfConfirm(unittest.TestCase):
    @patch("app.circles_flow.add_circle", return_value={"affiliation_id": "a1"})
    @patch("app.circle_invites._active_invite", return_value=dict(_INVITE))
    def test_writes_invite_confirmed_with_inviter(self, _inv, add) -> None:
        self_confirm("u2", "tok", circle_type="faith", detail="my parish")
        kwargs = add.call_args.kwargs
        self.assertEqual(kwargs["source"], "invite_confirmed")
        self.assertEqual(kwargs["invited_by"], "owner")
        # The joiner's own words only — the inviter's place never crosses over.
        self.assertEqual(kwargs["detail"], "my parish")

    @patch("app.circle_invites._active_invite", return_value=dict(_INVITE))
    def test_owner_cannot_self_confirm(self, _inv) -> None:
        with self.assertRaises(ValueError):
            self_confirm("owner", "tok", circle_type="faith")


class TestContribution(unittest.TestCase):
    @patch("app.circle_invites.service_client")
    def test_counts_verified_invitees(self, sb) -> None:
        table = _chain(count=4)
        sb.return_value.table.return_value = table
        self.assertEqual(contribution_count("u1"), 4)


if __name__ == "__main__":
    unittest.main()
