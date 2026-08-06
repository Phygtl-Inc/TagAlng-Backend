"""A queued question asked as a plain `reply` still counts as asked.

Prod 2026-08-05: the policy asked the stored gap "Is there a favorite dish or
something you always order at The Piazza Italia?" in chat at 21:30:42 AND
21:31:05, both times as kind='reply' with goal_id null. mark_chat_asked only ran
for kind='ask_gap', so chat_asked_at stayed NULL, the gap stayed offerable, and
the home tile served the identical question at 21:36:46 — after the user had
already answered it in chat at 21:31:22.
"""

from __future__ import annotations

import pytest

from app import rapport_gaps

GAP_Q = "Is there a favorite dish or something you always order at The Piazza Italia?"
# What the user actually read at 21:31:05.
REPLY_VERBATIM = (
    "You really do love their pizza! Out of curiosity, is there a favorite dish "
    "or something you always order at The Piazza Italia?"
)
REPLY_REWORDED = (
    "Their pizza must be something special. Do you have a dish you always order "
    "at The Piazza Italia?"
)


def _rows(**over):
    row = {
        "gap_row_id": "gap-1",
        "question": GAP_Q,
        "chat_asked_at": None,
        "answered_at": None,
    }
    row.update(over)
    return [row]


@pytest.fixture
def stamped(monkeypatch):
    """Capture mark_chat_asked calls and serve one open gap."""
    calls: list[str] = []
    monkeypatch.setattr(rapport_gaps, "mark_chat_asked", lambda gid: calls.append(gid))
    return calls


def _serve(monkeypatch, rows):
    class _Q:
        def select(self, *_a): return self
        def eq(self, *_a): return self
        def limit(self, *_a): return self
        def execute(self): return type("_R", (), {"data": rows})()

    monkeypatch.setattr(
        rapport_gaps, "service_client",
        lambda: type("_C", (), {"table": lambda _s, _n: _Q()})(),
    )


def test_verbatim_reuse_in_a_reply_is_stamped(monkeypatch, stamped) -> None:
    _serve(monkeypatch, _rows())
    assert rapport_gaps.mark_chat_asked_if_reused("u1", REPLY_VERBATIM) == "gap-1"
    assert stamped == ["gap-1"]


def test_reworded_reuse_is_stamped(monkeypatch, stamped) -> None:
    """QA note in mark_chat_asked: rewording is how the loop guard was evaded."""
    _serve(monkeypatch, _rows())
    assert rapport_gaps.mark_chat_asked_if_reused("u1", REPLY_REWORDED) == "gap-1"
    assert stamped == ["gap-1"]


def test_unrelated_reply_is_not_stamped(monkeypatch, stamped) -> None:
    _serve(monkeypatch, _rows())
    out = rapport_gaps.mark_chat_asked_if_reused(
        "u1", "All good — if you ever want to set something up nearby, just say the word."
    )
    assert out is None and stamped == []


def test_a_different_question_is_not_stamped(monkeypatch, stamped) -> None:
    """Asking about the gym must not tick off the Piazza Italia gap."""
    _serve(monkeypatch, _rows())
    out = rapport_gaps.mark_chat_asked_if_reused(
        "u1", "Which gym spot do you usually go to around here?"
    )
    assert out is None and stamped == []


def test_already_stamped_or_answered_gaps_are_skipped(monkeypatch, stamped) -> None:
    _serve(monkeypatch, _rows(chat_asked_at="2026-08-05T21:30:00Z"))
    assert rapport_gaps.mark_chat_asked_if_reused("u1", REPLY_VERBATIM) is None
    _serve(monkeypatch, _rows(answered_at="2026-08-05T21:31:22Z"))
    assert rapport_gaps.mark_chat_asked_if_reused("u1", REPLY_VERBATIM) is None
    assert stamped == []


def test_lead_in_alone_does_not_match(monkeypatch, stamped) -> None:
    """The warm-up sentence mentions the venue but asks nothing — a comment about
    the place is not the stored question."""
    _serve(monkeypatch, _rows())
    out = rapport_gaps.mark_chat_asked_if_reused(
        "u1", "The Piazza Italia sounds like a great spot."
    )
    assert out is None and stamped == []


def test_no_user_or_empty_utterance(monkeypatch, stamped) -> None:
    _serve(monkeypatch, _rows())
    assert rapport_gaps.mark_chat_asked_if_reused("", REPLY_VERBATIM) is None
    assert rapport_gaps.mark_chat_asked_if_reused("u1", "   ") is None
    assert stamped == []
