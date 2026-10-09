"""attribute_join: a join of an invite's community is credited to that invite
(20270201120000) — and the Join endpoint passes invite_token through."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app import circle_invites
from app.circle_invites import attribute_join, invite_link

_INVITE = {
    "id": "inv1",
    "owner_user_id": "owner",
    "circle_type": "other",
    "circle_key": "club",
    "place_ref": "p1",
    "revoked_at": None,
}


class _Q:
    """A tiny PostgREST-ish builder over in-memory rows: enough filtering that a
    missing guard (`is_("invite_id", "null")`, `eq("user_id", …)`) changes results."""

    def __init__(self, db: "_DB", table: str) -> None:
        self.db, self.table = db, table
        self.filters: list[tuple[str, str, Any]] = []
        self.op = "select"
        self.payload: dict[str, Any] | None = None

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def gte(self, *_a):
        return self

    def insert(self, row):
        self.op, self.payload = "insert", dict(row)
        return self

    def update(self, patch_):
        self.op, self.payload = "update", dict(patch_)
        return self

    def _match(self, row) -> bool:
        for kind, col, val in self.filters:
            if kind == "eq" and str(row.get(col)) != str(val):
                return False
            if kind == "is" and val == "null" and row.get(col) is not None:
                return False
        return True

    def execute(self):
        rows = self.db.tables.setdefault(self.table, [])
        if self.table in self.db.fail:
            raise RuntimeError("boom")
        if self.op == "insert":
            if self.table == "circle_invite_redemptions" and any(
                r["invite_id"] == self.payload["invite_id"]
                and r["user_id"] == self.payload["user_id"]
                for r in rows
            ):
                raise RuntimeError("duplicate key")
            row = {"id": f"{self.table}-{len(rows) + 1}", **self.payload}
            rows.append(row)
            self.db.writes.append((self.table, "insert", self.payload))
            return SimpleNamespace(data=[row], count=None)
        hit = [r for r in rows if self._match(r)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            self.db.writes.append((self.table, "update", self.payload))
        return SimpleNamespace(data=[dict(r) for r in hit], count=len(hit))


class _DB:
    def __init__(self, **tables) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            k: [dict(r) for r in v] for k, v in tables.items()
        }
        self.writes: list[tuple[str, str, dict]] = []
        self.fail: set[str] = set()

    def table(self, name):
        return _Q(self, name)

    def rows(self, name):
        return self.tables.get(name, [])


def _aff(**kw):
    return {
        "id": "aff1",
        "user_id": "u2",
        "place_ref": "p1",
        "invite_id": None,
        "invited_by": None,
        "dismissed_at": None,
        **kw,
    }


class TestAttributeJoin(unittest.TestCase):
    def _run(self, db, *, invite=_INVITE, user="u2", place="p1", rate=False, **kw):
        with (
            patch.object(circle_invites, "service_client", return_value=db),
            patch.object(
                circle_invites, "_active_invite", return_value=dict(invite) if invite else None
            ),
            patch.object(circle_invites, "_rate_limited", return_value=rate),
        ):
            return attribute_join(user, "tok", place, **kw)

    def test_match_creates_redemption_sets_invited_by_and_stamps_affiliation(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}], circle_affiliations=[_aff()])
        out = self._run(db, affiliation_id="aff1")
        self.assertEqual(
            out, {"invite_id": "inv1", "inviter_user_id": "owner", "redemption_created": True}
        )
        (red,) = db.rows("circle_invite_redemptions")
        self.assertEqual(red["invite_id"], "inv1")
        self.assertEqual(red["user_id"], "u2")
        self.assertTrue(red["joined_at"])
        self.assertEqual(red["joined_place_ref"], "p1")
        self.assertEqual(db.rows("users")[0]["invited_by"], "owner")
        aff = db.rows("circle_affiliations")[0]
        self.assertEqual((aff["invite_id"], aff["invited_by"]), ("inv1", "owner"))

    def test_existing_redemption_is_updated_not_duplicated(self) -> None:
        db = _DB(
            circle_invite_redemptions=[
                {"id": "r1", "invite_id": "inv1", "user_id": "u2", "joined_at": None}
            ],
            users=[{"id": "u2", "invited_by": "owner"}],
        )
        out = self._run(db)
        self.assertFalse(out["redemption_created"])
        (red,) = db.rows("circle_invite_redemptions")
        self.assertTrue(red["joined_at"])
        self.assertEqual(red["joined_place_ref"], "p1")

    def test_existing_joined_at_keeps_first_join(self) -> None:
        db = _DB(
            circle_invite_redemptions=[
                {"id": "r1", "invite_id": "inv1", "user_id": "u2", "joined_at": "2026-01-01"}
            ],
            users=[{"id": "u2", "invited_by": "owner"}],
        )
        self._run(db)
        self.assertEqual(db.rows("circle_invite_redemptions")[0]["joined_at"], "2026-01-01")

    def test_wrong_place_is_ignored(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}], circle_affiliations=[_aff(place_ref="p9")])
        self.assertIsNone(self._run(db, place="p9"))
        self.assertEqual(db.writes, [])

    def test_unlabeled_invite_is_ignored(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}])
        self.assertIsNone(self._run(db, invite={**_INVITE, "place_ref": None}))
        self.assertEqual(db.writes, [])

    def test_self_invite_is_ignored(self) -> None:
        db = _DB(users=[{"id": "owner", "invited_by": None}])
        self.assertIsNone(self._run(db, user="owner"))
        self.assertEqual(db.writes, [])

    def test_revoked_or_unknown_invite_is_ignored(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}])
        self.assertIsNone(self._run(db, invite=None))
        self.assertEqual(db.writes, [])

    def test_revoked_invite_via_real_lookup(self) -> None:
        db = _DB(
            circle_invites=[{**_INVITE, "token": "tok", "revoked_at": "2026-10-01"}],
            users=[{"id": "u2", "invited_by": None}],
        )
        with patch.object(circle_invites, "service_client", return_value=db):
            self.assertIsNone(attribute_join("u2", "tok", "p1"))
        self.assertEqual(db.writes, [])

    def test_invited_by_set_only_when_null(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": "earlier"}], circle_affiliations=[_aff()])
        self._run(db)
        self.assertEqual(db.rows("users")[0]["invited_by"], "earlier")
        self.assertFalse([w for w in db.writes if w[0] == "users"])

    def test_affiliation_stamped_only_when_empty(self) -> None:
        db = _DB(
            users=[{"id": "u2", "invited_by": None}],
            circle_affiliations=[_aff(invite_id="inv0", invited_by="first")],
        )
        self.assertIsNotNone(self._run(db))
        aff = db.rows("circle_affiliations")[0]
        self.assertEqual((aff["invite_id"], aff["invited_by"]), ("inv0", "first"))
        self.assertFalse([w for w in db.writes if w[0] == "circle_affiliations"])

    def test_affiliation_not_stamped_when_a_candidate_holds_this_invite(self) -> None:
        # unique(user_id, invite_id) on live rows: the self-confirm candidate already
        # carries the attribution; stamping the joined row too would violate it.
        db = _DB(
            users=[{"id": "u2", "invited_by": None}],
            circle_affiliations=[
                _aff(),
                _aff(id="aff0", place_ref=None, invite_id="inv1", invited_by="owner"),
            ],
        )
        self._run(db, affiliation_id="aff1")
        self.assertIsNone(db.rows("circle_affiliations")[0]["invite_id"])

    def test_other_users_affiliation_untouched(self) -> None:
        db = _DB(
            users=[{"id": "u2", "invited_by": None}],
            circle_affiliations=[_aff(user_id="someone")],
        )
        self._run(db)
        self.assertIsNone(db.rows("circle_affiliations")[0]["invite_id"])

    def test_rate_limit_blocks_new_rows_only(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}])
        self.assertIsNone(self._run(db, rate=True))
        self.assertEqual(db.rows("circle_invite_redemptions"), [])
        db2 = _DB(
            circle_invite_redemptions=[
                {"id": "r1", "invite_id": "inv1", "user_id": "u2", "joined_at": None}
            ],
            users=[{"id": "u2", "invited_by": None}],
        )
        self.assertIsNotNone(self._run(db2, rate=True))
        self.assertTrue(db2.rows("circle_invite_redemptions")[0]["joined_at"])

    def test_exceptions_are_swallowed(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}])
        db.fail.add("circle_invite_redemptions")
        self.assertIsNone(self._run(db))
        with (
            patch.object(circle_invites, "_active_invite", side_effect=RuntimeError("x")),
        ):
            self.assertIsNone(attribute_join("u2", "tok", "p1"))

    def test_affiliation_failure_does_not_lose_redemption(self) -> None:
        db = _DB(users=[{"id": "u2", "invited_by": None}])
        db.fail.add("circle_affiliations")
        out = self._run(db)
        self.assertIsNotNone(out)
        self.assertTrue(db.rows("circle_invite_redemptions")[0]["joined_at"])

    def test_blank_token_or_place(self) -> None:
        db = _DB()
        with patch.object(circle_invites, "service_client", return_value=db):
            self.assertIsNone(attribute_join("u2", "", "p1"))
        self.assertIsNone(self._run(db, place=""))


class TestInviteLink(unittest.TestCase):
    def test_link_from_rpc(self) -> None:
        sb = SimpleNamespace(
            rpc=lambda name, args: SimpleNamespace(
                execute=lambda: SimpleNamespace(
                    data={"placeId": "p1", "link": "invite-parent/night", "displayName": "N"}
                )
            )
        )
        with patch.object(circle_invites, "service_client", return_value=sb):
            self.assertEqual(invite_link("tok"), "invite-parent/night")

    def test_null_and_errors(self) -> None:
        sb = SimpleNamespace(
            rpc=lambda name, args: SimpleNamespace(execute=lambda: SimpleNamespace(data=None))
        )
        with patch.object(circle_invites, "service_client", return_value=sb):
            self.assertIsNone(invite_link("tok"))
        with patch.object(circle_invites, "service_client", side_effect=RuntimeError("x")):
            self.assertIsNone(invite_link("tok"))


class TestJoinEndpoint(unittest.TestCase):
    def _post(self, body, result):
        from fastapi.testclient import TestClient

        from app import main

        with (
            patch.object(main, "verify_auth", return_value=SimpleNamespace(user_id="u2")),
            patch("app.community_discovery.join_community", return_value=result) as join,
            patch("app.circle_invites.attribute_join", return_value={"invite_id": "inv1"}) as attr,
            patch.object(main, "amplitude_track"),
        ):
            res = TestClient(main.app).post(
                "/lana/circles/join", json=body, headers={"Authorization": "Bearer x"}
            )
        return res, join, attr

    _JOINED = {
        "affiliation_id": "aff1",
        "place_id": "p1",
        "place_name": "Club",
        "status": "confirmed",
        "already_member": False,
        "source": "community_join",
        "confirmed_via": "community_join",
    }

    def test_token_passed_through_on_a_real_join(self) -> None:
        res, _join, attr = self._post(
            {"place_id": "p1", "invite_token": "tok"}, dict(self._JOINED)
        )
        self.assertEqual(res.status_code, 200)
        attr.assert_called_once_with("u2", "tok", "p1", affiliation_id="aff1")
        self.assertTrue(res.json()["attributed"])

    def test_no_token_no_attribution(self) -> None:
        res, _join, attr = self._post({"place_id": "p1"}, dict(self._JOINED))
        self.assertEqual(res.status_code, 200)
        attr.assert_not_called()
        self.assertFalse(res.json()["attributed"])

    def test_already_member_not_attributed(self) -> None:
        res, _join, attr = self._post(
            {"place_id": "p1", "invite_token": "tok"},
            {**self._JOINED, "already_member": True},
        )
        self.assertEqual(res.status_code, 200)
        attr.assert_not_called()
        self.assertTrue(res.json()["already_member"])
        self.assertFalse(res.json()["attributed"])


if __name__ == "__main__":
    unittest.main()
