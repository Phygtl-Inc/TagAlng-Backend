"""Chat may not invent personal questions — it asks the vetted one or none.

QA 2026-08-03: "is there a favorite blue thing or place that cheers you up?",
asked three turns running while the user was explaining that their stomach hurt
and they hadn't slept. Nobody wrote that question. Two faults produced it:

  * rapport_synth writes questions under a real bar (no yes/no, no opinions, no
    consumer/brand preferences, and each must change who the person connects
    with). The home tile serves those strings verbatim — but the chat path got
    the question only as a topic hint and wrote its own sentence, so the bar
    never applied to what the user actually read.
  * nothing marked the gap as asked, so it stayed 'open' and was re-offered as a
    candidate goal every single turn. The loop guard compares whole replies for
    exact equality, so three differently-worded re-asks were invisible to it.
"""

import unittest
from unittest.mock import patch

from app.lana_unified_pipeline import _merge_vetted_question, _wire_ask_gap_action
from app.policy.goals import _asked_in_chat_recently


class _Action:
    def __init__(self, kind="ask_gap", goal_id="gap:row1", utterance="", chips=None):
        self.kind = kind
        self.goal_id = goal_id
        self.utterance = utterance
        self.chips = chips if chips is not None else []


_VETTED = "What's a spot nearby you love for a run?"


def _wire(action, row):
    with patch("app.rapport_gaps.get_gap_row", return_value=row) as get_row, \
         patch("app.rapport_gaps.mark_chat_asked") as marked:
        _wire_ask_gap_action(action)
    return get_row, marked


class TestVettedQuestionWins(unittest.TestCase):
    def test_invented_question_is_replaced_lead_in_kept(self) -> None:
        action = _Action(
            utterance="That sounds like a rough day. Is there a favorite blue "
                      "thing that lifts your mood?",
        )
        _, marked = _wire(action, {"gap_row_id": "row1", "question": _VETTED})
        self.assertEqual(action.kind, "ask_gap")
        self.assertNotIn("blue", action.utterance)
        self.assertIn(_VETTED, action.utterance)
        self.assertIn("rough day", action.utterance, "the warm lead-in is worth keeping")
        marked.assert_called_once_with("row1")

    def test_gap_is_stamped_so_it_cannot_repeat_next_turn(self) -> None:
        _, marked = _wire(_Action(utterance="Hi. Which nights suit you?"),
                          {"gap_row_id": "row1", "question": _VETTED})
        marked.assert_called_once_with("row1")

    def test_unvetted_ask_becomes_a_reply(self) -> None:
        """No resolvable open gap means no question passed any bar."""
        action = _Action(goal_id=None, utterance="Out of curiosity, what's your favorite colour?")
        _wire(action, None)
        self.assertEqual(action.kind, "reply")
        self.assertIsNone(action.goal_id)

    def test_row_without_a_question_is_not_vetted_either(self) -> None:
        action = _Action(utterance="What's your favorite colour?")
        _wire(action, {"gap_row_id": "row1", "question": ""})
        self.assertEqual(action.kind, "reply")

    def test_other_kinds_are_untouched(self) -> None:
        for kind in ("reply", "follow_thread", "bridge_offer", "ground_place", "capture_defer"):
            action = _Action(kind=kind, utterance="Anything at all?")
            _wire_ask_gap_action(action)  # no patches: must not hit the DB at all
            self.assertEqual(action.kind, kind)
            self.assertEqual(action.utterance, "Anything at all?")


class TestMergeVettedQuestion(unittest.TestCase):
    def test_keeps_acknowledgement_drops_their_question(self) -> None:
        merged = _merge_vetted_question(
            "Ouch, hope that eases up. Do you have a favourite blue mug?", _VETTED
        )
        self.assertEqual(merged, f"Ouch, hope that eases up. {_VETTED}")

    def test_bare_question_becomes_just_the_vetted_one(self) -> None:
        self.assertEqual(_merge_vetted_question("Favourite colour?", _VETTED), _VETTED)

    def test_empty_utterance_is_the_question(self) -> None:
        self.assertEqual(_merge_vetted_question("", _VETTED), _VETTED)

    def test_already_the_vetted_question_is_left_alone(self) -> None:
        said = f"Nice one. {_VETTED}"
        self.assertEqual(_merge_vetted_question(said, _VETTED), said)

    def test_statement_only_lead_in_is_kept_whole(self) -> None:
        merged = _merge_vetted_question("Good to hear from you.", _VETTED)
        self.assertEqual(merged, f"Good to hear from you. {_VETTED}")


class TestChatAskCooldown(unittest.TestCase):
    def test_never_chat_asked_is_offerable(self) -> None:
        self.assertFalse(_asked_in_chat_recently({}))
        self.assertFalse(_asked_in_chat_recently({"chat_asked_at": None}))

    def test_just_asked_is_held_back(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        self.assertTrue(_asked_in_chat_recently({"chat_asked_at": now}))

    def test_asked_long_ago_may_come_back(self) -> None:
        """Asked-and-ignored is not answered, so it is allowed to return —
        just not on the next turn."""
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        self.assertFalse(_asked_in_chat_recently({"chat_asked_at": old}))

    def test_naive_and_zulu_timestamps_parse(self) -> None:
        from datetime import datetime, timezone

        naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        self.assertTrue(_asked_in_chat_recently({"chat_asked_at": naive}))
        zulu = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.assertTrue(_asked_in_chat_recently({"chat_asked_at": zulu}))

    def test_garbage_timestamp_does_not_block_the_queue(self) -> None:
        self.assertFalse(_asked_in_chat_recently({"chat_asked_at": "not a date"}))


if __name__ == "__main__":
    unittest.main()
