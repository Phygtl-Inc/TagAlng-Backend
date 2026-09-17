"""Lana never promises a text message, because there is no SMS in this app.

`notifications.notify_user` is web push + Resend email. Nothing anywhere can send an SMS,
yet "I'll text you the moment a neighbor recommends one" shipped in the tip-ask receipt
goal, in its hardcoded fallback, and in messages/{en,es,pt-BR}.json — so a user waited on
a message that could not arrive. Same class as the unshipped-feature guard next door, and
worse in effect: an unshipped word confuses, an unshipped CHANNEL makes someone wait.

Delete these tests together with _FALSE_CHANNEL_RE the day SMS ships.
"""

from __future__ import annotations

import pytest

from app.lingo_guard import find_violations, naive_clean


@pytest.mark.parametrize(
    "text",
    [
        # The exact line from the screenshot that started this.
        "I asked your neighbors nearby if they know any good coffee shop for you, and "
        "I'll text you as soon as someone recommends one.",
        # The receipt fallback it came from.
        "I'll text you the moment someone recommends one.",
        "I'll text you when a neighbor shares a tip that fits.",
        "I'm texting you as soon as I hear back.",
        "I'll send you a text when someone answers.",
        "You'll hear from me by SMS.",
    ],
)
def test_promises_of_a_text_are_caught(text: str) -> None:
    assert find_violations(text), f"false-channel promise shipped: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "I'll email you the moment someone recommends one.",
        # The noun stays legal — only the verb sense makes a promise.
        "I kept the text you wrote, word for word.",
        "That text you sent me had the address in it.",
        "I'll let you know as soon as a neighbor recommends one.",
    ],
)
def test_clean_copy_is_untouched(text: str) -> None:
    assert find_violations(text) == []
    assert naive_clean(text) == text


def test_naive_fallback_names_the_channel_we_actually_have() -> None:
    """The floor for when the LLM rewrite is down: still clean, still a sentence."""
    out = naive_clean("I'll text you the moment someone recommends one.")
    assert find_violations(out) == []
    assert out == "I'll email you the moment someone recommends one."


def test_naive_fallback_output_is_always_clean() -> None:
    for text in (
        "I'll text you.",
        "texting you now",
        "I'll send you a text",
        "delivered via SMS",
    ):
        assert find_violations(naive_clean(text)) == [], text
