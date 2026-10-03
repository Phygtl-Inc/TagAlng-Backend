"""People are peers or others, never "neighbors" (Tommaso, 2026-10-01: "find/replace all
labels 'neighbors' with peers / 'others' depending on the circumstance").

Lingo rule 11. The PLACE stays legal — "your neighborhood", "vecindario", "vizinhança" —
so these tests pin both sides: every people-form is caught, no place-form is.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import i18n
from app.context import lingo_constitution
from app.lingo_guard import enforce, find_violations, naive_clean


@pytest.mark.parametrize(
    "text",
    [
        "I found 3 neighbors near Lake Nona:",
        "A neighbor answered your ask",
        "your neighbour posted a tip",
        "Neighbours who run too",
        "Un vecino respondió a tu pregunta",
        "Tres vecinas cerca",
        "Um vizinho respondeu",
        "Vizinhos por perto",
    ],
)
def test_people_forms_are_caught_and_cleaned(text: str) -> None:
    assert find_violations(text), text
    cleaned = naive_clean(text)
    assert not find_violations(cleaned), cleaned


@pytest.mark.parametrize(
    "text",
    [
        "What's the ZIP code for your neighborhood?",
        "Your neighbourhood just came alive",
        "Show my neighborhood log",
        "warm, neighborly — natural informal register",
        "un vecindario tranquilo",
        "na sua vizinhança",
    ],
)
def test_place_forms_stay_legal(text: str) -> None:
    assert find_violations(text) == []
    assert naive_clean(text) == text


def test_fallback_reads_naturally_and_keeps_case() -> None:
    assert naive_clean("A neighbor answered your ask") == "Someone answered your ask"
    assert naive_clean("I found 3 neighbors nearby") == "I found 3 others nearby"
    assert naive_clean("Neighbors nearby love it") == "Others nearby love it"
    assert naive_clean("Un vecino respondió") == "Alguien respondió"


def test_enforce_never_ships_the_word_when_the_rewrite_is_down() -> None:
    with patch("app.lingo_guard._rewrite_clean", return_value=None):
        out = enforce("I found 2 neighbors near you", ["Yes, ask my neighbors"])
    assert not find_violations(out.text)
    assert not any(find_violations(c) for c in out.chip_labels)
    assert out.naive_fallback and not out.ok


def test_constitution_teaches_the_rule() -> None:
    text = lingo_constitution()
    assert 'Never call people "neighbors"' in text
    assert "peers" in text and "others" in text


def test_no_canned_string_calls_people_neighbors() -> None:
    """Notifications and other catalogue copy never pass through the reply guard."""
    dirty = {
        key: lang
        for key, langs in i18n._STRINGS.items()
        for lang, value in langs.items()
        if find_violations(value) and any(
            w in h for h in find_violations(value) for w in ("neighb", "vecin", "vizinh")
        )
    }
    assert dirty == {}, dirty
