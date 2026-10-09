"""Chapters you can make, unmake, and see content across (20270125120000).

2026-10-07: RCC is a chapter of SJSU only because it was attached by hand in SQL; "create
RCC as a club inside SJSU" made a standalone community; and a parent's members never saw
a chapter's meets. The SQL holds the rules (covered in the migration's container run);
these pin what the worker does with them.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_chapter_ops as ops
import app.community_capture as cc
import app.community_discovery as cd
from app.discovery_slots import slots_chapter_change

SJSU = "3f2a0c4e-1111-4222-8333-444455556666"
RCC = "3f2a0c4e-1111-4222-8333-777788889999"
DATA = "3f2a0c4e-1111-4222-8333-aaaabbbbcccc"


class _Rpc:
    def __init__(self, data: Any = None, exc: Exception | None = None) -> None:
        self.data, self.exc, self.calls = data, exc, []

    def rpc(self, fn: str, params: dict) -> "_Rpc":
        self.calls.append((fn, params))
        return self

    def execute(self) -> Any:
        if self.exc:
            raise self.exc
        return mock.Mock(data=self.data)


# ── the SQL calls ─────────────────────────────────────────────────────────────────


def test_attach_returns_the_refusal_as_a_reason(monkeypatch: Any) -> None:
    monkeypatch.setattr(ops, "service_client",
                        lambda: _Rpc(exc=RuntimeError("{'message': 'not_a_member_of_parent'}")))
    assert ops.attach_chapter("u", RCC, SJSU) == {"ok": False, "reason": "not_a_member_of_parent"}
    monkeypatch.setattr(ops, "service_client", lambda: _Rpc(exc=RuntimeError("boom")))
    assert ops.attach_chapter("u", RCC, SJSU)["reason"] == "failed"


def test_attach_ok(monkeypatch: Any) -> None:
    client = _Rpc({"parentName": "San Jose State University", "inheritedLocation": True,
                   "alreadyAttached": False})
    monkeypatch.setattr(ops, "service_client", lambda: client)
    got = ops.attach_chapter("u", RCC, SJSU)
    assert got["ok"] and got["parent_name"] == "San Jose State University"
    assert got["inherited_location"] is True
    assert client.calls[0] == ("attach_chapter", {"p_user_id": "u", "p_chapter": RCC, "p_parent": SJSU})


def test_family_never_widens_on_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(ops, "service_client", lambda: _Rpc(exc=RuntimeError("db")))
    assert [f["place_id"] for f in ops.community_family("u", SJSU)] == [SJSU]
    # A family read that somehow lacks the community itself is not trusted either.
    monkeypatch.setattr(ops, "service_client",
                        lambda: _Rpc([{"place_id": RCC, "place_name": "RCC", "relation": "chapter"}]))
    assert [f["place_id"] for f in ops.community_family("u", SJSU)] == [SJSU]
    # No viewer: the community alone.
    assert [f["place_id"] for f in ops.community_family(None, SJSU)] == [SJSU]


def test_label_origin_marks_only_meets_from_another_community() -> None:
    family = [{"place_id": SJSU, "place_name": "SJSU", "relation": "self"},
              {"place_id": RCC, "place_name": "RCC", "relation": "chapter"}]
    rows = [{"id": "own", "circle_place_ref": SJSU},
            {"id": "at_own_place", "place_ref": SJSU, "circle_place_ref": RCC},
            {"id": "chapter", "circle_place_ref": RCC}]
    ops.label_origin(rows, SJSU, family)
    assert "origin_place_name" not in rows[0]
    assert "origin_place_name" not in rows[1]  # held AT SJSU: it is SJSU's meet too
    assert rows[2]["origin_place_name"] == "RCC" and rows[2]["origin_place_id"] == RCC


# ── meets roll up ─────────────────────────────────────────────────────────────────


class _Chain:
    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.or_arg = rows, None

    def __getattr__(self, name: str) -> Any:
        def step(*a: Any, **k: Any) -> "_Chain":
            if name == "or_":
                self.or_arg = a[0]
            return self
        return step

    def execute(self) -> Any:
        return mock.Mock(data=[dict(r) for r in self.rows])


def test_community_events_reads_the_family_and_labels_it(monkeypatch: Any) -> None:
    import app.community_scope as scope

    chain = _Chain([{"id": "e1", "title": "AI ethics night", "circle_place_ref": RCC, "host_id": "h"}])
    monkeypatch.setattr("app.auth.service_client", lambda: chain)
    monkeypatch.setattr("app.event_publish.roll_recurring_events", lambda: None)
    monkeypatch.setattr(ops, "community_family", lambda viewer, pid: [
        {"place_id": SJSU, "place_name": "SJSU", "relation": "self"},
        {"place_id": RCC, "place_name": "RCC", "relation": "chapter"},
    ])
    rows = scope.community_events(SJSU, viewer_id="u1")
    assert chain.or_arg == f"circle_place_ref.in.({SJSU},{RCC}),place_ref.in.({SJSU},{RCC})"
    assert rows[0]["origin_place_name"] == "RCC"

    chain2 = _Chain([])
    monkeypatch.setattr("app.auth.service_client", lambda: chain2)
    scope.community_events(SJSU)  # no viewer: the community alone, filter unchanged
    assert chain2.or_arg == f"circle_place_ref.eq.{SJSU},place_ref.eq.{SJSU}"


def test_profile_meets_carry_their_origin(monkeypatch: Any) -> None:
    import app.community_surface as cs

    seen: dict = {}

    def events_at(pid: str, *, limit: int, family_ids: list | None = None) -> list[dict]:
        seen["family_ids"] = family_ids
        return [{"id": "e1", "title": "AI ethics night", "circle_place_ref": RCC}]

    monkeypatch.setattr(cs, "_events_at_place", events_at)
    monkeypatch.setattr(cs, "_going_counts", lambda ids: {})
    monkeypatch.setattr(ops, "community_family", lambda viewer, pid: [
        {"place_id": SJSU, "place_name": "SJSU", "relation": "self"},
        {"place_id": RCC, "place_name": "RCC", "relation": "chapter"},
    ])
    monkeypatch.setattr(cs, "_event_fit_scores", lambda *a, **k: {}, raising=False)
    rows, _ = cs._event_rows_for_profile(SJSU, viewer_id="u1")
    assert seen["family_ids"] == [SJSU, RCC]
    assert rows[0]["origin_place_name"] == "RCC" and rows[0]["origin_place_id"] == RCC


# ── creating one inside another ─────────────────────────────────────────────────


def test_parent_resolves_once_and_records_its_point(monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cd, "find_named_community",
                        lambda uid, said: calls.append(said) or {"place_id": SJSU, "place_name": "San Jose State University"})

    class _T:
        def __getattr__(self, n: str) -> Any:
            return lambda *a, **k: self

        def execute(self) -> Any:
            return mock.Mock(data=[{"lat": 37.3, "lng": -121.8}])

    monkeypatch.setattr("app.auth.service_client", lambda: _T())
    draft = {"parent": "SJSU", "name": "RCC"}
    cc._resolve_parent(draft, "u1")
    cc._resolve_parent(draft, "u1")
    assert calls == ["SJSU"]
    assert draft["parent_place"] == {
        "place_id": SJSU, "place_name": "San Jose State University", "located": True, "handle": None,
    }
    assert {"label": "Part of San Jose State University", "tone": "sky", "field": "parent"} in draft["chips"]


def test_unknown_parent_is_said_and_the_community_stands_alone(monkeypatch: Any) -> None:
    monkeypatch.setattr(cd, "find_named_community", lambda uid, said: None)
    draft = {"parent": "Hogwarts", "name": "RCC"}
    cc._resolve_parent(draft, "u1")
    assert draft["parent_unresolved"] is True
    facts = cc._attach_to_parent(draft, "u1", RCC)
    assert "no community by that name" in facts[0] and "stands on its own" in facts[0]


def test_attach_after_publish_reports_each_outcome(monkeypatch: Any) -> None:
    draft = {"parent": "SJSU", "parent_place": {"place_id": SJSU, "place_name": "SJSU", "located": True}}
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: {"ok": True})
    assert "now a club inside SJSU" in cc._attach_to_parent(dict(draft), "u1", RCC)[0]
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: {"ok": False, "reason": "not_a_member_of_parent"})
    fact = cc._attach_to_parent(dict(draft), "u1", RCC)[0]
    assert "not a member of SJSU" in fact and "live on its own" in fact
    assert cc._attach_to_parent({"name": "x"}, "u1", RCC) == []


def test_the_ready_card_shows_the_parent_as_a_correctable_chip() -> None:
    draft = {"parent": "SJSU", "parent_place": {"place_id": SJSU}, "parent_unresolved": False}
    chips = cc._build_chips(dict(draft))
    assert any(c["field"] == "parent" for c in chips)


def test_publish_writes_the_creators_words_and_embeds(monkeypatch: Any) -> None:
    writes: list = []

    class _T:
        def table(self, n: str) -> "_T":
            return self

        def update(self, d: dict) -> "_T":
            writes.append(("update", d))
            return self

        def eq(self, *a: Any) -> "_T":
            return self

        def is_(self, col: str, val: Any) -> "_T":
            writes.append(("is", col, val))
            return self

        def execute(self) -> Any:
            return mock.Mock(data=[])

    monkeypatch.setattr("app.circles_flow.add_circle", lambda *a, **k: {"place_id": RCC})
    monkeypatch.setattr("app.circles_capture.upsert_place_feature", lambda **k: None)
    monkeypatch.setattr("app.auth.service_client", lambda: _T())
    embedded: list = []
    monkeypatch.setattr("app.community_embeddings.embed_place_later", lambda pid: embedded.append(pid))
    draft = {"circle_type": "school", "google_place_id": "g1", "name": "RCC",
             "blurb": "AI ethics and responsible computing"}
    result, err = cc.publish_community(draft=draft, user_id="u1")
    assert result["place_id"] == RCC and not err
    assert ("update", {"blurb": "AI ethics and responsible computing"}) in writes
    # Only when it has none: an existing community's description is never overwritten.
    assert ("is", "blurb", "null") in writes
    assert embedded == [RCC]


# ── changing it in chat ────────────────────────────────────────────────────────


def _change(monkeypatch: Any, action: str, parent: str | None, **ops_kw: Any) -> tuple[str, dict, dict]:
    seen: dict = {}

    def compose(*, goal: str, facts: list | None = None, fallback: str, **_: Any) -> str:
        seen.update(goal=goal, facts=facts or [])
        return fallback

    monkeypatch.setattr("app.reply_compose.compose_reply", compose)
    for k, v in ops_kw.items():
        monkeypatch.setattr(ops, k, v)
    ctx: dict = {}
    out = cd._chapter_change_turn(
        "u1", community={"place_id": RCC, "place_name": "RCC"}, action=action,
        parent_said=parent, message="put RCC under SJSU", session_ctx=ctx,
    )
    return out, ctx, seen


def test_put_rcc_under_sjsu(monkeypatch: Any) -> None:
    monkeypatch.setattr(cd, "find_named_community", lambda u, s: {"place_id": SJSU, "place_name": "San Jose State University"})
    got: list = []
    out, _, _ = _change(monkeypatch, "attach", "SJSU",
                        attach_chapter=lambda u, c, p: got.append((c, p)) or {"ok": True})
    assert got == [(RCC, SJSU)]
    assert "RCC is now a club inside San Jose State University" in out


def test_not_a_member_offers_to_join_the_parent_first(monkeypatch: Any) -> None:
    monkeypatch.setattr(cd, "find_named_community", lambda u, s: {"place_id": SJSU, "place_name": "SJSU"})
    _, ctx, seen = _change(monkeypatch, "attach", "SJSU",
                           attach_chapter=lambda u, c, p: {"ok": False, "reason": "not_a_member_of_parent"})
    assert ctx["community_join_pending"]["places"][0]["place_id"] == SJSU
    assert "not a member of SJSU" in seen["facts"][0]


def test_unknown_parent_name_changes_nothing(monkeypatch: Any) -> None:
    monkeypatch.setattr(cd, "find_named_community", lambda u, s: None)
    attach = mock.Mock()
    out, _, _ = _change(monkeypatch, "attach", "Hogwarts", attach_chapter=attach)
    attach.assert_not_called()
    assert "no community called" in out


def test_detach(monkeypatch: Any) -> None:
    out, _, _ = _change(monkeypatch, "detach", None, detach_chapter=lambda u, c: {
        "ok": True, "was_attached": True, "parent_name": "SJSU"})
    assert "no longer part of SJSU" in out
    out, _, _ = _change(monkeypatch, "detach", None, detach_chapter=lambda u, c: {
        "ok": False, "reason": "not_your_community"})
    assert "Only whoever runs RCC" in out


def test_attach_without_a_parent_asks_which(monkeypatch: Any) -> None:
    out, _, _ = _change(monkeypatch, "attach", None)
    assert out == "Which community should RCC be part of?"


def test_slots_chapter_change() -> None:
    assert slots_chapter_change({"community_ask": "manage", "chapter_action": "attach",
                                 "community_parent": "SJSU"}) == ("attach", "SJSU")
    assert slots_chapter_change({"community_ask": "manage", "chapter_action": "detach"}) == ("detach", None)
    # Never outside a manage ask, and never an unknown action.
    assert slots_chapter_change({"community_ask": "about", "chapter_action": "attach"}) == (None, None)
    assert slots_chapter_change({"community_ask": "manage", "chapter_action": "delete"}) == (None, None)


def test_a_club_inside_a_located_parent_is_not_asked_for_a_city(monkeypatch: Any) -> None:
    """It meets at its parent's spot (attach_chapter copies the point), so "which city is it
    run from?" would be a question with no point — it publishes and attaches straight away."""
    monkeypatch.setattr(cc, "compose_reply", lambda *, goal, facts, fallback, **k: fallback)
    monkeypatch.setattr(cc, "_handle_offer", lambda *a, **k: None)
    publish = mock.Mock(return_value=({"place_id": RCC}, ""))
    monkeypatch.setattr(cc, "publish_community", publish)
    attached: list = []
    monkeypatch.setattr(ops, "attach_chapter", lambda u, c, p: attached.append((c, p)) or {"ok": True})

    def run(parent_place: dict) -> dict:
        ctx: dict[str, Any] = {"community_ready": True, "community_draft": {
            "name": "RCC", "circle_type": "school", "parent": "SJSU", "parent_place": parent_place}}
        cc.run_community_capture_turn(user_message="publish", session_ctx=ctx, history=[],
                                      user_jwt="jwt", user_id="u1", home_block_id=None)
        return ctx

    ctx = run({"place_id": SJSU, "place_name": "SJSU", "located": True})
    publish.assert_called_once()
    assert attached == [(RCC, SJSU)]
    assert ctx["community_draft"]["parent_attached"] is True

    # A parent with no spot either: the city is still asked, nothing is published yet.
    publish.reset_mock()
    ctx = run({"place_id": SJSU, "place_name": "Podcasters", "located": False})
    publish.assert_not_called()
    assert ctx["community_pending_ask"] == "hq"
