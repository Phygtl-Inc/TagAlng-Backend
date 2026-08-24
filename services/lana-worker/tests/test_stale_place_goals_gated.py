"""Stale place-questions can't interrupt a real topic — and duplicates stop piling up.

Two prod reports, same shape:
  · 2026-08-05 "Piazza Italia" conversation answered with "Host a meal at Mizu
    Sushi" (circle_offer pinned days earlier).
  · 2026-08-07 "Piazza Italia or Mangia" answered with "Which specific spot is your
    Fitness CF St. Cloud gym?" (ungrounded_circle) — asked while `fitness_cf` was
    ALREADY pinned to Fitness CF - St. Cloud.

The second exposed the cause behind both: capture made one row per phrasing, so one
gym had four circles and one church three — six standing questions for two real
communities, each eligible on every turn.
"""

from __future__ import annotations

import pytest

from app.circles_capture import (
    CircleCandidate,
    _names_a_different_place,
    same_community,
)
from app.policy.goals import (
    _grounding_goals,
    filter_place_goals,
    goal_matches_message,
)


# --- duplicates are recognised as one community ---------------------------


@pytest.mark.parametrize(
    "a,a_noun,b,b_noun",
    [
        # bare category vs the real gym — the NOUNS are what say it's one community
        ("gym", "gym", "fitness_cf", "gym crew"),
        ("fitness_cf", None, "fitness_cf_st_cloud", None),   # subset of the same words
        ("gym", None, "gym_st_cloud", None),                 # category inside the key
        ("restaurant", "restaurant", "piazza_italia", "restaurant regulars"),
        ("church", "church", "lagoinha_visitor", "church group"),
    ],
)
def test_duplicate_phrasings_are_the_same_community(
    a: str, a_noun: str | None, b: str, b_noun: str | None
) -> None:
    assert same_community(
        a, "fitness", b, "fitness", a_noun=a_noun, b_noun=b_noun
    ) or same_community(a, "other", b, "other", a_noun=a_noun, b_noun=b_noun)


@pytest.mark.parametrize(
    "a,a_noun,b,b_noun",
    [
        ("fitness_cf", None, "crossfit_st_cloud", None),  # two genuinely different places
        ("table_tennis_group", None, "futsal", None),
        # prod 2026-08-18: a bare category merged ANY two same-type circles, so a new
        # library mention was folded into an existing gaming zone and OrangeTheory into
        # an already-pinned gym. The nouns disagree; they are separate communities.
        ("library", "library regulars", "gaming_zone", "gaming zone"),
        ("orangetheory", "fitness studio", "gym", "gym"),
        # No noun on either side is not evidence of sameness — answer no.
        ("gym", None, "orangetheory", None),
    ],
)
def test_different_communities_stay_separate(
    a: str, a_noun: str | None, b: str, b_noun: str | None
) -> None:
    """Merging two real communities is worse than leaving a duplicate."""
    assert not same_community(
        a, "fitness", b, "fitness", a_noun=a_noun, b_noun=b_noun
    )
    assert not same_community(a, "hobby", b, "hobby", a_noun=a_noun, b_noun=b_noun)


def test_a_different_type_is_never_merged() -> None:
    assert not same_community("gym", "fitness", "church", "faith")


# --- a named venue is never folded into a row pinned somewhere else --------


def _cand(place_name: str) -> CircleCandidate:
    return CircleCandidate(
        circle_type="fitness", circle_key="orangetheory", raw_phrase="x",
        confidence=0.9, place_name=place_name, noun="fitness studio", emoji="",
    )


def test_named_venue_is_not_folded_into_a_place_pinned_elsewhere() -> None:
    assert _names_a_different_place(
        _cand("OrangeTheory Narcoossee"),
        {"place_name": "Crunch Fitness - Lake Nona", "place_ref": "p1"},
    )


def test_the_same_venue_still_corroborates() -> None:
    assert not _names_a_different_place(
        _cand("OrangeTheory Narcoossee"),
        {"place_name": "Orangetheory Fitness Narcoossee", "place_ref": "p1"},
    )


def test_no_venue_named_leaves_the_decision_to_the_nouns() -> None:
    assert not _names_a_different_place(_cand(""), {"place_name": "", "place_ref": "p1"})


# --- a pinned sibling silences the duplicate's question -------------------


