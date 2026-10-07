"""Asking for "more" over a list that is already everything (2026-10-07, Orlando).

"what others" / "no others" / "any other" each re-ran the same browse, got the same single
card, and answered with the same canned "Here's what I found in Orlando, FL…" header —
three times running, so Lana read as stuck. The same cards twice in a row now get an
AI-authored "that's all there is" with the moves that are left, never the header again.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import app.activity_browse as ab

_BOOKS = {"id": "e1", "title": "Books & Neighbors Gathering", "distance_meters": 3000.0}
_RUN = {"id": "e2", "title": "Sunday Run Club", "distance_meters": 2000.0}


def _turn(
    ctx: dict[str, Any],
    msg: str,
    history: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    interest: str = "",
    compose: mock.Mock | None = None,
) -> str:
    patches = [
        mock.patch.object(ab, "_fetch_block_events", return_value=[dict(e) for e in events]),
        mock.patch.object(
            ab, "_fetch_admitted_events", return_value=([dict(e) for e in events], False)
        ),
        mock.patch.object(ab, "_filter_events_by_query", side_effect=lambda r, q, **_: (r, "")),
        mock.patch.object(ab, "_topic_from_slots", return_value=interest),
        mock.patch.object(ab, "_zip_gate_frame", return_value=None),
        mock.patch.object(ab, "_far_offer", return_value=([], "", [])),
        mock.patch.object(ab, "_attach_host_names"),
        mock.patch("app.lana_paths.stretch_offer_enabled", return_value=False),
        mock.patch("app.orchestrator.llm.llm_configured", return_value=False),
        mock.patch("app.discovery_route._try_assign_home_block"),
    ]
    if compose is not None:
        patches.append(mock.patch("app.reply_compose.compose_reply", compose))
    for p in patches:
        p.start()
    try:
        reply = ab.run_activity_browse_turn(
            user_message=msg, session_ctx=ctx, history=history, user_jwt="jwt",
            home_block_id="b-home", slots={}, user_id="u1",
        )
    finally:
        mock.patch.stopall()
    # The transcript grows by the user turn and Lana's reply, as main.py lists it.
    history.extend([{"role": "user", "content": msg}, {"role": "assistant", "content": reply}])
    return reply


def _ctx() -> dict[str, Any]:
    return {"activity_browse_active": True, "browse_draft": {"_asked": True},
            "phone_verified": True}


def test_the_orlando_loop_says_thats_all_instead_of_repeating() -> None:
    ctx, history = _ctx(), []
    first = _turn(ctx, "any events?", history, [_BOOKS])
    assert "only" not in first.lower()

    again = _turn(ctx, "any other", history, [_BOOKS])
    assert again != first
    assert "only meet" in again
    # The card stays on screen; the moves left are pills, and an open ask has no topic
    # to listen for, so hosting is the one on offer.
    assert [p["activity_id"] for p in ctx["activity_previews"]] == ["e1"]
    assert ctx["browse_draft"]["suggestions"] == ["Host a meet"]
    assert ctx["browse_draft"]["_seek_offer"] is False

    third = _turn(ctx, "no others", history, [_BOOKS])
    assert third != first
    assert ctx["browse_last_shown"]["repeats"] == 2


def test_a_topic_repeat_offers_to_listen_and_arms_the_offer() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any running meets?", history, [_RUN], interest="running")
    reply = _turn(ctx, "more?", history, [_RUN], interest="running")
    assert "running" in reply
    assert ctx["browse_draft"]["suggestions"] == ["Yes, listen for me", "Host a meet"]
    # So "yes" next turn is read as taking the listen offer, not as a new search.
    assert ctx["browse_draft"]["_seek_offer"] is True


def test_the_ai_is_told_nothing_else_exists_and_escalates_on_a_second_repeat() -> None:
    compose = mock.Mock(return_value="That's it for now!")
    ctx, history = _ctx(), []
    _turn(ctx, "any events?", history, [_BOOKS])
    _turn(ctx, "any other", history, [_BOOKS], compose=compose)
    facts = " ".join(compose.call_args.kwargs["facts"])
    assert "Books & Neighbors Gathering" in facts
    assert "NO other meets" in facts
    assert "already told them" not in facts

    _turn(ctx, "no others", history, [_BOOKS], compose=compose)
    assert "already told them" in " ".join(compose.call_args.kwargs["facts"])


def test_different_cards_are_a_fresh_list() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any events?", history, [_BOOKS])
    reply = _turn(ctx, "what about runs", history, [_RUN, _BOOKS])
    assert "only" not in reply.lower()
    assert ctx["browse_last_shown"]["repeats"] == 0


def test_the_same_cards_much_later_are_not_a_loop() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any events?", history, [_BOOKS])
    history.extend([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}] * 3)
    reply = _turn(ctx, "any events?", history, [_BOOKS])
    assert "only" not in reply.lower()


def test_an_empty_turn_in_between_resets_the_streak() -> None:
    # The real sequence: cards, then "music?" came up empty, then "what others" showed the
    # card again — that third turn is a fresh look, only the fourth is the repeat.
    ctx, history = _ctx(), []
    _turn(ctx, "any events in orlando?", history, [_BOOKS])
    _turn(ctx, "any music events?", history, [], interest="music")
    back = _turn(ctx, "what others", history, [_BOOKS])
    assert "only" not in back.lower()
    assert "only meet" in _turn(ctx, "no others", history, [_BOOKS])


def test_the_streak_survives_the_lane_releasing_between_turns() -> None:
    # The draft is reset when the lane releases and re-enters; the loop must still show.
    ctx, history = _ctx(), []
    _turn(ctx, "any events?", history, [_BOOKS])
    ab.reset_activity_browse_state(ctx)
    ctx.update(_ctx())
    assert "only meet" in _turn(ctx, "any other", history, [_BOOKS])


# ── Replies read by the AI (app/browse_followup_ai.py) ──────────────────────────────────


def _with_verdict(verdict: str | None):
    return mock.patch("app.browse_followup_ai.read_browse_followup", return_value=verdict)


def test_no_to_the_empty_offer_closes_instead_of_listing_everything() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any yoga?", history, [_BOOKS], interest="yoga")  # filter mock admits all
    ctx["browse_draft"]["_seek_offer"] = True  # as an empty yoga search leaves it
    with _with_verdict("decline"):
        reply = _turn(ctx, "no", history, [_BOOKS, _RUN])
    assert "Here's what" not in reply
    assert ctx["activity_browse_active"] is None
    assert ctx["browse_offer_response"]["response"] == "decline"


def test_nah_after_cards_closes() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any events?", history, [_BOOKS])
    with _with_verdict("decline"):
        reply = _turn(ctx, "nah", history, [_BOOKS])
    assert "Here's what" not in reply
    assert ctx["activity_browse_active"] is None


def test_what_others_after_an_empty_topic_shows_everything_not_the_words_as_topic() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any music?", history, [_BOOKS], interest="music")
    ctx["browse_draft"]["_seek_offer"] = True
    with _with_verdict("more"):
        reply = _turn(ctx, "what others", history, [_BOOKS], interest="what others")
    assert "what others" not in reply
    assert ctx["browse_draft"]["interest"] == ""
    assert ctx["browse_offer_response"]["response"] == "more"


def test_more_over_the_same_cards_is_the_thats_all_reply() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any badminton?", history, [_RUN], interest="running")
    with _with_verdict("more"):
        reply = _turn(ctx, "any others?", history, [_RUN], interest="")
    assert "only meet" in reply
    # The topic survives: "more" re-runs the same search, it does not drop to everything.
    assert ctx["browse_draft"]["interest"] == "running"


def test_no_model_keeps_the_old_reading() -> None:
    ctx, history = _ctx(), []
    _turn(ctx, "any events?", history, [_BOOKS])
    with _with_verdict(None):
        reply = _turn(ctx, "any other", history, [_BOOKS])
    assert "only meet" in reply  # the repeat guard still holds without the reader


def test_no_after_the_lane_lets_go_still_answers_the_offer() -> None:
    # The live failure: the empty yoga offer, then "no" released the sticky lane and the
    # router re-entered browse fresh — the draft (and the offer) were wiped and "no"
    # listed every meet. Re-entry right after a browse keeps the draft.
    from app.discovery_route import _start_activity_browse_from_discovery

    ctx, history = _ctx(), []
    _turn(ctx, "any yoga?", history, [_BOOKS], interest="yoga")
    ctx["browse_draft"]["_seek_offer"] = True
    assert ctx["browse_last_turn_at"] == 0
    with _with_verdict("decline"), \
         mock.patch.object(ab, "_fetch_block_events", return_value=[dict(_BOOKS)]):
        reply, out, _, _ = _start_activity_browse_from_discovery(
            msg="no", session_ctx=ctx, history=history, user_jwt="jwt", home_block_id="b",
            slots={}, user_id="u1",
        )
    assert "Here's what" not in reply
    assert ctx["browse_offer_response"]["response"] == "decline"
    assert ctx["browse_last_turn_at"] is None  # closed: nothing left to answer


def test_a_fresh_browse_much_later_starts_clean() -> None:
    from app.discovery_route import _start_activity_browse_from_discovery

    ctx, history = _ctx(), []
    _turn(ctx, "any yoga?", history, [_BOOKS], interest="yoga")
    ctx["browse_draft"]["_seek_offer"] = True
    history.extend([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}] * 3)
    with mock.patch("app.browse_followup_ai.read_browse_followup") as reader, \
         mock.patch.object(ab, "_fetch_block_events", return_value=[dict(_BOOKS)]), \
         mock.patch.object(ab, "_filter_events_by_query", side_effect=lambda r, q, **_: (r, "")), \
         mock.patch.object(ab, "_topic_from_slots", return_value=""), \
         mock.patch.object(ab, "_far_offer", return_value=([], "", [])), \
         mock.patch.object(ab, "_zip_gate_frame", return_value=None), \
         mock.patch("app.orchestrator.llm.llm_configured", return_value=False):
        _start_activity_browse_from_discovery(
            msg="any events?", session_ctx=ctx, history=history, user_jwt="jwt",
            home_block_id="b", slots={}, user_id="u1",
        )
    reader.assert_not_called()
    assert not ctx["browse_draft"].get("_seek_offer")
