"""Pouya's RCC QA (2026-10-05): a club meet pinned in Florida, the out-of-scope
clarifier swallowing "what community am I in?", and the club setup walk."""

from __future__ import annotations

import re
from typing import Any
from unittest import mock

import app.community_capture as cc
from app.community_question_sets import COMMUNITY_SUBJECT_FIELD, community_head_step


# ── Fix 2: a placeless community is not a place to meet ──────────────────────────────


def _stamp(body: dict[str, Any], ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    from app import main
    from app.models import EventVenueRequest

    saved: dict[str, Any] = {}
    with mock.patch.object(main, "verify_auth", return_value=mock.Mock(user_id="u1")), mock.patch.object(
        main, "get_session_for_user", return_value={"context": dict(ctx or {})}
    ), mock.patch.object(main, "update_session_context", side_effect=lambda _sid, c: saved.update(c)):
        main.set_event_venue("s1", EventVenueRequest(**body), authorization="Bearer x")
    return saved


def test_a_club_with_no_location_tags_the_meet_but_leaves_where_open() -> None:
    ctx = _stamp({
        "name": "Responsible Computing Club",
        "place_id": "creator:responsible-computing-club",
        "circle_place_id": "pRCC",
    })
    assert ctx["event_draft"] == {"circle_place_id": "pRCC"}
    assert not ctx.get("event_venue")
    assert not ctx.get("event_place_asked")  # the host flow still asks where
    assert ctx["event_venue_seeded"] is True


def test_a_real_place_still_pins_exactly() -> None:
    ctx = _stamp({
        "name": "Lp Fit", "place_id": "ChIJx", "lat": 28.4, "lng": -81.2, "circle_place_id": "pGym",
    })
    assert ctx["event_draft"]["place_id"] == "ChIJx"
    assert ctx["event_draft"]["circle_place_id"] == "pGym"
    assert ctx["event_place_asked"] is True


def test_the_profile_response_carries_the_create_event_venue() -> None:
    from app import main

    venue = {"name": "RCC", "address": "", "place_id": "creator:rcc", "lat": None, "lng": None,
             "circle_place_id": "pRCC"}
    data = {"place_id": "pRCC", "place_name": "RCC", "create_event_venue": venue}
    with mock.patch.object(main, "verify_auth", return_value=mock.Mock(user_id="u1", phone_verified=True)), \
            mock.patch("app.community_surface.community_profile", return_value=data):
        out = main.post_circles_profile(main.CommunityProfileBody(place_id="pRCC"), authorization="Bearer x")
    assert out.create_event_venue is not None
    assert out.create_event_venue.circle_place_id == "pRCC"


def test_a_venue_name_is_looked_up_near_the_host_not_lake_nona() -> None:
    from app import event_location as el

    sb = mock.MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = (
        mock.Mock(data=[{"home_block_id": "b9", "home_zip": "95112"}])
    )
    with mock.patch.object(el, "service_client", return_value=sb), mock.patch.object(
        el, "_geocode_venue", return_value=(37.33, -121.88)
    ) as geo:
        lat, lng, _ = el.resolve_event_location("u1", "Student Union")
    assert geo.call_args.args == ("Student Union", "95112")
    assert (lat, lng) == (37.33, -121.88)


# ── Fix 3: a confident community question escapes the clarifier ────────────────────


def test_a_community_question_is_answered_not_reclarified() -> None:
    from app.discovery_route import handle_discovery_turn

    slots = {
        "goal": "chat",
        "linear_intent": "discovery.communities",
        "confidence": 0.95,
        "in_discovery": False,
    }
    ctx = {
        "routing_phase": "listening",
        "out_of_scope_pending": True,
        "out_of_scope_offer": {"q": "I can't pull in SJSU's events page — want me to…?"},
    }
    with mock.patch("app.discovery_route.discovery_ai_enabled", return_value=True), mock.patch(
        "app.discovery_route.discovery_slots_for_turn", return_value=slots
    ), mock.patch("app.discovery_route.log_feature_request") as log, mock.patch(
        "app.community_discovery.communities_chat_turn", return_value="YOUR COMMUNITIES"
    ):
        reply, out, _, _ = handle_discovery_turn(
            "what community am I a part of?",
            session_ctx=ctx,
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="b1",
            is_anonymous=False,
            history=[],
            user_id="u1",
        )
    assert reply == "YOUR COMMUNITIES"
    assert not out.get("out_of_scope_pending")
    log.assert_not_called()


def test_an_unsure_read_still_gets_the_clarifier() -> None:
    from app.discovery_route import _reply_pivots_to_supported

    unsure = {"goal": "chat", "linear_intent": "discovery.communities", "confidence": 0.6}
    errand = {"goal": "out_of_scope", "linear_intent": "system.out_of_scope", "confidence": 0.95}
    assert not _reply_pivots_to_supported(unsure, "hmm")
    assert not _reply_pivots_to_supported(errand, "no, just do it")


# ── Fix 4: the club setup walk ───────────────────────────────────────────────────────


def test_a_club_is_asked_where_it_meets_not_which_club() -> None:
    step = community_head_step("hobby", question="Which club is this for?")
    assert step["question"] == "Where does the group meet?"
    # A venue type keeps the question written for it.
    assert community_head_step("friends", question="Which bakery is it?")["question"] == (
        "Which bakery is it?"
    )


def _run(monkeypatch: Any, msg: str, ctx: dict[str, Any], extracted: dict[str, Any]) -> str:
    monkeypatch.setattr(cc, "_extract_fields", lambda **_: extracted)
    monkeypatch.setattr(cc, "_place_suggestions", lambda *a, **k: [])
    monkeypatch.setattr(
        "app.reply_compose.compose_reply", lambda *, goal, facts, fallback, **k: fallback
    )
    return cc.run_community_capture_turn(
        user_message=msg, session_ctx=ctx, history=[], user_jwt="jwt", user_id="u1", home_block_id="b1"
    )


def test_a_skipped_question_never_shares_its_number(monkeypatch: Any) -> None:
    ctx: dict[str, Any] = {}
    seen: list[str] = []
    reply = _run(monkeypatch, "make a community for RCC, our computing club", ctx,
                 {"name": "RCC", "circle_type": "hobby"})
    msgs = ["we talk responsible tech", "Wednesdays 6pm", "the engineering building", "students", "skip", "skip"]
    for m in [None, *msgs]:
        if m is not None:
            reply = _run(monkeypatch, m, ctx, {})
        found = re.search(r"\((\d+)/(\d+)\)", reply or "")
        if not found:
            break
        seen.append(found.group(1))
    assert len(seen) >= 3, seen
    assert len(seen) == len(set(seen)), f"repeated step numbers: {seen}"
    assert seen == sorted(seen, key=int)
