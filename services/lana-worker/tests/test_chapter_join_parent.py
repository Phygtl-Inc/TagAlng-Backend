"""Joining a chapter joins its parent: a member of RCC is a member of SJSU.

The parent membership is a real circle_affiliations row (every count, roster and visibility
read works off that table), written BEFORE the chapter's, never downgraded, and skipped for
a paused parent. Leaving a chapter does not leave the parent.

The fake below honours eq / is_ / in_ filters, so a read of the chapter and a read of the
parent return different rows — the shared `_chain` fake returns every row for every query.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

import app.community_discovery as cd

SJSU = "11111111-1111-1111-1111-111111111111"
RCC = "22222222-2222-2222-2222-222222222222"
SOLO = "33333333-3333-3333-3333-333333333333"


class _Query:
    def __init__(self, db: "_DB", table: str) -> None:
        self.db, self.table = db, table
        self.filters: list[tuple[str, str, Any]] = []
        self.op: tuple[str, Any] = ("select", None)

    def select(self, *_: Any, **__: Any) -> "_Query":
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_Query":
        self.filters.append(("is", col, None if val == "null" else val))
        return self

    def in_(self, col: str, vals: list[Any]) -> "_Query":
        self.filters.append(("in", col, list(vals)))
        return self

    def limit(self, *_: Any) -> "_Query":
        return self

    def insert(self, row: dict[str, Any]) -> "_Query":
        self.op = ("insert", row)
        return self

    def update(self, patch: dict[str, Any]) -> "_Query":
        self.op = ("update", patch)
        return self

    def _match(self, row: dict[str, Any]) -> bool:
        for kind, col, val in self.filters:
            have = row.get(col)
            if kind == "eq" and have != val:
                return False
            if kind == "is" and have is not val:
                return False
            if kind == "in" and have not in val:
                return False
        return True

    def execute(self) -> Any:
        rows = self.db.tables[self.table]
        kind, arg = self.op
        if kind == "insert":
            if self.db.fail_insert_for == arg.get("place_ref"):
                raise RuntimeError("insert refused")
            row = {"id": f"a{len(self.db.writes) + 1}", "dismissed_at": None, **arg}
            rows.append(row)
            self.db.writes.append(("insert", row["place_ref"], row["status"]))
            return mock.Mock(data=[row])
        if kind == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                r.update(arg)
                self.db.writes.append(("update", r.get("place_ref"), r.get("status")))
            return mock.Mock(data=hit)
        return mock.Mock(data=[dict(r) for r in rows if self._match(r)])


class _DB:
    def __init__(self, places: list[dict[str, Any]], affs: list[dict[str, Any]] | None = None):
        self.tables = {"places": places, "circle_affiliations": affs or []}
        self.writes: list[tuple[str, Any, Any]] = []
        self.fail_insert_for: str | None = None

    def table(self, name: str) -> _Query:
        return _Query(self, name)


def _places(**sjsu: Any) -> list[dict[str, Any]]:
    return [
        {"id": SJSU, "name": "SJSU", "place_type": "school", "parent_place_ref": None,
         "governance_state": "operator_verified", **sjsu},
        {"id": RCC, "name": "RCC", "place_type": "hobby", "parent_place_ref": SJSU,
         "governance_state": None},
        {"id": SOLO, "name": "Chess Club", "place_type": "hobby", "parent_place_ref": None,
         "governance_state": None},
    ]


def _aff(place: str, status: str, **kw: Any) -> dict[str, Any]:
    return {"id": f"x-{place[:4]}", "user_id": "u1", "place_ref": place, "status": status,
            "circle_key": f"k{place[:4]}", "circle_type": "school", "source": "profile_add",
            "dismissed_at": None, **kw}


@pytest.fixture
def db(monkeypatch: Any) -> _DB:
    store = _DB(_places())
    monkeypatch.setattr(cd, "service_client", lambda: store)
    monkeypatch.setattr(cd, "_after_join", lambda *a, **k: None)
    monkeypatch.setattr(cd, "notify_members_of_join", lambda *a, **k: None)
    monkeypatch.setattr(cd, "_place_noun_emoji", lambda *a, **k: {})
    return store


def _mine(db: _DB) -> dict[str, str]:
    return {r["place_ref"]: r["status"] for r in db.tables["circle_affiliations"]}


def test_joining_a_chapter_joins_its_parent_first(db: _DB) -> None:
    out = cd.join_community("u1", RCC)
    assert _mine(db) == {SJSU: "confirmed", RCC: "confirmed"}
    # Parent written before the chapter: a failure can't leave her in RCC but not SJSU.
    assert [w[1] for w in db.writes] == [SJSU, RCC]
    assert out["place_id"] == RCC
    assert out["parent_joined"] == {"place_id": SJSU, "place_name": "SJSU"}


def test_a_failed_parent_write_fails_the_chapter_join(db: _DB) -> None:
    db.fail_insert_for = SJSU
    with pytest.raises(ValueError, match="join_failed"):
        cd.join_community("u1", RCC)
    assert _mine(db) == {}


def test_already_in_the_parent_is_left_alone(db: _DB) -> None:
    db.tables["circle_affiliations"].append(_aff(SJSU, "confirmed"))
    out = cd.join_community("u1", RCC)
    assert out["parent_joined"] is None
    assert [w[1] for w in db.writes] == [RCC]


def test_a_curious_chapter_join_never_downgrades_a_parent_member(db: _DB) -> None:
    db.tables["circle_affiliations"].append(_aff(SJSU, "confirmed"))
    cd.join_community("u1", RCC, membership="curious")
    assert _mine(db) == {SJSU: "confirmed", RCC: "curious"}


def test_a_curious_chapter_join_is_curious_about_the_parent(db: _DB) -> None:
    out = cd.join_community("u1", RCC, membership="curious")
    assert _mine(db) == {SJSU: "curious", RCC: "curious"}
    assert out["parent_joined"]["place_id"] == SJSU


def test_a_parent_she_only_mentioned_is_promoted(db: _DB) -> None:
    db.tables["circle_affiliations"].append(_aff(SJSU, "suggested"))
    cd.join_community("u1", RCC)
    assert _mine(db) == {SJSU: "confirmed", RCC: "confirmed"}


def test_a_standalone_community_joins_nothing_else(db: _DB) -> None:
    out = cd.join_community("u1", SOLO)
    assert _mine(db) == {SOLO: "confirmed"}
    assert out["parent_joined"] is None


def test_a_paused_parent_is_not_joined(db: _DB, monkeypatch: Any) -> None:
    store = _DB(_places(governance_state="suspended"))
    monkeypatch.setattr(cd, "service_client", lambda: store)
    cd.join_community("u1", RCC)
    assert _mine(store) == {RCC: "confirmed"}


def test_a_chapter_member_from_before_the_rule_heals_on_the_next_tap(db: _DB) -> None:
    db.tables["circle_affiliations"].append(_aff(RCC, "confirmed"))
    out = cd.join_community("u1", RCC)
    assert out["already_member"] is True
    assert out["parent_joined"] == {"place_id": SJSU, "place_name": "SJSU"}
    assert _mine(db) == {SJSU: "confirmed", RCC: "confirmed"}


def test_curious_to_member_on_a_chapter_joins_the_parent(db: _DB) -> None:
    db.tables["circle_affiliations"].append(_aff(RCC, "curious", id="aff-rcc"))
    out = cd.set_membership("u1", "aff-rcc", "member")
    assert _mine(db) == {SJSU: "confirmed", RCC: "confirmed"}
    assert out["parent_joined"]["place_id"] == SJSU


def test_member_to_curious_on_a_chapter_leaves_the_parent_alone(db: _DB) -> None:
    db.tables["circle_affiliations"].extend(
        [_aff(SJSU, "confirmed"), _aff(RCC, "confirmed", id="aff-rcc")]
    )
    cd.set_membership("u1", "aff-rcc", "curious")
    assert _mine(db) == {SJSU: "confirmed", RCC: "curious"}


def test_the_chat_confirmation_names_the_parent(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    def compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        seen.update(goal=goal, facts=facts)
        return fallback

    monkeypatch.setattr("app.reply_compose.compose_reply", compose)
    reply = cd.join_confirm_reply(
        {"place_name": "RCC", "parent_joined": {"place_id": SJSU, "place_name": "SJSU"}},
        session_ctx={}, message="join RCC",
    )
    assert any("member of SJSU too" in f for f in seen["facts"])
    assert "SJSU" not in seen["goal"]  # data in facts, never wording in the goal
    assert "SJSU" in reply
