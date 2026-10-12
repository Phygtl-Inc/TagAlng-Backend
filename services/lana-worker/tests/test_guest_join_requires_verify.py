"""A guest's community join waits for verification.

Guests (anonymous sessions) could join from a creator link or the chat's join offer, so
members were mailed "Someone new joined" about a person with no name or email. The
endpoint now refuses guests (tests/test_invite_join_attribution.py); this covers the chat
path: a guest's accept is held, the verify gate asks for their email, and the membership
is written once they are verified and named.
"""

from __future__ import annotations

from unittest.mock import patch

from app import discovery_route as dr
from app.community_discovery import (
    JOIN_VERIFY_PENDING,
    hold_join_for_verify,
    join_held_after_verify,
    match_join_reply,
)

_PLACE = {"place_id": "pBeast", "place_name": "MrBeast"}


def _offered() -> dict:
    return {"community_join_pending": {"places": [dict(_PLACE)]}}


def _joined(user_id, place_id, **_kw):
    return {
        "affiliation_id": "aff1",
        "place_id": place_id,
        "place_name": "MrBeast",
        "status": "confirmed",
        "already_member": False,
    }


def test_match_join_reply_returns_place_without_joining() -> None:
    ctx = _offered()
    with patch("app.community_discovery.join_community") as join:
        named = match_join_reply("Join MrBeast", ctx)
    assert named == _PLACE
    join.assert_not_called()
    assert ctx["community_join_pending"] is None


def test_guest_accept_is_held_and_gated() -> None:
    ctx = _offered()
    with patch("app.community_discovery.join_community") as join:
        out = dr._try_community_join_reply_turn(
            msg="Join MrBeast", session_ctx=ctx, user_id="guest1", phone_verified=False
        )
    assert out is not None
    reply, turn_ctx, _routing, _peers = out
    join.assert_not_called()
    assert turn_ctx["requires_phone_verification"] is True
    assert turn_ctx["routing_phase"] == dr.PHASE_AWAIT_SIGNUP_PHONE
    # Not "peers": the funnel must end at the join, never an unrequested neighbors list.
    assert turn_ctx["signup_origin"] == "community"
    assert turn_ctx[JOIN_VERIFY_PENDING]["place_id"] == "pBeast"
    assert "MrBeast" in reply


def test_guest_non_join_reply_falls_through() -> None:
    ctx = _offered()
    out = dr._try_community_join_reply_turn(
        msg="what's the weather like", session_ctx=ctx, user_id="guest1", phone_verified=False
    )
    assert out is None
    assert not ctx.get(JOIN_VERIFY_PENDING)


def test_verified_accept_joins_now() -> None:
    ctx = _offered()
    with (
        patch("app.community_discovery.join_community", side_effect=_joined) as join,
        patch("app.community_discovery.join_confirm_reply", return_value="You're in"),
        patch("app.community_surface.member_count", return_value=3),
    ):
        out = dr._try_community_join_reply_turn(
            msg="Join MrBeast", session_ctx=ctx, user_id="u1", phone_verified=True
        )
    assert out is not None
    join.assert_called_once_with("u1", "pBeast")
    assert not ctx.get(JOIN_VERIFY_PENDING)


def test_held_join_completes_and_consumes_the_hold() -> None:
    ctx: dict = {}
    hold_join_for_verify(ctx, _PLACE)
    with patch("app.community_discovery.join_community", side_effect=_joined) as join:
        result = join_held_after_verify("u1", ctx)
    join.assert_called_once_with("u1", "pBeast")
    assert result["place_name"] == "MrBeast"
    # None, not popped — the session merge would resurrect a popped key.
    assert JOIN_VERIFY_PENDING in ctx and ctx[JOIN_VERIFY_PENDING] is None
    with patch("app.community_discovery.join_community") as again:
        assert join_held_after_verify("u1", ctx) is None
    again.assert_not_called()


def test_hold_is_carried_across_a_login() -> None:
    from app.login_carry import collect, describe

    ctx: dict = {}
    hold_join_for_verify(ctx, _PLACE)
    carry = collect(ctx)
    assert carry[JOIN_VERIFY_PENDING]["place_id"] == "pBeast"
    assert describe(carry) == "joining MrBeast"


def _discovery_turn(ctx: dict, *, verified: bool, needs_name: bool, msg: str = "hi"):
    """Run the discovery turn far enough to hit the held-join block; everything after
    it is stubbed out so a miss returns the sentinel."""
    sentinel = ("FELL_THROUGH", {}, {}, [])
    with (
        patch.object(dr, "discovery_ai_enabled", return_value=False),
        patch.object(dr, "user_profile_photo_url", return_value=None),
        patch.object(dr, "resolve_block_id", return_value="b1"),
        patch.object(dr, "_unsafe_kind_for_turn", return_value=None),
        patch.object(dr, "_is_crisis_turn", return_value=False),
        patch.object(dr, "user_needs_display_name", return_value=needs_name),
        patch.object(dr, "_host_via_orchestrator", side_effect=RuntimeError("past the block")),
        patch("app.community_discovery.join_community", side_effect=_joined) as join,
        patch("app.community_discovery.join_confirm_reply", return_value="You're in MrBeast"),
        patch("app.community_surface.member_count", return_value=3),
    ):
        ctx = {**ctx, "event_host_active": True}
        try:
            out = dr.handle_discovery_turn(
                msg,
                session_ctx=ctx,
                user_jwt="jwt",
                phone_verified=verified,
                home_block_id="b1",
                is_anonymous=not verified,
                user_id="u1",
            )
        except RuntimeError:
            out = sentinel
    return out, join


