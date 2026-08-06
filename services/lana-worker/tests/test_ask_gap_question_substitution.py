"""The stored question actually reaches the person.

get_gap_row's select list omitted `question` for as long as
_wire_ask_gap_action has depended on it. That function treats a row with no
question as "nothing vetted this", so EVERY ask_gap silently downgraded to
`reply`: the stored wording was never substituted and mark_chat_asked (which sits
after the early return) never ran. Because the model writes a LEAD-IN expecting
the question to be appended, the person received a fragment —
"Italian pizza's a good one —" and nothing else (dev 2026-08-06, gap 857a641f,
whose question "Any favorite local spots for Italian pizza?" was open the whole
time).
"""

from __future__ import annotations

import pytest

from app import rapport_gaps
from app.lana_unified_pipeline import _close_dangling_lead_in


def test_get_gap_row_asks_for_the_question() -> None:
    """A regression guard on the select list itself: without `question` the whole
    vetted-question mechanism is dead code."""
    import inspect

    src = inspect.getsource(rapport_gaps.get_gap_row)
    assert "question" in src.split(".select(")[1].split(")")[0], (
        "get_gap_row must SELECT question — _wire_ask_gap_action reads it, and "
        "treats its absence as 'unvetted', downgrading every ask_gap to reply"
    )


# --- a lead-in never ships unfinished ------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Italian pizza's a good one —", "Italian pizza's a good one."),
        ("Pizza is always a win -", "Pizza is always a win."),
        ("Nice, exploring is half the fun:", "Nice, exploring is half the fun."),
        ("Got it,", "Got it."),
        ("Sounds lovely —  ", "Sounds lovely."),
    ],
)
def test_dangling_connectors_are_closed(given: str, expected: str) -> None:
    assert _close_dangling_lead_in(given) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Pizza is always a win.",
        "That sounds great!",
        "Which spot do you go to?",
        "Hmm…",
    ],
)
def test_complete_sentences_are_untouched(text: str) -> None:
    assert _close_dangling_lead_in(text) == text


def test_empty_stays_empty() -> None:
    assert _close_dangling_lead_in("") == ""
    assert _close_dangling_lead_in("   ") == ""
    assert _close_dangling_lead_in(None) == ""


def test_a_hyphenated_word_is_not_treated_as_a_connector() -> None:
    """Only a TRAILING connector is stripped — never punctuation inside the line."""
    assert _close_dangling_lead_in("Your well-being matters.") == "Your well-being matters."
    assert _close_dangling_lead_in("A well-known spot") == "A well-known spot."


# --- the acknowledgement survives the substitution ------------------------

from app.lana_unified_pipeline import _merge_vetted_question  # noqa: E402

VETTED = "Any favorite local spots for Italian pizza?"


def test_lead_in_joined_by_a_dash_is_kept() -> None:
    """The shipped bug: a lead-in joined to the question by a connector was
    discarded with it, so the person got a bare question and no acknowledgement
    of what they had just said (2026-08-06)."""
    out = _merge_vetted_question("Italian pizza's a good one — any favorite spots?", VETTED)
    assert out == f"Italian pizza's a good one. {VETTED}"


def test_the_last_connector_wins_so_a_name_survives() -> None:
    out = _merge_vetted_question("Got it, Asjid — any favourite spots?", VETTED)
    assert out == f"Got it, Asjid. {VETTED}"


def test_a_full_sentence_lead_in_still_works() -> None:
    out = _merge_vetted_question(
        "Pizza is always a win! Do you have a favourite spot?", VETTED
    )
    assert out == f"Pizza is always a win! {VETTED}"


def test_a_bare_question_yields_just_the_vetted_question() -> None:
    """Nothing to acknowledge — don't invent a lead-in."""
    assert _merge_vetted_question("Any favourite spots?", VETTED) == VETTED


def test_an_utterance_already_asking_it_is_untouched() -> None:
    said = f"Nice — {VETTED}"
    assert _merge_vetted_question(said, VETTED) == said


def test_empty_utterance_yields_the_question() -> None:
    assert _merge_vetted_question("", VETTED) == VETTED
    assert _merge_vetted_question(None, VETTED) == VETTED


def test_a_statement_only_lead_in_is_kept_whole() -> None:
    """No question mark at all — nothing to strip, so keep it and append."""
    out = _merge_vetted_question("Italian pizza is a great shout.", VETTED)
    assert out == f"Italian pizza is a great shout. {VETTED}"
