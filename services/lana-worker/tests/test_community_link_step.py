"""Choosing the community's link is the last step of creating it (Asjid, 2026-10-06).

Order once every question is in: where a community with no place is run from → its link
(prefilled, required) → the ready card showing both → Share publishes AND claims the link.
A guest verifies their email before any of it starts.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.community_capture as cc

_NAME = "Rosetta's Bakery"


def _patches(monkeypatch: Any, *, checks: list[dict], geo: Any = None) -> dict[str, mock.Mock]:
    monkeypatch.setattr(cc, "compose_reply", lambda *, goal, facts, fallback, **k: fallback)
    check = mock.Mock(side_effect=list(checks))
    monkeypatch.setattr(cc, "_link_check", check)
    monkeypatch.setattr("app.community_hq.geocode_city", lambda text: geo)
    publish = mock.Mock(return_value=({"place_id": "pNew"}, ""))
    monkeypatch.setattr(cc, "publish_community", publish)
    claim = mock.Mock(return_value="rosettasbakery")
    monkeypatch.setattr(cc, "_claim_link", claim)
    offer = mock.Mock(return_value=None)
    monkeypatch.setattr(cc, "_handle_offer", offer)
    write = mock.Mock(return_value={"status": "saved"})
    monkeypatch.setattr("app.community_hq.write_community_hq", write)
    return {"check": check, "publish": publish, "claim": claim, "offer": offer, "write": write}


def _turn(msg: str, ctx: dict) -> str:
    return cc.run_community_capture_turn(
        user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt",
        user_id="u1", home_block_id=None,
    )


def _draft(**extra: Any) -> dict:
    return {"name": _NAME, "circle_type": "hobby", "draft_id": "d1", **extra}


def test_placeless_asks_city_then_link_then_ready_then_publishes_with_the_link(
    monkeypatch: Any,
) -> None:
    m = _patches(monkeypatch, geo={"city": "Orlando, FL", "lat": 28.5, "lng": -81.4}, checks=[
        {"status": "invalid", "reason": "empty",
         "suggestions": ["rosettasbakery", "rosettas-bakery"]},       # prefill
        {"status": "available", "normalizedHandle": "rosettasbakery"},  # their pick
    ])
    ctx: dict[str, Any] = {"community_create_active": True, "community_draft": _draft()}

    # Every question in → the city first (a community with no place).
    cc._after_questions(draft=ctx["community_draft"], session_ctx=ctx, user_id="u1")
    assert ctx["community_pending_ask"] == "hq" and not ctx.get("community_ready")

    # The city → straight on to the link, prefilled from the name.
    _turn("Orlando", ctx)
    d = ctx["community_draft"]
    assert d["hq_city"] == "Orlando, FL"
    assert ctx["community_pending_ask"] == "handle" and d["pending_field"] == "handle"
    assert d["handle_suggestion"] == "rosettasbakery"
    assert not ctx.get("community_ready")

    # The link → the ready card, which carries it. Nothing is created yet.
    _turn("rosettasbakery", ctx)
    d = ctx["community_draft"]
    assert d["handle"] == "rosettasbakery" and d["ready"] is True
    assert ctx["community_ready"] is True and ctx["community_pending_ask"] is None
    m["publish"].assert_not_called()

    # Share → published, the link claimed, the HQ written; no "claim" button needed.
    reply = _turn("Share with the community", ctx)
    m["publish"].assert_called_once()
    m["claim"].assert_called_once_with("u1", "pNew", "rosettasbakery")
    m["write"].assert_called_once()
    m["offer"].assert_not_called()
    assert ctx["community_draft"]["handle"] == "rosettasbakery"
    assert ctx["community_draft"]["published"] is True
    assert "is up" in reply


def test_the_link_cannot_be_skipped(monkeypatch: Any) -> None:
    """Required: anything that is not an available link re-asks; it never reaches ready."""
    _patches(monkeypatch, checks=[
        {"status": "unavailable", "reason": "taken", "normalizedHandle": "skip",
         "suggestions": ["rosettasbakery2"]},
    ])
    ctx: dict[str, Any] = {"community_create_active": True, "community_pending_ask": "handle",
                           "community_draft": _draft(hq_city="Orlando, FL", pending_field="handle")}
    reply = _turn("skip", ctx)
    assert "isn't available" in reply
    assert ctx["community_pending_ask"] == "handle" and not ctx.get("community_ready")
    assert ctx["community_draft"]["suggestions"] == ["rosettasbakery2"]
    assert ctx["community_draft"]["handle_error"] == "taken"


def test_a_place_that_already_has_a_link_skips_the_step_and_shows_it(monkeypatch: Any) -> None:
    m = _patches(monkeypatch, checks=[
        {"status": "already_has_handle", "handle": "rosettas-bakery-orl"},
    ])
    ctx: dict[str, Any] = {"community_create_active": True,
                           "community_draft": _draft(hq_city="Orlando, FL")}
    cc._after_questions(draft=ctx["community_draft"], session_ctx=ctx, user_id="u1")
    assert ctx["community_ready"] is True
    assert ctx["community_draft"]["handle"] == "rosettas-bakery-orl"
    # Publishing joins it; the existing link is never claimed over.
    _turn("Share with the community", ctx)
    m["claim"].assert_not_called()
    assert ctx["community_draft"]["handle"] == "rosettas-bakery-orl"


def test_someone_elses_place_gets_no_link_step(monkeypatch: Any) -> None:
    _patches(monkeypatch, checks=[{"status": "not_eligible", "reason": "not_eligible"}])
    ctx: dict[str, Any] = {"community_create_active": True,
                           "community_draft": _draft(google_place_id="gp-cafe")}
    cc._after_questions(draft=ctx["community_draft"], session_ctx=ctx, user_id="u1")
    assert ctx["community_ready"] is True and not ctx["community_draft"].get("handle")


def test_a_lost_race_falls_back_to_the_claim_button(monkeypatch: Any) -> None:
    m = _patches(monkeypatch, checks=[])
    m["claim"].return_value = None
    m["offer"].return_value = {"place_id": "pNew", "suggestion": "rosettasbakery3"}
    ctx: dict[str, Any] = {"community_create_active": True, "community_ready": True,
                           "community_draft": _draft(hq_city="Orlando, FL",
                                                     handle="rosettasbakery", ready=True)}
    _turn("Share with the community", ctx)
    assert ctx["community_draft"]["handle"] is None
    assert ctx["community_draft"]["handle_offer"]["suggestion"] == "rosettasbakery3"


def test_claim_link_takes_the_next_suggestion_on_a_race(monkeypatch: Any) -> None:
    calls: list = []

    class _Rpc:
        def rpc(self, fn: str, params: dict) -> "_Rpc":
            calls.append(params["p_handle"])
            self.handle = params["p_handle"]
            return self

        def execute(self) -> Any:
            if self.handle == "taken":
                return mock.Mock(data={"status": "unavailable", "suggestions": ["taken2"]})
            return mock.Mock(data={"status": "claimed", "handle": self.handle})

    monkeypatch.setattr("app.auth.service_client", lambda: _Rpc())
    assert cc._claim_link("u1", "p1", "taken") == "taken2"
    assert calls == ["taken", "taken2"]


def test_a_guest_is_asked_to_verify_before_creating(monkeypatch: Any) -> None:
    from app import lana_unified_pipeline as up

    monkeypatch.setattr("app.reply_compose.compose_reply",
                        lambda *, goal, facts, fallback, **k: fallback)
    timer = mock.Mock(to_dict=lambda: {})
    ctx: dict[str, Any] = {}
    out = up._community_create_verify_gate(ctx, phone_verified=False,
                                           user_message="create a community", timer=timer)
    assert out is not None
    reply, _status, ctx_out, _ui, _ev = out
    assert "verify your email" in reply.lower()
    assert ctx_out["requires_phone_verification"] is True
    assert ctx_out["routing_phase"] == "await_signup_phone"
    assert not ctx_out.get("community_create_active")


def test_verified_users_and_ongoing_captures_pass_the_gate() -> None:
    from app import lana_unified_pipeline as up

    timer = mock.Mock(to_dict=lambda: {})
    assert up._community_create_verify_gate({}, phone_verified=True,
                                            user_message="x", timer=timer) is None
    # Mid-capture (already started before this change shipped): never yanked out.
    assert up._community_create_verify_gate({"community_create_active": True},
                                            phone_verified=False, user_message="x",
                                            timer=timer) is None


def test_the_pipeline_runs_the_gate_before_starting_a_capture() -> None:
    from app import lana_unified_pipeline as up

    src = open(up.__file__).read()
    gate = src.index("gated = _community_create_verify_gate(")
    start = src.index('session_ctx["community_create_active"] = True', gate)
    assert gate < start