def test_verified_named_turn_writes_the_held_join() -> None:
    ctx: dict = {"routing_phase": "listening"}
    hold_join_for_verify(ctx, _PLACE)
    out, join = _discovery_turn(ctx, verified=True, needs_name=False)
    join.assert_called_once_with("u1", "pBeast")
    assert out[0] == "You're in MrBeast"
    assert out[1][JOIN_VERIFY_PENDING] is None


def test_nameless_verified_turn_waits_for_the_name() -> None:
    ctx: dict = {"routing_phase": "listening"}
    hold_join_for_verify(ctx, _PLACE)
    out, join = _discovery_turn(ctx, verified=True, needs_name=True)
    join.assert_not_called()
    assert out[0] == "FELL_THROUGH"


def test_unverified_turn_never_writes_the_held_join() -> None:
    ctx: dict = {"routing_phase": "listening"}
    hold_join_for_verify(ctx, _PLACE)
    out, join = _discovery_turn(ctx, verified=False, needs_name=False)
    join.assert_not_called()
    assert out[0] == "FELL_THROUGH"


def test_signup_funnel_ends_in_the_held_join_after_the_name() -> None:
    """Fresh signup: OTP verified, Lana asked for a name, they answer — the join is the
    funnel's ending (not the generic welcome), and the name was saved first."""
    ctx: dict = {
        "routing_phase": dr.PHASE_NEED_DISPLAY_NAME,
        "pending_post_verify": True,
        "signup_origin": "community",
        "signup_phone": "sam@example.com",
    }
    hold_join_for_verify(ctx, _PLACE)
    saved: dict = {}
    with (
        patch.object(dr, "discovery_ai_enabled", return_value=False),
        patch.object(dr, "user_profile_photo_url", return_value=None),
        patch.object(dr, "_unsafe_kind_for_turn", return_value=None),
        patch.object(dr, "_is_crisis_turn", return_value=False),
        patch.object(dr, "_try_assign_home_block", return_value=None),
        patch.object(dr, "identity_from_saved_claims", return_value=""),
        patch.object(dr, "user_needs_display_name", side_effect=lambda *_a: not saved),
        patch.object(dr, "persist_profile_patch", side_effect=lambda _u, p: saved.update(p)),
        patch.object(dr, "compose_offscript_reply", return_value="GENERIC WELCOME"),
        patch("app.community_discovery.join_community", side_effect=_joined) as join,
        patch("app.community_discovery.join_confirm_reply", return_value="You're in MrBeast"),
        patch("app.community_surface.member_count", return_value=3),
    ):
        reply, turn_ctx, _r, _p = dr.handle_discovery_turn(
            "Sam",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b1",
            is_anonymous=False,
            user_id="u1",
        )
    assert saved.get("nickname") == "Sam"
    join.assert_called_once_with("u1", "pBeast")
    assert reply == "You're in MrBeast"
    assert turn_ctx["pending_post_verify"] is None
    assert turn_ctx[JOIN_VERIFY_PENDING] is None


def test_chat_turn_passes_guest_status_to_the_join_lane() -> None:
    """Through the real call site: a guest's "Join MrBeast" in chat is gated, not joined."""
    ctx = {**_offered(), "routing_phase": "listening"}
    with (
        patch.object(dr, "discovery_ai_enabled", return_value=True),
        patch.object(dr, "discovery_slots_for_turn", return_value={"goal": "join_community"}),
        patch.object(dr, "enrich_slots", side_effect=lambda s, msg=None: s),
        patch.object(dr, "user_profile_photo_url", return_value=None),
        patch.object(dr, "_unsafe_kind_for_turn", return_value=None),
        patch.object(dr, "_is_crisis_turn", return_value=False),
        patch("app.community_discovery.join_community") as join,
    ):
        reply, turn_ctx, _r, _p = dr.handle_discovery_turn(
            "Join MrBeast",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=False,
            home_block_id="b1",
            is_anonymous=True,
            user_id="guest1",
        )
    join.assert_not_called()
    assert turn_ctx[JOIN_VERIFY_PENDING]["place_id"] == "pBeast"
    assert turn_ctx["requires_phone_verification"] is True


def test_join_mail_names_the_joiner_from_full_name() -> None:
    from app import community_discovery as cd

    rendered: dict = {}

    def _mail(place_id, exclude_user_id, render):
        rendered["subject"] = render("en")[0]
        return 1

    with (
        patch("app.notifications._user_contact", return_value=("sam@example.com", None)),
        patch.object(cd, "_first_name", return_value="Sam"),
        patch.object(cd, "_confirmed_member_count", return_value=4),
        patch("app.notifications.mail_community_members", side_effect=_mail),
    ):
        cd.mail_join_to_members("pBeast", "MrBeast", "u1")
    assert rendered["subject"] == "Sam joined MrBeast"
