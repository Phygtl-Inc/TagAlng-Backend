"""Guards key on what the person READ, not on the kind the model chose.

Prod 2026-08-06, three turns in a row:
  "Do you have a favorite spot around here, or do you make it at home?"
  "do you have a favorite pizza spot around here, or are you still exploring?"
  "Is there a place you've tried recently that stood out?"
All shipped as kind='reply' (confirmed in lana_audit_log). note_ask_streak only
counted ("ask_gap", "ground_place"), so `reply` fell to the else-branch and RESET
the counter each time — the cap of 2 saw a streak of 0 and never engaged. None
carried chips either, and the dead-end backstop was satisfied by the '?' alone.
"""

from __future__ import annotations

from app.policy.decide import (
    MAX_CONSECUTIVE_ASKS,
    NextAction,
    _question_names_its_options,
    _revision_note,
    ask_streak,
    note_ask_streak,
    turn_asks_personal_question,
)

PIZZA_TURNS = [
    "Pizza is always a classic! Do you have a favorite spot around here, or do you make it at home?",
    "Thanks for clarifying, Asjid! Just curious — do you have a favorite pizza spot "
    "around here, or are you still exploring?",
    "Exploring new pizza spots is half the fun! Is there a place you've tried recently that stood out?",
]


def _a(kind: str, utterance: str = "", **kw) -> NextAction:
    return NextAction(kind=kind, utterance=utterance, **kw)


# --- what counts as an ask -------------------------------------------------


def test_a_reply_containing_a_question_counts() -> None:
    for text in PIZZA_TURNS:
        assert turn_asks_personal_question(_a("reply", text)), text


def test_a_reply_without_a_question_does_not_count() -> None:
    assert not turn_asks_personal_question(
        _a("reply", "All good — if you ever want to set something up nearby, just say the word.")
    )


def test_ask_gap_counts_even_with_no_question_mark_yet() -> None:
    """The vetted question is pasted in downstream by _wire_ask_gap_action."""
    assert turn_asks_personal_question(_a("ask_gap", "Just curious—"))


def test_an_offer_question_is_not_charged_to_the_interrogation_budget() -> None:
    """"want me to find pizza spots near you?" is a GIFT. Counting it would
    suppress the exact turns the pizza conversation was missing."""
    assert not turn_asks_personal_question(
        _a("bridge_offer", "Want me to find pizza spots near you?")
    )
    assert not turn_asks_personal_question(
        _a("capture_defer", "Noted — shall I look into that later?")
    )


# --- the streak actually accumulates now ----------------------------------


def test_three_reply_questions_reach_the_cap() -> None:
    ctx: dict = {}
    seen = []
    for text in PIZZA_TURNS:
        seen.append(ask_streak(ctx))
        note_ask_streak(ctx, _a("reply", text))
    assert seen == [0, 1, 2], f"shipped code produced [0, 0, 0]; got {seen}"
    assert seen[-1] >= MAX_CONSECUTIVE_ASKS, "the 3rd question must arrive at the cap"


def test_a_non_asking_turn_clears_the_streak() -> None:
    ctx: dict = {}
    note_ask_streak(ctx, _a("reply", "Do you have a favorite spot?"))
    assert ask_streak(ctx) == 1
    note_ask_streak(ctx, _a("bridge_offer", "Want me to find pizza spots near you?"))
    assert ask_streak(ctx) == 0


def test_over_cap_reply_question_is_sent_back_for_revision() -> None:
    note = _revision_note(_a("reply", PIZZA_TURNS[2]), streak=MAX_CONSECUTIVE_ASKS)
    assert note and "interrogation" in note
    assert "GIVE" in note, "the revision must push an offer, not just drop the question"


def test_under_cap_reply_question_is_left_alone() -> None:
    """An OPEN question under the cap is a fine turn: nothing to tap because no
    options were named, and the '?' clears the dead-end check. (PIZZA_TURNS[0]
    and [1] name their options, so those are flagged for chips at any streak.)"""
    assert _revision_note(_a("reply", PIZZA_TURNS[2]), streak=0) is None


# --- closed questions must ship their options as chips --------------------


def test_either_or_questions_are_detected() -> None:
    for text in PIZZA_TURNS[:2]:
        assert _question_names_its_options(text), text


def test_an_open_question_is_not_a_closed_one() -> None:
    assert not _question_names_its_options("Any favorite local pizza spots you'd recommend?")
    assert not _question_names_its_options(PIZZA_TURNS[2])


def test_or_in_the_lead_in_does_not_count() -> None:
    """Only the sentence ending in '?' decides. An "or" while warming up lists
    nothing the person can answer with."""
    assert not _question_names_its_options(
        "Pizza or pasta, you have taste! What do you like about it?"
    )


def test_closed_question_without_chips_is_sent_back() -> None:
    note = _revision_note(_a("reply", PIZZA_TURNS[1]), streak=0)
    assert note and "chip" in note


def test_closed_question_with_chips_passes() -> None:
    action = _a(
        "reply",
        PIZZA_TURNS[1],
        chips=[
            {"label": "A favorite spot", "send": "I have a favorite pizza spot"},
            {"label": "Still exploring", "send": "Exploring"},
        ],
    )
    assert _revision_note(action, streak=0) is None


def test_offer_without_a_chip_is_sent_back() -> None:
    note = _revision_note(
        _a("bridge_offer", "I can help you find pizza spots nearby."), streak=0
    )
    assert note and "tap" in note


def test_distress_turn_may_stay_bare() -> None:
    """Silence IS the decision there — no chip pressure, no question pressure."""
    action = _a("reply", "That sounds rough. I hope it eases up soon.", distress_turn=True)
    assert _revision_note(action, streak=0) is None


# --- an offer's goal_id is observable -------------------------------------


def test_offer_without_a_goal_id_is_logged(caplog) -> None:
    """Prod 12:43:57 pitched sharing.host with goal_id None, so nothing
    downstream could tell which capability was offered."""
    from app.policy.decide import audit_offer_goal

    with caplog.at_level("WARNING"):
        audit_offer_goal(
            _a("bridge_offer", "I can help you organize a pizza night nearby."),
            [{"id": "cap:sharing.host"}],
            "u1",
        )
    assert "decide_turn_offer_without_goal" in caplog.text


def test_goal_id_not_on_this_turns_menu_is_logged(caplog) -> None:
    from app.policy.decide import audit_offer_goal

    with caplog.at_level("WARNING"):
        audit_offer_goal(
            _a("bridge_offer", "Want a hand?", goal_id="cap:looking.swap"),
            [{"id": "cap:sharing.host"}],
            "u1",
        )
    assert "decide_turn_goal_not_on_menu" in caplog.text


def test_a_valid_offer_goal_logs_nothing(caplog) -> None:
    from app.policy.decide import audit_offer_goal

    with caplog.at_level("WARNING"):
        audit_offer_goal(
            _a("bridge_offer", "Want a hand?", goal_id="cap:sharing.host"),
            [{"id": "cap:sharing.host"}, {"id": "gap:pizza"}],
            "u1",
        )
    assert caplog.text == ""


def test_a_plain_reply_needs_no_goal_id(caplog) -> None:
    from app.policy.decide import audit_offer_goal

    with caplog.at_level("WARNING"):
        audit_offer_goal(_a("reply", "Pizza is always a good call."), [], "u1")
    assert caplog.text == ""