def test_grounding_is_skipped_when_a_sibling_is_pinned() -> None:
    """The exact prod case: don't ask which spot the gym is when the gym is pinned."""
    world = {
        "circles": [
            {"key": "fitness_cf", "type": "fitness", "grounded": True, "confirmed": True,
             "place": "Fitness CF - St. Cloud", "noun": "gym"},
            {"key": "fitness_cf_st_cloud", "type": "fitness", "grounded": False},
            {"key": "gym", "type": "fitness", "grounded": False, "noun": "gym"},
        ]
    }
    ids = {g["id"] for g in _grounding_goals(world)}
    assert ids == set(), f"asked about an already-pinned gym: {ids}"


def test_an_unrelated_ungrounded_circle_still_asks() -> None:
    world = {
        "circles": [
            {"key": "fitness_cf", "type": "fitness", "grounded": True, "confirmed": True},
            {"key": "lagoinha_small_group", "type": "faith", "grounded": False},
        ]
    }
    assert {g["id"] for g in _grounding_goals(world)} == {"circle:lagoinha_small_group"}


# --- the relevance gate ---------------------------------------------------


def _place_goal(gid: str, kind: str, **ctx) -> dict:
    return {"id": gid, "kind": kind, "summary": "s", "value_hint": 0.6, "context": ctx}


MIZU = _place_goal("circle_offer:restaurant", "circle_offer",
                   circle_key="restaurant", place_name="Mizu Sushi & Steakhouse")
GYM = _place_goal("circle:crossfit_st_cloud", "ungrounded_circle",
                  circle_key="crossfit_st_cloud")
CAP = {"id": "cap:looking.tip", "kind": "capability", "summary": "s",
       "value_hint": 0.6, "context": {"capability_id": "looking.tip"}}


def test_the_venue_is_matched_not_its_category() -> None:
    """The subtlety that makes or breaks this: "restaurant" IS related to "Piazza
    Italia", so matching the category would wave Mizu Sushi straight through."""
    assert not goal_matches_message(MIZU, "Piazza Italia or Mangia")
    assert goal_matches_message(MIZU, "heading to Mizu tonight")


def test_both_reported_bugs_are_withheld() -> None:
    out = filter_place_goals(
        [MIZU, GYM, CAP], message="Piazza Italia or Mangia", turn_has_topic=True
    )
    assert out == [CAP], "a stale venue/gym reached the policy on a restaurant turn"


def test_a_relevant_goal_survives() -> None:
    out = filter_place_goals(
        [MIZU, GYM, CAP], message="I hit my crossfit box on Tuesdays", turn_has_topic=True
    )
    assert [g["id"] for g in out] == ["circle:crossfit_st_cloud", "cap:looking.tip"]


def test_an_empty_turn_keeps_them_all() -> None:
    """"hey" / "not much" — nothing to interrupt, and a specific question beats
    "how can I help you today?"."""
    out = filter_place_goals([MIZU, GYM, CAP], message="hey", turn_has_topic=False)
    assert len(out) == 3


def test_capabilities_are_never_gated() -> None:
    out = filter_place_goals([CAP], message="Piazza Italia", turn_has_topic=True)
    assert out == [CAP]


def test_no_place_goals_is_a_no_op() -> None:
    assert filter_place_goals([CAP], message="anything", turn_has_topic=True) == [CAP]


# --- the topic signal comes from the classifier, not a word list ----------


def test_topic_is_read_off_the_classifier_verdict() -> None:
    from app.policy.decide import turn_has_topic as has_topic

    assert has_topic({"_discovery_slots": {"linear_intent": "looking.tip"}}, "x")
    assert not has_topic({"_discovery_slots": {"linear_intent": "none"}}, "hey")
    assert not has_topic({"_discovery_slots": {"linear_intent": ""}}, "ok")


def test_missing_verdict_assumes_they_said_something() -> None:
    """Cautious side: a withheld goal costs a beat, a mistimed one costs trust."""
    from app.policy.decide import turn_has_topic as has_topic

    assert has_topic({}, "Piazza Italia or Mangia")
    assert not has_topic({}, "   ")


def test_no_message_gates_nothing() -> None:
    """Fail open on absent information: a caller that passes no message must not
    silently lose every place goal (it broke test_deferred_goals_marked)."""
    out = filter_place_goals([MIZU, GYM, CAP], message="", turn_has_topic=True)
    assert len(out) == 3
