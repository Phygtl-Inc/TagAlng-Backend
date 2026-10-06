"""A freshly published community offers its creator a short link (in-app handle claim).

The offer is a rendered control, not a chat lane: the worker only asks the database whether
this user may claim a handle for this community (community_handle_offer_for) and, if so,
stamps `handle_offer` on the published draft. The PWA renders it and makes the claim itself.
Eligibility lives in SQL (20270108120000) — the worker never decides it.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_capture as cc
from app.models import CommunityDraft


class _Rpc:
    def __init__(self, data: Any = None, exc: Exception | None = None) -> None:
        self.data, self.exc, self.calls = data, exc, []

    def rpc(self, fn: str, params: dict[str, Any]) -> "_Rpc":
        self.calls.append((fn, params))
        return self

    def execute(self) -> Any:
        if self.exc:
            raise self.exc
        return mock.Mock(data=self.data)


def _publish(monkeypatch: Any, client: _Rpc, user_id: str | None = "user-1") -> tuple[dict, list]:
    facts_seen: list = []

    def _compose(*, goal: str, facts: list, fallback: str, **_: Any) -> str:
        facts_seen.extend(facts)
        return fallback

    monkeypatch.setattr(cc, "compose_reply", _compose)
    monkeypatch.setattr("app.auth.service_client", lambda: client)
    ctx: dict[str, Any] = {
        "community_ready": True,
        "community_draft": {"name": "Austin Run Club", "circle_type": "hobby"},
    }
    with mock.patch.object(cc, "publish_community",
                           return_value=({"place_id": "place-1"}, "")):
        cc.run_community_capture_turn(
            user_message="publish", session_ctx=ctx, history=[], user_jwt="jwt",
            user_id=user_id, home_block_id=None,
        )
    return dict(ctx.get("community_draft") or {}), facts_seen


def test_eligible_creator_gets_an_offer_and_lana_can_mention_it(monkeypatch: Any) -> None:
    client = _Rpc({"eligible": True, "placeId": "place-1", "suggestion": "austin-run-club"})
    draft, facts = _publish(monkeypatch, client)

    assert client.calls == [
        ("community_handle_offer_for", {"p_user_id": "user-1", "p_place_id": "place-1"})
    ]
    assert draft["handle_offer"] == {"place_id": "place-1", "suggestion": "austin-run-club"}
    assert any("get.lana.help/austin-run-club" in f for f in facts), facts
    # The FE model carries it through to the client.
    assert CommunityDraft(**draft).handle_offer.suggestion == "austin-run-club"


def test_ineligible_gets_no_offer(monkeypatch: Any) -> None:
    # A suggestion alone is not permission: the eligible flag is the database's verdict.
    client = _Rpc({"eligible": False, "reason": "not_eligible", "suggestion": "fitness-cf"})
    draft, facts = _publish(monkeypatch, client)
    assert draft.get("handle_offer") is None
    assert not any("get.lana.help" in f for f in facts)


def test_offer_failure_never_costs_the_community(monkeypatch: Any) -> None:
    draft, _ = _publish(monkeypatch, _Rpc(exc=RuntimeError("db down")))
    assert draft.get("published") is True
    assert draft.get("handle_offer") is None


def test_guest_is_not_asked(monkeypatch: Any) -> None:
    client = _Rpc({"eligible": True, "suggestion": "x-y"})
    draft, _ = _publish(monkeypatch, client, user_id=None)
    assert client.calls == []
    assert draft.get("handle_offer") is None
