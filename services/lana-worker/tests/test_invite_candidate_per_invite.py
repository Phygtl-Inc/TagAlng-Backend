"""Invite self-confirm: one candidate per INVITE (§28(c)), membership before a place (§23).

The bug: add_circle keyed the invite candidate on the kind alone, so a second owner's
fitness invite got back the gym the joiner had already pinned for the first — and the
ack said grounded:false for a grounded row. These tests run the real add_circle /
self_confirm / ground_affiliation code against a small in-memory circle_affiliations
table, so the dedupe, the key and the ack are read off rows, not mocks of them.
"""

import re
import unittest
import uuid
from typing import Any
from unittest.mock import patch

from app import circle_invites, circles_flow

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
USER = "u-joiner"


class _Q:
    def __init__(self, table: "_Table") -> None:
        self.t = table
        self.filters: list[tuple[str, Any]] = []
        self.op = "select"
        self.payload: Any = None
        self.cols: list[str] | None = None

    def select(self, cols: str, **_kw):
        self.cols = [c.strip() for c in cols.split(",")]
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def is_(self, col, _null):
        self.filters.append((col, None))
        return self

    def limit(self, _n):
        return self

    def order(self, *_a, **_k):
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, patch_):
        self.op, self.payload = "update", patch_
        return self

    def _match(self, r):
        return all(r.get(c) == v for c, v in self.filters)

    def execute(self):
        t = self.t
        if self.cols and t.legacy and any(
            c in ("invite_id", "membership_intent") for c in self.cols
        ):
            raise RuntimeError("{'code': '42703', 'message': 'column invite_id does not exist'}")
        if self.op == "insert":
            row = dict(self.payload)
            if t.legacy and ("invite_id" in row or "membership_intent" in row):
                raise RuntimeError("42703")
            assert KEY_RE.match(row["circle_key"]), row["circle_key"]
            for r in t.rows:
                if r["dismissed_at"] is None and r["user_id"] == row["user_id"]:
                    assert r["circle_key"] != row["circle_key"], "user_key unique index"
                    if row.get("invite_id"):
                        assert r.get("invite_id") != row["invite_id"], "user_invite unique"
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("place_ref", None)
            row.setdefault("dismissed_at", None)
            row.setdefault("membership_intent", None)
            t.rows.append(row)
            return type("R", (), {"data": [dict(row)]})()
        if self.op == "update":
            for r in t.rows:
                if self._match(r):
                    r.update(self.payload)
            return type("R", (), {"data": []})()
        data = [dict(r) for r in t.rows if self._match(r)]
        return type("R", (), {"data": data})()


