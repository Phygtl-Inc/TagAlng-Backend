"""A place-grounding gap asked in chat must go out as `ground_place`, not `ask_gap`.

dev QA 2026-08-05: the user said "i play flute regularly" and Lana replied "…which
spot do you play violin at every week?" — the queued violin grounding question,
pasted verbatim by the ask_gap wiring. Two faults, this file covers the second:

  * the policy picked a goal about a different instrument (prompt fix)
  * it was wired as `ask_gap`, which only pastes vetted text. `ground_place` is the
    kind that fetches real map candidates and arms rapport_grounding, and only that
    armed state routes the answer into handle_grounding_confirmation →
    ground_and_confirm. So the ask reached the user with no chips and nothing armed:
    naming the place would have pinned nothing and created no community.

Which kind carries a place ask is structural, so the promotion is deterministic
rather than left to the policy.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch


class _Action:
    def __init__(self, kind="ask_gap", goal_id="gap:row1", utterance="", chips=None):
        self.kind = kind
        self.goal_id = goal_id
        self.utterance = utterance
        self.chips = chips if chips is not None else []


_PLACE_Q = "Which spot do you play violin at every week?"


def _wire(action, row, aff_rows):
    """Run the wiring with the gap row and the affiliation lookup both stubbed."""
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.eq.return_value.is_.return_value.limit.return_value.execute.return_value = MagicMock(  # noqa: E501
        data=aff_rows
    )
    with patch("app.rapport_gaps.get_gap_row", return_value=row), \
         patch("app.rapport_gaps.mark_chat_asked"), \
         patch("app.auth.service_client", return_value=sb):
        from app.lana_unified_pipeline import _wire_ask_gap_action

        _wire_ask_gap_action(action, user_id="u1")


class TestGroundingGapPromotion(unittest.TestCase):
    def test_affiliation_backed_gap_becomes_ground_place(self) -> None:
        action = _Action(utterance="That's great — music brings people together.")
        _wire(
            action,
            {"gap_row_id": "row1", "question": _PLACE_Q, "affiliation_ref": "aff1"},
            [{"circle_key": "violin_group", "place_ref": None}],
        )
        self.assertEqual(action.kind, "ground_place")
        # _wire_ground_place_action resolves the affiliation from this shape.
        self.assertEqual(action.goal_id, "circle:violin_group")
        # The vetted question still rides along.
        self.assertIn(_PLACE_Q, action.utterance)

    def test_ordinary_gap_stays_ask_gap(self) -> None:
        action = _Action(utterance="Good to hear from you.")
        _wire(
            action,
            {"gap_row_id": "row1", "question": "Mornings or weekends for running?"},
            [],
        )
        self.assertEqual(action.kind, "ask_gap")
        self.assertEqual(action.goal_id, "gap:row1")

    def test_already_grounded_affiliation_is_not_promoted(self) -> None:
        # Pinned (or dismissed) since the gap opened: the place-null filter returns
        # nothing, so there is nothing left to ground and the text ask stands.
        action = _Action(utterance="Nice.")
        _wire(
            action,
            {"gap_row_id": "row1", "question": _PLACE_Q, "affiliation_ref": "aff1"},
            [],
        )
        self.assertEqual(action.kind, "ask_gap")

    def test_missing_user_id_leaves_the_ask_alone(self) -> None:
        # The shadow/audit call site passes no user_id; a plain question is a worse
        # turn than a wired one, never a broken one.
        action = _Action(utterance="Nice.")
        row = {"gap_row_id": "row1", "question": _PLACE_Q, "affiliation_ref": "aff1"}
        with patch("app.rapport_gaps.get_gap_row", return_value=row), \
             patch("app.rapport_gaps.mark_chat_asked"), \
             patch("app.auth.service_client") as sb:
            from app.lana_unified_pipeline import _wire_ask_gap_action

            _wire_ask_gap_action(action)
        self.assertEqual(action.kind, "ask_gap")
        sb.assert_not_called()  # no DB read without a user to scope it to

    def test_lookup_failure_leaves_the_ask_alone(self) -> None:
        action = _Action(utterance="Nice.")
        row = {"gap_row_id": "row1", "question": _PLACE_Q, "affiliation_ref": "aff1"}
        with patch("app.rapport_gaps.get_gap_row", return_value=row), \
             patch("app.rapport_gaps.mark_chat_asked"), \
             patch("app.auth.service_client", side_effect=RuntimeError("boom")):
            from app.lana_unified_pipeline import _wire_ask_gap_action

            _wire_ask_gap_action(action, user_id="u1")
        self.assertEqual(action.kind, "ask_gap")


if __name__ == "__main__":
    unittest.main()
