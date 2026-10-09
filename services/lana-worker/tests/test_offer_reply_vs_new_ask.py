"""An armed "ask your neighbours too?" offer must not swallow the user's next ASK.

Prod 2026-10-07 (Pouya, SJSU): right after Lana answered a recommendation ask and offered to
ask others, "Has any users recommended a beginner friendly project program…?" was read as a
reply to that offer (goal=continue, find_peers) and became an intro to a stranger.
"""

from app.discovery_slots import _active_capture_context


def _line() -> str:
    return _active_capture_context(
        {"tip_ask_offer_pending": {"detail": "beginner friendly project program"}}
    )


def test_a_message_that_asks_is_a_new_request_not_an_offer_reply():
    line = _line()
    assert line.startswith("offer_reply")
    assert "is a NEW request even when it repeats or refines" in line
    assert "looking.tip with goal=save_signal, never goal=continue and never a people search" in line
    # The old framing that made every next message an answer to the offer is gone.
    assert "most likely their ANSWER" not in line


def test_an_answer_to_the_offer_still_never_becomes_a_share():
    line = _line()
    assert "only when all it does is respond to it" in line
    assert "NEVER tip_share / sharing.tip" in line