class _Table:
    def __init__(self, legacy: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self.legacy = legacy


class _SB:
    def __init__(self, table: _Table) -> None:
        self.t = table

    def table(self, name):
        assert name == "circle_affiliations", name
        return _Q(self.t)


def _invite(inv_id: str, owner: str) -> dict:
    return {
        "id": inv_id,
        "owner_user_id": owner,
        "circle_type": "fitness",
        "circle_key": None,
        "place_ref": None,
        "revoked_at": None,
    }


class InviteCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = _Table()
        self._p = [
            patch("app.circles_flow.service_client", return_value=_SB(self.table)),
        ]
        for p in self._p:
            p.start()

    def tearDown(self) -> None:
        for p in self._p:
            p.stop()

    def _confirm(self, inv_id: str, owner: str, membership: str | None = None) -> dict:
        with patch.object(circle_invites, "_active_invite", return_value=_invite(inv_id, owner)):
            return circle_invites.self_confirm(
                USER, "tok-" + inv_id, circle_type="fitness", membership=membership
            )

    def _ground(self, aff_id: str, place_id: str) -> dict:
        with patch(
            "app.places.place_details", return_value={"name": "YouFit " + place_id}
        ), patch.object(circles_flow, "upsert_canonical_place", return_value=place_id), patch(
            "app.community_discovery.notify_members_of_join"
        ) as notify, patch.object(circles_flow, "_flush_parked_features"), patch.object(
            circles_flow, "_close_grounding_gap"
        ), patch(
            "app.rapport_gaps.open_semantic_gap"
        ) as gap:
            out = circles_flow.ground_affiliation(USER, aff_id, "g-" + place_id)
        out["_notified"] = notify.called
        out["_gap"] = gap.called
        return out

    def _row(self, aff_id: str) -> dict:
        return next(r for r in self.table.rows if r["id"] == aff_id)

    # §28(c) ─────────────────────────────────────────────────────────────────

    def test_two_owners_same_kind_make_two_rows(self) -> None:
        a = self._confirm("inv-a", "owner-a")
        self._ground(a["affiliation_id"], "p-colonial")
        b = self._confirm("inv-b", "owner-b")
        self.assertNotEqual(a["affiliation_id"], b["affiliation_id"])
        self.assertEqual(len(self.table.rows), 2)
        keys = sorted(r["circle_key"] for r in self.table.rows)
        self.assertEqual(keys, ["fitness", "fitness_2"])
        self.assertFalse(b["grounded"])
        self.assertIsNone(b["place_id"])
        # The first community is untouched by the second accept.
        self.assertEqual(self._row(a["affiliation_id"])["place_ref"], "p-colonial")

    def test_same_invite_again_returns_the_real_row_state(self) -> None:
        a = self._confirm("inv-a", "owner-a")
        self.assertEqual(
            a,
            {
                "affiliation_id": a["affiliation_id"],
                "status": "suggested",
                "grounded": False,
                "place_id": None,
                "membership": None,
            },
        )
        self._ground(a["affiliation_id"], "p-colonial")
        again = self._confirm("inv-a", "owner-a")
        self.assertEqual(again["affiliation_id"], a["affiliation_id"])
        self.assertTrue(again["grounded"])
        self.assertEqual(again["place_id"], "p-colonial")
        self.assertEqual(again["status"], "confirmed")
        self.assertEqual(len(self.table.rows), 1)

    def test_pre_migration_schema_falls_back_but_never_lies_about_grounding(self) -> None:
        self.table.legacy = True
        self.table.rows.append(
            {
                "id": "old",
                "user_id": USER,
                "circle_type": "fitness",
                "circle_key": "fitness",
                "status": "confirmed",
                "place_ref": "p-colonial",
                "dismissed_at": None,
                "source": "invite_confirmed",
            }
        )
        out = self._confirm("inv-b", "owner-b")
        self.assertEqual(out["affiliation_id"], "old")
        self.assertTrue(out["grounded"])
        self.assertEqual(out["place_id"], "p-colonial")

    # §23 ────────────────────────────────────────────────────────────────────

    def test_curious_before_the_pin_sticks_through_grounding(self) -> None:
        a = self._confirm("inv-a", "owner-a", membership="curious")
        self.assertEqual(a["membership"], "curious")
        self.assertEqual(self._row(a["affiliation_id"])["status"], "suggested")
        g = self._ground(a["affiliation_id"], "p-colonial")
        self.assertEqual(g["status"], "curious")
        self.assertEqual(self._row(a["affiliation_id"])["status"], "curious")
        # She said she does not go there: nobody is mailed, no "what do you enjoy?".
        self.assertFalse(g["_notified"])
        self.assertFalse(g["_gap"])

    def test_no_answer_grounds_as_a_member_as_before(self) -> None:
        a = self._confirm("inv-a", "owner-a")
        g = self._ground(a["affiliation_id"], "p-colonial")
        self.assertEqual(g["status"], "confirmed")
        self.assertTrue(g["_notified"])

    def test_answer_can_change_before_and_after_the_pin(self) -> None:
        a = self._confirm("inv-a", "owner-a", membership="curious")
        self._confirm("inv-a", "owner-a", membership="member")
        self.assertEqual(self._row(a["affiliation_id"])["membership_intent"], "member")
        self._ground(a["affiliation_id"], "p-colonial")
        self.assertEqual(self._row(a["affiliation_id"])["status"], "confirmed")
        late = self._confirm("inv-a", "owner-a", membership="curious")
        self.assertEqual(late["membership"], "curious")
        self.assertEqual(self._row(a["affiliation_id"])["status"], "curious")


class SelfConfirmRouteTests(unittest.TestCase):
    def test_route_passes_membership_and_rejects_other_values(self) -> None:
        from fastapi.testclient import TestClient

        from app import main

        client = TestClient(main.app)
        with patch.object(main, "verify_auth", return_value=type("A", (), {"user_id": USER})()), patch(
            "app.circle_invites.self_confirm", return_value={"affiliation_id": "a1"}
        ) as sc:
            ok = client.post(
                "/lana/invites/self-confirm",
                json={"token": "t", "circle_type": "Fitness", "membership": "curious"},
            )
            bad = client.post(
                "/lana/invites/self-confirm",
                json={"token": "t", "circle_type": "fitness", "membership": "maybe"},
            )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(sc.call_args.kwargs["membership"], "curious")
        self.assertEqual(sc.call_args.kwargs["circle_type"], "fitness")
        self.assertEqual(bad.status_code, 422)


if __name__ == "__main__":
    unittest.main()
