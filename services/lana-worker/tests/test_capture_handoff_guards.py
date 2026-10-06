"""The capture lanes' own hand-offs must never be classified out of the lane.

§35(g) / §38(b): the carousel stamps every answer through /tip-setup (/community-setup)
and then sends "Looks good" — the only thing that renders the ready card. That message
went through the release check first, and a goal=chat read (or an abandon read) released
the lane and reset it: a filled-in carousel and the `*_ready` flag the endpoint had just
written were thrown away. The host flow is immune because `_is_host_confirm` intercepts
the same words before any release check; these are that intercept for the two capture
lanes, scoped to the client's hand-off phrase and the state it is sent in.

Also here: §35(a) the fork turn, §35(e) the button name, §35(h) a swapped subject, §38(a)
inert subject-step chips.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import app.community_capture as cc
import app.tip_share as ts
from app.lane_decision import is_setup_handoff

# Every read a stateless classifier could give the bare words "Looks good" that releases
# a lane: a confident chat/meta turn, a confident foreign intent, and an abandon.
_HOSTILE_READS = [
    {"goal": "chat", "confidence": 0.95},
    {"goal": "find_peers", "confidence": 0.9, "linear_intent": "discovery.find_peers"},
    {"goal": "chat", "confidence": 0.9, "abandon": True},
]

_TIP_STEPS = [
    {"field": "subject", "label": "Who", "question": "What's the doctor's name?",
     "kind": "text", "required": True},
    {"field": "ages", "label": "Ages", "question": "Which ages does she see?",
     "kind": "choice", "options": ["Babies", "Toddlers", "Big kids"], "required": True},
    {"field": "walk_ins", "label": "Walk-ins", "question": "Does she take walk-ins?",
     "kind": "choice", "options": ["Yes", "No"], "required": False},
]


def _tip_ctx(*, ready: bool = True, step_set: bool = True) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "draft_id": "d1",
        "name": "Dr. Sarah",
        "category": "pediatric dentist",
        "reco_type": "professional",
        "answers": {"subject": "Dr. Sarah", "ages": "Toddlers", "walk_ins": "Yes"},
    }
    if step_set:
        draft["step_set"] = list(_TIP_STEPS)
    return {
        "tip_share_active": True,
        "tip_draft": draft,
        "tip_ready": True if ready else None,
        "tip_asked_fields": ["subject", "ages", "walk_ins"],
        "tip_turns": 3,
    }


# ── the shared matcher ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("msg", ["Looks good", "looks good", "  Looks  good. ", "LOOKS GOOD!"])
def test_handoff_matches_the_clients_phrase(msg: str) -> None:
    assert is_setup_handoff(msg)


@pytest.mark.parametrize(
    "msg",
    ["", "looks good but never mind", "it looks good to me, find me a dentist", "good",
     "Looks good · next"],
)
def test_handoff_is_exact_not_a_substring(msg: str) -> None:
    # Rule 1: an exact match on a rendered control, never a keyword guess at free text.
    assert not is_setup_handoff(msg)


# ── §35(g) tip lane ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slots", _HOSTILE_READS)
def test_tip_handoff_never_releases_a_stamped_carousel(slots: dict[str, Any]) -> None:
    assert ts.tip_share_should_release("Looks good", _tip_ctx(), slots) is False


@pytest.mark.parametrize("slots", _HOSTILE_READS)
def test_tip_handoff_guard_is_scoped_to_a_ready_carousel(slots: dict[str, Any]) -> None:
    # Without tip_ready (nothing stamped yet), or without a step set, "Looks good" is just
    # words and the classifier's read decides, exactly as before.
    assert ts.tip_share_should_release("Looks good", _tip_ctx(ready=False), slots) is True
    assert ts.tip_share_should_release("Looks good", _tip_ctx(step_set=False), slots) is True


def test_typed_text_around_the_phrase_still_pivots() -> None:
    slots = {"goal": "find_peers", "confidence": 0.9, "linear_intent": "discovery.find_peers"}
    assert ts.tip_share_should_release("looks good, now find me a dentist", _tip_ctx(), slots)


def test_typed_option_still_releases_on_abandon() -> None:
    # The no-trapping rule the guard must not widen (test_abandon_wins_over_an_offered_option):
    # an option the user could TYPE still releases on an abandon read.
    ctx = _tip_ctx()
    ctx["tip_draft"]["suggestions"] = ["Toddlers"]
    assert ts.tip_share_should_release("Toddlers", ctx, {"goal": "chat", "abandon": True})


def test_tip_handoff_turn_returns_the_ready_card_without_reading_the_phrase(monkeypatch) -> None:
    def _no_extract(**_: Any):
        raise AssertionError("the hand-off is a control — it must not be extracted as content")

    monkeypatch.setattr(ts, "_extract_tip_fields", _no_extract)
    ctx = _tip_ctx()
    reply = ts.run_tip_share_turn(
        user_message="Looks good", session_ctx=ctx, history=[], user_jwt="", home_block_id="b1",
        slots={"goal": "chat", "confidence": 0.95},
    )
    draft = ctx["tip_draft"]
    assert draft["ready"] is True and ctx["tip_ready"] is True
    assert ctx["tip_share_active"] is True
    assert draft["answers"] == {"subject": "Dr. Sarah", "ages": "Toddlers", "walk_ins": "Yes"}
    assert "Drop the recommendation" in reply


def test_tip_carousel_end_to_end_through_the_real_endpoint(monkeypatch) -> None:
    """/tip-setup (real) → release check under a hostile read → the turn (real)."""
    from app.auth import AuthSession
    from app.main import TipSetupRequest, set_tip_setup

    ctx = _tip_ctx(ready=False)
    ctx["tip_draft"]["answers"] = {"subject": "Dr. Sarah"}
    written: dict[str, Any] = {}
    with (
        patch("app.main.verify_auth", return_value=AuthSession(
            user_id="u-1", is_anonymous=False, phone_verified=True, home_block_id="b1")),
        patch("app.main.get_session_for_user", return_value={"context": ctx}),
        patch("app.main.update_session_context", side_effect=lambda sid, c: written.update(c)),
        patch("app.tip_share.judge_answers", return_value=[]),
    ):
        res = set_tip_setup(
            "s-1", TipSetupRequest(answers={"ages": "Toddlers", "walk_ins": "No"}),
            authorization="Bearer t",
        )
    assert res["weak"] == [] and written["tip_ready"] is True
    for slots in _HOSTILE_READS:
        assert ts.tip_share_should_release("Looks good", dict(written), slots) is False
    monkeypatch.setattr(ts, "_extract_tip_fields", lambda **_: ({}, None))
    reply = ts.run_tip_share_turn(
        user_message="Looks good", session_ctx=written, history=[], user_jwt="",
        home_block_id="b1", slots=_HOSTILE_READS[0],
    )
    assert written["tip_draft"]["ready"] is True
    assert written["tip_draft"]["answers"]["walk_ins"] == "No"
    assert "Drop the recommendation" in reply


# ── §38(b) community lane ─────────────────────────────────────────────────────────────


def _community_ctx(*, ready: bool = True) -> dict[str, Any]:
    from app.community_question_sets import community_steps_for

    steps = community_steps_for("fitness")
    return {
        "community_create_active": True,
        "community_turns": 3,
        "community_ready": True if ready else None,
        "community_draft": {
            "draft_id": "c1",
            "name": "CF Fitness",
            "circle_type": "fitness",
            "google_place_id": "gp1",
            "step_set": steps,
            "answers": {s["field"]: "x" for s in steps},
        },
        "community_asked_fields": [s["field"] for s in steps],
    }


@pytest.mark.parametrize("slots", _HOSTILE_READS)
def test_community_handoff_never_releases_a_stamped_carousel(slots: dict[str, Any]) -> None:
    assert cc.community_capture_should_release("Looks good", _community_ctx(), slots) is False


@pytest.mark.parametrize("slots", _HOSTILE_READS)
def test_community_handoff_guard_is_scoped_to_a_ready_carousel(slots: dict[str, Any]) -> None:
    assert cc.community_capture_should_release("Looks good", _community_ctx(ready=False), slots)


def test_community_handoff_turn_returns_the_ready_card(monkeypatch) -> None:
    def _no_extract(**_: Any):
        raise AssertionError("the hand-off must not be extracted as content")

    monkeypatch.setattr(cc, "_extract_fields", _no_extract)
    ctx = _community_ctx()
    reply = cc.run_community_capture_turn(
        user_message="Looks good", session_ctx=ctx, history=[], user_jwt="", user_id="u-1",
        home_block_id="b1",
    )
    assert ctx["community_ready"] is True
    assert ctx["community_draft"]["ready"] is True
    assert ctx["community_create_active"] is True
    assert "Share with the community" in reply


# ── §38(a) inert subject-step chips ───────────────────────────────────────────────────


def test_pinned_type_subject_step_sends_no_place_chips() -> None:
    # A typed/tapped name is dropped on a pinned type's subject step, so a chip there can
    # only re-ask the question. Nothing is sent; the map picker is the answer control.
    with patch("app.places.nearby_place_suggestions", return_value=["Some Gym"]) as near:
        out = cc._place_suggestions({"circle_type": "fitness", "name": "CF"}, zip_code="32827",
                                    block_id="b1")
    assert out == [] and not near.called


def test_group_subject_step_keeps_place_chips() -> None:
    # A club's subject step asks where it meets and keeps a tapped name as meets_at — the
    # chips land there, so they stay.
    with patch("app.places.nearby_place_suggestions", return_value=["Engineering Bldg"]):
        out = cc._place_suggestions({"circle_type": "hobby"}, zip_code="95112", block_id="b1")
    assert out == ["Engineering Bldg"]


# ── §35(a) the fork turn ──────────────────────────────────────────────────────────────


def _opening(monkeypatch, *, fork: bool) -> tuple[str, dict[str, Any]]:
    if fork:
        monkeypatch.setenv("LANA_TIP_FORK", "1")
    else:
        monkeypatch.delenv("LANA_TIP_FORK", raising=False)
    monkeypatch.setattr(ts, "_name_suggestions", lambda *a, **k: [])
    monkeypatch.setattr(ts, "my_communities", lambda *_: [])
    monkeypatch.setattr(
        ts, "_extract_tip_fields",
        lambda **_: ({"name": "Dr. Sarah", "category": "pediatric dentist",
                      "reco_type": "professional", "steps_raw": None}, None),
    )
    ctx: dict[str, Any] = {"tip_share_active": True}
    reply = ts.run_tip_share_turn(
        user_message="Dr. Sarah, our pediatric dentist", session_ctx=ctx, history=[],
        user_jwt="", home_block_id="b1",
    )
    return reply, ctx


def test_fork_off_by_default_keeps_todays_first_question(monkeypatch) -> None:
    from app.ui_intent import derive_ui_intent

    reply, ctx = _opening(monkeypatch, fork=False)
    assert not ctx.get("tip_fork_pending")
    assert ctx.get("tip_pending_ask")
    assert derive_ui_intent(ctx) == "collect_tip_detail"
    assert "/" in reply  # "(2/N)"


def test_fork_turn_asks_the_fork_with_steps_populated(monkeypatch) -> None:
    from app.ui_intent import derive_ui_intent

    reply, ctx = _opening(monkeypatch, fork=True)
    draft = ctx["tip_draft"]
    assert ctx["tip_fork_pending"] is True
    assert derive_ui_intent(ctx) == "collect_tip_fork"
    # Both forks can render from this one response.
    assert draft["steps"] and draft["tailored"] is True
    # The first step is held back: not pending, not counted as asked, no "(1/8)".
    assert not ctx.get("tip_pending_ask") and not draft.get("pending_field")
    assert not ctx.get("tip_asked_fields")
    assert "flip through cards" in reply.lower() and "/" not in reply
    assert ctx["tip_pending_question"] == "How should we take it down?"


def test_chat_pick_asks_the_held_back_first_question(monkeypatch) -> None:
    from app.ui_intent import derive_ui_intent

    _, ctx = _opening(monkeypatch, fork=True)
    # The pick is a rendered control: it stays in the lane whatever the classifier says.
    assert ts.tip_share_should_release("Just chat with me", ctx, _HOSTILE_READS[2]) is False

    def _no_extract(**_: Any):
        raise AssertionError("the fork pick is not content")

    monkeypatch.setattr(ts, "_extract_tip_fields", _no_extract)
    reply = ts.run_tip_share_turn(
        user_message="Just chat with me", session_ctx=ctx, history=[], user_jwt="",
        home_block_id="b1",
    )
    assert not ctx.get("tip_fork_pending")
    assert ctx["tip_pending_ask"]
    assert "Just chat with me" not in (ctx["tip_draft"].get("answers") or {}).values()
    assert derive_ui_intent(ctx) == "collect_tip_detail"
    assert "/" in reply


def test_fork_pick_phrase_outside_the_fork_is_just_words() -> None:
    ctx = _tip_ctx(ready=False)
    assert not ts._is_fork_pick("Just chat with me", ctx)


# ── §35(h) a swapped subject rewrites the set ─────────────────────────────────────────


def test_new_subject_rewrites_the_set_and_drops_the_old_answers(monkeypatch) -> None:
    monkeypatch.delenv("LANA_TIP_FORK", raising=False)
    monkeypatch.setattr(ts, "_name_suggestions", lambda *a, **k: [])
    monkeypatch.setattr(ts, "my_communities", lambda *_: [])
    ctx = _tip_ctx(ready=False)
    ctx["tip_pending_ask"] = "walk_ins"
    ctx["tip_draft"]["trait"] = "gentle with toddlers"
    ctx["tip_draft"]["circle_place_id"] = "p-community"
    calls: list[bool] = []

    def _extract(*, prev: dict[str, Any], **_: Any):
        calls.append(bool(prev.get("step_set")))
        if prev.get("step_set") and prev.get("reco_type") == "professional":
            # The swap turn: read against the dentist's set.
            return ({"name": "Canvas", "category": "restaurant", "reco_type": "restaurant",
                     "reply_role": "new_subject"}, None)
        if not prev.get("step_set"):
            # The re-ask with no set on the draft — the one that writes the new set.
            return ({"steps_raw": [
                {"field": "subject", "question": "Which restaurant?"},
                {"field": "must_order", "label": "Must order",
                 "question": "What's the one dish to order at Canvas?",
                 "options": ["Short rib", "Burger", "Brunch"]},
            ]}, None)
        return ({}, None)  # the backfill re-read

    monkeypatch.setattr(ts, "_extract_tip_fields", _extract)
    ts.run_tip_share_turn(
        user_message="actually it's Canvas, the restaurant", session_ctx=ctx, history=[],
        user_jwt="", home_block_id="b1",
    )
    draft = ctx["tip_draft"]
    assert draft["reco_type"] == "restaurant" and draft["name"] == "Canvas"
    fields = {s["field"] for s in draft["step_set"]}
    assert "ages" not in fields and "walk_ins" not in fields
    assert not {"ages", "walk_ins"} & set(draft.get("answers") or {})
    assert draft["answers"].get("subject") == "Canvas"
    assert "trait" not in draft, "the dentist's trait is not about Canvas"
    assert draft["circle_place_id"] == "p-community", "where it goes is the user's pick"
    assert draft["draft_id"] != "d1"
    assert calls[:2] == [True, False], "the new set is written by a fresh extraction"


def test_a_fuller_name_for_the_same_subject_keeps_the_set(monkeypatch) -> None:
    monkeypatch.setattr(ts, "_name_suggestions", lambda *a, **k: [])
    ctx = _tip_ctx(ready=False)
    monkeypatch.setattr(
        ts, "_extract_tip_fields",
        lambda **_: ({"name": "Dr. Sarah Lee", "reply_role": "answer"}, None),
    )
    ts.run_tip_share_turn(
        user_message="her full name is Dr. Sarah Lee", session_ctx=ctx, history=[],
        user_jwt="", home_block_id="b1",
    )
    assert ctx["tip_draft"]["step_set"] == _TIP_STEPS
    assert ctx["tip_draft"]["answers"]["ages"] == "Toddlers"


def test_new_subject_role_naming_the_same_thing_is_a_noop(monkeypatch) -> None:
    monkeypatch.setattr(ts, "_name_suggestions", lambda *a, **k: [])
    ctx = _tip_ctx(ready=False)
    monkeypatch.setattr(
        ts, "_extract_tip_fields",
        lambda **_: ({"name": "dr. sarah", "reco_type": "professional",
                      "reply_role": "new_subject"}, None),
    )
    ts.run_tip_share_turn(
        user_message="Dr. Sarah", session_ctx=ctx, history=[], user_jwt="", home_block_id="b1",
    )
    assert ctx["tip_draft"]["step_set"] == _TIP_STEPS
    assert ctx["tip_draft"]["draft_id"] == "d1"


# ── §44 correct a posted recommendation in the conversation ──────────────────────────


def _posted_ctx() -> dict[str, Any]:
    """The session right after a post: capture closed, the draft kept with listed+signal_id."""
    ctx = _tip_ctx()
    ctx["tip_draft"].update({"listed": True, "ready": True, "signal_id": "sig-1"})
    for k in ("tip_share_active", "tip_ready", "tip_pending_ask", "tip_pending_question"):
        ctx[k] = None
    return ctx


def _tip_setup(ctx: dict[str, Any], **body: Any) -> tuple[Any, dict[str, Any]]:
    from fastapi import HTTPException

    from app.auth import AuthSession
    from app.main import TipSetupRequest, set_tip_setup

    written: dict[str, Any] = {}
    with (
        patch("app.main.verify_auth", return_value=AuthSession(
            user_id="u-1", is_anonymous=False, phone_verified=True, home_block_id="b1")),
        patch("app.main.get_session_for_user", return_value={"context": ctx}),
        patch("app.main.update_session_context", side_effect=lambda sid, c: written.update(c)),
        patch("app.tip_share.judge_answers", return_value=[]),
    ):
        try:
            return set_tip_setup("s-1", TipSetupRequest(**body), authorization="Bearer t"), written
        except HTTPException as exc:
            return exc, written


def test_rearm_rejects_a_signal_this_session_did_not_post() -> None:
    res, written = _tip_setup(_posted_ctx(), signal_id="someone-elses")
    assert getattr(res, "status_code", None) == 409 and res.detail == "signal_not_in_session"
    assert written == {}


def test_rearm_rejects_a_draft_that_was_never_posted() -> None:
    ctx = _tip_ctx()
    ctx["tip_draft"]["signal_id"] = "sig-1"  # no `listed`
    assert ts.rearm_posted_tip(ctx, "sig-1") is False


def test_posted_tip_is_corrected_in_place(monkeypatch) -> None:
    import app.supabase_rpc as rpc

    calls: list[tuple[str, dict[str, Any]]] = []

    def _rpc(jwt: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, payload))
        return {}

    monkeypatch.setattr(rpc, "call_rpc", _rpc)
    monkeypatch.setattr(ts, "_extract_tip_fields", lambda **_: ({}, None))

    res, ctx = _tip_setup(_posted_ctx(), signal_id="sig-1")
    assert res["ok"] and ctx["tip_share_active"] and ctx["tip_draft"]["edit_signal_id"] == "sig-1"

    # The live card's chip, under a read that would otherwise release the lane.
    assert ts.tip_share_should_release("fix:ages", ctx, _HOSTILE_READS[0]) is False
    ts.run_tip_share_turn(user_message="fix:ages", session_ctx=ctx, history=[], user_jwt="jwt",
                          home_block_id="b1")
    assert ctx["tip_pending_ask"] == "ages"
    ts.run_tip_share_turn(user_message="Big kids", session_ctx=ctx, history=[], user_jwt="jwt",
                          home_block_id="b1")
    assert ctx["tip_ready"] is True
    reply = ts.run_tip_share_turn(user_message="pass the tip along", session_ctx=ctx,
                                  history=[], user_jwt="jwt", home_block_id="b1")

    names = [n for n, _ in calls]
    assert "save_local_signal" not in names, "a correction must never insert a second row"
    assert names == ["set_signal_reco"]
    payload = calls[0][1]
    assert payload["p_signal_id"] == "sig-1"
    ages = next(f for f in payload["p_reco_fields"] if f["field"] == "ages")
    assert ages["answer"] == "Big kids"
    draft = ctx["tip_draft"]
    assert draft["signal_id"] == "sig-1" and draft["listed"] is True
    assert "edit_signal_id" not in draft
    assert ctx["tip_listed_now"] is True and not ctx["tip_share_active"]
    assert "Updated" in reply


def test_fix_chip_for_an_unknown_field_is_just_words() -> None:
    assert not ts._is_fix_chip("fix:password", _tip_ctx())
    assert ts._is_fix_chip("fix:ages", _tip_ctx())


def test_a_normal_post_still_inserts(monkeypatch) -> None:
    # The update branch is keyed on edit_signal_id alone — a fresh capture still posts.
    def _boom(**_: Any):
        raise AssertionError("not an edit")

    monkeypatch.setattr(ts, "_update_posted_tip", _boom)
    with patch("app.local_signals.save_local_signal",
               return_value={"signal_id": "sig-new", "matches_created": 0}) as save:
        saved, _ = ts._save_tip(draft=dict(_tip_ctx()["tip_draft"]), user_jwt="",
                                block_id="b1", zip_code=None)
    assert save.called and saved["signal_id"] == "sig-new"
