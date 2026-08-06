"""Grounding confirm = ACKNOWLEDGE → ONE OFFER (rapport-bridge shape).

Covers the LANA_RAPPORT_BRIDGE_SPEC_v1 rules on the circles grounding confirm:
AC-1 never a bare acknowledgement (offer by default), AC-5 intro only on a real
co-member count, rule 5 create+invite as the always-on default, the
once-per-session annoyance guard, and the pipeline arming the offer rails.
"""

import unittest
from unittest.mock import patch

from app.circles_flow import ground_and_confirm
from app.lana_unified_pipeline import _grounding_turn_result


class _Timer:
    def to_dict(self):
        return {}


_GROUND_RESULT = {
    "affiliation_id": "aff1",
    "place_id": "place-uuid-1",
    "place_name": "PingPod",
    "status": "confirmed",
}
_AFFILIATION = {"id": "aff1", "circle_key": "table_tennis", "circle_type": "hobby"}


def _fallback_compose(goal, facts, fallback, session_ctx):
    _ = goal, facts, session_ctx
    return fallback


@patch("app.circles_flow._compose_grounding_reply", side_effect=_fallback_compose)
@patch("app.circles_flow.ground_affiliation", return_value=dict(_GROUND_RESULT))
@patch("app.circles_flow._own_affiliation", return_value=dict(_AFFILIATION))
class TestGroundAndConfirmBridge(unittest.TestCase):
    def test_no_comembers_offers_create(self, *_mocks) -> None:
        with patch("app.circles_flow._place_co_member_count", return_value=0):
            ctx: dict = {}
            result = ground_and_confirm("u1", "aff1", "gpid", session_ctx=ctx)
        self.assertTrue(result["grounded"])
        offer = result["offer"]
        self.assertEqual(offer["kind"], "host_meet")
        self.assertIn("table tennis", offer["send"])
        self.assertIn("PingPod", offer["send"])
        self.assertIn("want to set up", result["reply"].lower())
        self.assertTrue(ctx["_grounding_offer_done"])

    def test_comembers_offer_intro(self, *_mocks) -> None:
        with patch("app.circles_flow._place_co_member_count", return_value=2):
            result = ground_and_confirm("u1", "aff1", "gpid", session_ctx={})
        offer = result["offer"]
        self.assertEqual(offer["kind"], "find_neighbors")
        self.assertIn("2 of your neighbors", result["reply"])
        self.assertIn("want an intro", result["reply"].lower())

    def test_offer_once_per_session(self, *_mocks) -> None:
        with patch("app.circles_flow._place_co_member_count", return_value=0):
            ctx = {"_grounding_offer_done": True}
            result = ground_and_confirm("u1", "aff1", "gpid", session_ctx=ctx)
        self.assertIsNone(result["offer"])
        # Announce decision 2026-07-28: even the plain close says the save happened.
        self.assertIn("saved to your communities", result["reply"])

    def test_tile_endpoint_path_has_no_offer(self, *_mocks) -> None:
        with patch("app.circles_flow._place_co_member_count", return_value=3):
            result = ground_and_confirm("u1", "aff1", "gpid", session_ctx=None)
        self.assertIsNone(result["offer"])
        self.assertTrue(result["grounded"])

    def test_reply_is_lexicon_clean(self, *_mocks) -> None:
        from app.lingo_guard import find_violations

        for count in (0, 2):
            with patch("app.circles_flow._place_co_member_count", return_value=count):
                result = ground_and_confirm("u1", "aff1", "gpid", session_ctx={})
            self.assertEqual(find_violations(result["reply"]), [])
            self.assertEqual(find_violations(result["offer"]["label"]), [])
            self.assertEqual(find_violations(result["offer"]["send"]), [])


class TestGroundingTurnResultArmsOffer(unittest.TestCase):
    def test_offer_arms_rapport_rails(self) -> None:
        result = {
            "reply": "Locked in — PingPod. Want to set up a table tennis get-together?",
            "options": [],
            "pending": None,
            "grounded": True,
            "offer": {
                "kind": "host_meet",
                "label": "Set something up",
                "send": "help me host a table tennis meet at PingPod",
                "topic": "table tennis",
            },
        }
        reply, status, ctx, _ui, _draft = _grounding_turn_result({}, result, _Timer())
        self.assertEqual(status, "continue")
        self.assertIn("PingPod", reply)
        self.assertTrue(ctx["rapport_active"])
        self.assertTrue(ctx["rapport_offer_pending"])
        self.assertEqual(ctx["rapport_pending_action"]["kind"], "host_meet")
        self.assertEqual(
            ctx["rapport_reply"]["action"]["label"], "Set something up"
        )
        self.assertIsNone(ctx["rapport_grounding"])

    def test_unpinned_close_still_arms_the_find_people_offer(self) -> None:
        # The place could not be pinned, but the community is known — the offer to
        # LOOK for neighbors rides the same rails a pinned close uses (2026-08-03).
        result = {
            "reply": "I'll remember it the way you said it. Want me to look for "
                     "neighbors into table tennis too?",
            "options": [],
            "pending": None,
            "grounded": False,
            "offer": {
                "kind": "find_neighbors",
                "label": "Yes, look",
                "send": "connect me with neighbors into table tennis",
                "topic": "table tennis",
            },
        }
        _reply, _status, ctx, _ui, _draft = _grounding_turn_result({}, result, _Timer())
        self.assertTrue(ctx["rapport_offer_pending"])
        self.assertEqual(ctx["rapport_pending_action"]["kind"], "find_neighbors")
        self.assertIsNone(ctx["rapport_grounding"])

    def test_no_offer_resets_state(self) -> None:
        result = {"reply": "Locked in.", "options": [], "pending": None,
                  "grounded": True, "offer": None}
        _reply, _status, ctx, _ui, _draft = _grounding_turn_result({}, result, _Timer())
        self.assertIsNone(ctx["rapport_active"])
        self.assertIsNone(ctx["rapport_offer_pending"])

    def test_pending_chips_still_arm_grounding(self) -> None:
        result = {
            "reply": "Which spot — PingPod on 27th, or somewhere else?",
            "options": [{"label": "PingPod", "send": "PingPod"}],
            "pending": {"affiliation_id": "aff1", "candidates": [], "attempts": 1},
            "grounded": False,
        }
        _reply, _status, ctx, _ui, _draft = _grounding_turn_result({}, result, _Timer())
        self.assertTrue(ctx["rapport_active"])
        self.assertIsInstance(ctx["rapport_grounding"], dict)
        self.assertFalse(ctx["rapport_offer_pending"])


if __name__ == "__main__":
    unittest.main()
