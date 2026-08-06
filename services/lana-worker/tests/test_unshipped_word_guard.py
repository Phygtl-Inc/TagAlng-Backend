"""The word "swap" never reaches a user while swapping is unbuilt.

Suppressing the swap CAPABILITY was not enough. With it switched off, Lana still
wrote "if you ever want to swap favorite spots" — she meant trading
recommendations, and a tester read it as the unbuilt item-swap feature. Intent is
invisible to the reader, so the word itself is banned on output.

Delete these tests together with _UNSHIPPED_FEATURE_RE the day swap ships.
"""

from __future__ import annotations

import pytest

from app.lingo_guard import find_violations, naive_clean


@pytest.mark.parametrize(
    "text",
    [
        # The exact sentence a tester flagged.
        "If you ever want to swap favorite spots or try a new place, I'm here.",
        "I can help you swap or pass along kids gear with other families nearby.",
        "Want to see who's swapping nearby?",
        "She swapped a stroller last week.",
        "Plenty of hand-me-downs around here.",
        "Any hand me downs you're done with?",
    ],
)
def test_swap_wording_is_caught(text: str) -> None:
    assert find_violations(text), f"unshipped-feature wording shipped: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Want me to find pizza spots near you, or ask neighbors which they swear by?",
        "I can help you share a tip with neighbors.",
        "Want a hand setting up a get-together at Fitness CF - St. Cloud?",
        "Great choice! Mortadella pizza always hits the spot.",
    ],
)
def test_clean_copy_is_untouched(text: str) -> None:
    """Clean turns must pay nothing — no rewrite, no false positive."""
    assert find_violations(text) == []
    assert naive_clean(text) == text


def test_naive_fallback_is_clean_and_reads_naturally() -> None:
    """The fallback runs when the LLM rewrite is down, so its output must both be
    clean AND still be a sentence."""
    out = naive_clean("If you ever want to swap favorite spots, I'm here.")
    assert find_violations(out) == []
    assert out == "If you ever want to share favorite spots, I'm here."


def test_naive_fallback_output_is_always_clean() -> None:
    for text in ("swap", "Swapping gear?", "hand-me-downs", "SWAPPED"):
        assert find_violations(naive_clean(text)) == [], text


def test_the_word_ban_does_not_fix_meaning() -> None:
    """Honest limitation: the filter launders the WORD, not the offer. "pass
    along kids gear" survives because only the banned token is substituted —
    which is why the policy prompt must refuse the offer upstream, and this guard
    is the second line, not the first."""
    out = naive_clean("I can help you swap or pass along kids gear.")
    assert find_violations(out) == []
    assert "pass along kids gear" in out
