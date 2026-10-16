"""A "did you mean" for a CHANGE only offers communities they are in.

Prod 2026-10-09: "create a chapter of Podcasters … at downtown credo coffee" was read as an
attach; the name matched nothing of theirs, and the buttons said "I want to update my
Orlando Public Library community" for places they had never joined.
"""

from __future__ import annotations

from typing import Any

import app.community_discovery as cd

LIB = {"place_id": "p-lib", "place_name": "Orlando Public Library"}
CHURCH = {"place_id": "p-church", "place_name": "Lagoinha Orlando Church"}
GYM = {"place_id": "p-gym", "place_name": "Orlando Fitness"}


def _run(monkeypatch: Any, near: list[dict], mine: list[dict]) -> tuple[str, dict, list]:
    managed: list = []
    monkeypatch.setattr(cd, "resolve_community_name",
                        lambda uid, name: {"hit": None, "inexact": None, "near": near})
    monkeypatch.setattr(cd, "_my_communities", lambda uid: mine)
    monkeypatch.setattr(cd, "_manage_turn",
                        lambda uid, **k: managed.append(k.get("community")) or "manage-reply")
    monkeypatch.setattr("app.reply_compose.compose_reply",
                        lambda **k: k.get("fallback") or "")
    ctx: dict[str, Any] = {}
    reply = cd.communities_chat_turn(
        "u1", message="update podcaster orlando", session_ctx=ctx,
        community_name="podcaster orlando", community_ask="manage",
    )
    return reply, ctx, managed


def test_none_of_theirs_goes_to_their_own_list_not_a_did_you_mean(monkeypatch: Any) -> None:
    reply, ctx, managed = _run(monkeypatch, near=[LIB, CHURCH], mine=[])
    assert managed == [None]
    assert not ctx.get("policy_chips")


def test_only_their_own_near_misses_become_buttons(monkeypatch: Any) -> None:
    _, ctx, managed = _run(monkeypatch, near=[LIB, GYM, CHURCH], mine=[GYM, LIB])
    assert managed == []
    assert [c["label"] for c in ctx["policy_chips"]] == ["Orlando Public Library", "Orlando Fitness"]


def test_other_asks_still_offer_every_near_miss(monkeypatch: Any) -> None:
    monkeypatch.setattr(cd, "resolve_community_name",
                        lambda uid, name: {"hit": None, "inexact": None, "near": [LIB, CHURCH]})
    monkeypatch.setattr(cd, "_my_communities", lambda uid: [])
    monkeypatch.setattr("app.reply_compose.compose_reply", lambda **k: k.get("fallback") or "")
    ctx: dict[str, Any] = {}
    cd.communities_chat_turn("u1", message="who is in orlando lib", session_ctx=ctx,
                             community_name="orlando lib", community_ask="people")
    assert len(ctx["policy_chips"]) == 2
