"""Typing your email after "Verify your email to RSVP" starts verification (prod 2026-10-07).

The browse tail invited a guest to verify, armed nothing, and the email typed next came
back as "I can't verify it from here" with only a mention of "the sign-in flow". The
invitation is now kept on the session and an email answering it arms the same signup
handshake the verify gate uses, which sends the code.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from app.discovery_route import handle_discovery_turn, take_offered_verify_email

_EMAILS = [
    "sam@example.com",
    "my email is ana.lopez+rsvp@correo.mx",
    "es maria@ejemplo.es",
    "  J.Doe@Uni.EDU ",
]


@pytest.mark.parametrize("msg", _EMAILS)
def test_an_email_answering_the_offer_arms_signup(msg: str) -> None:
    ctx: dict[str, Any] = {"verify_offer": "rsvp", "activity_browse_active": True}
    assert take_offered_verify_email(ctx, msg, phone_verified=False)
    assert ctx["requires_phone_verification"] is True
    assert ctx["signup_origin"] == "rsvp"
    assert ctx["activity_browse_active"] is False
    assert ctx["verify_offer"] is None


@pytest.mark.parametrize("ctx,msg,verified", [
    ({}, "sam@example.com", False),                         # nothing offered
    ({"verify_offer": "rsvp"}, "show me yoga instead", False),  # not an email
    ({"verify_offer": "rsvp"}, "sam@example.com", True),     # already verified
])
def test_nothing_is_armed_otherwise(ctx: dict, msg: str, verified: bool) -> None:
    before = dict(ctx)
    assert not take_offered_verify_email(ctx, msg, phone_verified=verified)
    assert ctx == before


def test_the_armed_email_sends_the_signup_code() -> None:
    ctx: dict[str, Any] = {
        "verify_offer": "rsvp",
        "routing_phase": "listening",
        "preview_block_id": "b1",
    }
    assert take_offered_verify_email(ctx, "sam@example.com", phone_verified=False)
    with mock.patch("app.discovery_route.email_has_registered_account", return_value=False), \
         mock.patch("app.discovery_route.discovery_ai_enabled", return_value=False):
        result = handle_discovery_turn(
            "sam@example.com", session_ctx=ctx, user_jwt="jwt",
            phone_verified=False, home_block_id=None, is_anonymous=True,
        )
    assert result is not None
    _, out, _, _ = result
    assert out["routing_phase"] == "await_signup_otp"
    assert out["auth_action"]["type"] == "link_email_signup"
    assert out["auth_action"]["email"] == "sam@example.com"


def test_guest_browse_with_cards_offers_verify_and_verified_does_not() -> None:
    import app.activity_browse as ab

    ev = {"id": "e1", "title": "Python Study Group", "distance_meters": 3000.0}

    def run(verified: bool) -> dict:
        ctx = {"activity_browse_active": True, "browse_draft": {"_asked": True},
               "phone_verified": verified}
        patches = [
            mock.patch.object(ab, "_fetch_block_events", return_value=[dict(ev)]),
            mock.patch.object(ab, "_fetch_admitted_events", return_value=None, create=True),
            mock.patch.object(ab, "_filter_events_by_query", side_effect=lambda r, q: (r, q)),
            mock.patch.object(ab, "_zip_gate_frame", return_value=None),
            mock.patch.object(ab, "_far_offer", return_value=([], "", [])),
            mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False),
            mock.patch("app.orchestrator.llm.llm_configured", return_value=False),
            mock.patch("app.discovery_route._try_assign_home_block"),
        ]
        for p in patches:
            p.start()
        try:
            ab.run_activity_browse_turn(
                user_message="any events?", session_ctx=ctx, history=[], user_jwt="jwt",
                home_block_id="b-home", slots={},
            )
        finally:
            for p in patches:
                p.stop()
        return ctx

    assert run(False).get("verify_offer") == "rsvp"
    assert not run(True).get("verify_offer")


def test_pipeline_hooks_run_before_any_lane_or_the_policy() -> None:
    """The offered-verify email is armed, and a stated town noted, before the browse lane
    and decide_turn can read the turn (same structural check as test_nudge_target)."""
    import inspect

    from app import lana_unified_pipeline as pipe

    src = inspect.getsource(pipe.run_lana_unified_pipeline)
    arm = src.index("take_offered_verify_email(session_ctx, user_message")
    note = src.index("note_chat_area(")
    browse = src.index('if session_ctx.get("activity_browse_active"):')
    policy = src.index("_decide_mode = decide_turn_mode()")
    assert arm < browse and arm < policy
    assert note < browse and note < policy
