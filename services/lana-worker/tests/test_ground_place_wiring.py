"""Policy `ground_place` decisions must be backed by the REAL grounding rails.

QA 2026-07-30 (the squash case): the policy asked "which court?" with chips it
invented; the tap landed as plain text, nothing was armed, and the next turn
dead-ended. _wire_ground_place_action resolves the affiliation, runs the same
Places search the tile uses, replaces the invented chips with real candidates,
and arms rapport_grounding so the answer turn flows into
handle_grounding_confirmation → ground_and_confirm → the bridge offer.

Also covers the look_meet relevance floor (bridge spec §2): a seeker who named
what kind of meet they want never gets an unrelated event back — an off-topic
result would block the create+invite fall-through.
"""

import unittest
from unittest.mock import patch

from app.lana_unified_pipeline import _wire_ground_place_action
from app.look_meet import _find_block_events


class _Action:
    def __init__(self, kind="ground_place", goal_id="circle:squash", utterance="Which court?"):
        self.kind = kind
        self.goal_id = goal_id
        self.utterance = utterance
        self.chips = [{"label": "It's the main rec center", "send": "It's the main rec center"}]


_AFF_ROW = {
    "id": "aff1",
    "circle_type": "hobby",
    "circle_key": "squash",
    "detail": "squash on Saturdays at the community court",
    "status": "suggested",
    "place_ref": None,
}


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        def _chain(*_a, **_k):
            return self

        return _chain

    def execute(self):
        class _Res:
            data = self._rows

        return _Res()


class _SB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _Query(self._rows)


_PLACES = [
    {"name": "Lake Nona Rec Center", "address": "1 Main St", "google_place_id": "gp1"},
    {"name": "Narcoossee Courts", "address": "2 Elm St", "google_place_id": "gp2"},
]


class TestWireGroundPlace(unittest.TestCase):
    def test_wires_real_chips_and_arms_grounding(self) -> None:
        action = _Action()
        ctx: dict = {}
        with patch("app.auth.service_client", return_value=_SB([dict(_AFF_ROW)])), \
             patch("app.circles_flow.ground_options", return_value=list(_PLACES)), \
             patch("app.circles_flow._home_block_id", return_value="blk1"):
            _wire_ground_place_action(action, user_id="u1", session_ctx=ctx)
        # The invented chip is gone; real candidates render instead.
        labels = [c["label"] for c in action.chips]
        self.assertIn("Lake Nona Rec Center", labels)
        self.assertNotIn("It's the main rec center", labels)
        # The grounding rails are armed for the answer turn.
        self.assertTrue(ctx["rapport_active"])
        grounding = ctx["rapport_grounding"]
        self.assertEqual(grounding["affiliation_id"], "aff1")
        self.assertEqual(
            [c["google_place_id"] for c in grounding["candidates"]], ["gp1", "gp2"]
        )
        self.assertFalse(ctx["rapport_offer_pending"])
        # A way out of a wrong list, offered as a chip but never as a candidate.
        from app.circles_flow import _ESCAPE_SEND

        self.assertEqual(action.chips[-1]["send"], _ESCAPE_SEND)
        self.assertNotIn(
            _ESCAPE_SEND, [c["send"] for c in grounding["candidates"]]
        )

    def test_no_affiliation_strips_fiction_chips_only(self) -> None:
        action = _Action()
        ctx: dict = {}
        with patch("app.auth.service_client", return_value=_SB([])):
            _wire_ground_place_action(action, user_id="u1", session_ctx=ctx)
        self.assertEqual(action.chips, [])  # never ship invented places
        self.assertNotIn("rapport_grounding", ctx)  # nothing armed — free-text ask

    def test_empty_places_still_arms_pending_state(self) -> None:
        # Zero candidates → the user's ANSWER drives the search next turn via
        # handle_grounding_confirmation's re-search path.
        action = _Action()
        ctx: dict = {}
        with patch("app.auth.service_client", return_value=_SB([dict(_AFF_ROW)])), \
             patch("app.circles_flow.ground_options", return_value=[]), \
             patch("app.circles_flow._home_block_id", return_value="blk1"):
            _wire_ground_place_action(action, user_id="u1", session_ctx=ctx)
        self.assertEqual(action.chips, [])
        self.assertTrue(ctx["rapport_active"])
        self.assertEqual(ctx["rapport_grounding"]["candidates"], [])

    def test_non_ground_place_untouched(self) -> None:
        action = _Action(kind="reply")
        ctx: dict = {}
        _wire_ground_place_action(action, user_id="u1", session_ctx=ctx)
        self.assertEqual(len(action.chips), 1)
        self.assertEqual(ctx, {})


_EVENTS = [
    {"id": "e1", "title": "Coffee & Catch-up", "cohort_tags": ["social"],
     "starts_at": "2026-08-01T09:00:00Z", "venue_name": "KFC"},
    {"id": "e2", "title": "Morning Run Club", "cohort_tags": ["running", "fitness"],
     "starts_at": "2026-08-02T07:00:00Z", "venue_name": "Park"},
]


class TestLookMeetRelevanceFloor(unittest.TestCase):
    def _events(self, kind):
        with patch("app.look_meet._jwt_sub", return_value=None), \
             patch("app.supabase_rpc.call_rpc", return_value=list(_EVENTS)):
            return _find_block_events(
                user_jwt="jwt", kind=kind, zip_code="32827", block_id=None
            )

    def test_named_kind_drops_offtopic_events(self) -> None:
        out = self._events("running")
        self.assertEqual([e["title"] for e in out], ["Morning Run Club"])

    def test_nothing_on_topic_returns_empty(self) -> None:
        out = self._events("badminton")
        self.assertEqual(out, [])  # → the seek/create path owns the turn

    def test_open_kind_keeps_everything(self) -> None:
        out = self._events(None)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main()
