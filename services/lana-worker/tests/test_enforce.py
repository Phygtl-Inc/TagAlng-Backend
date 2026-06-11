import unittest

from app.orchestrator.enforce import enforce_routing, is_affirmative, should_execute_tool
from app.orchestrator.slots import event_missing_slots, is_placeholder, validate_tool_slots


class EnforceRoutingTests(unittest.TestCase):
    def _base(self, **overrides):
        routing = {
            "outcome": "T",
            "intent_class": "activity",
            "confidence": 0.9,
            "tool_to_call": "publish_activity",
            "tool_args": {"title": "Coffee"},
            "missing_slots": [],
            "sentiment": "neutral",
            "needs_confirmation": False,
            "thinking": "",
        }
        routing.update(overrides)
        return routing

    def test_low_confidence_forces_respond(self):
        out = enforce_routing(
            self._base(confidence=0.3),
            purpose="event_draft",
            utterance="maybe coffee",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "R")
        self.assertIsNone(out["tool_to_call"])

    def test_medium_confidence_forces_ask(self):
        out = enforce_routing(
            self._base(confidence=0.7),
            purpose="event_draft",
            utterance="host coffee",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "A")
        self.assertIsNone(out["tool_to_call"])

    def test_missing_publish_slots_downgrades_to_draft(self):
        out = enforce_routing(
            self._base(tool_args={"title": "Coffee"}),
            purpose="event_draft",
            utterance="coffee meetup",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "T")
        self.assertEqual(out["tool_to_call"], "update_event_draft")
        self.assertIn("starts_at", out["missing_slots"])

    def test_partial_slots_downgrade_to_draft_update(self):
        out = enforce_routing(
            self._base(
                tool_args={"title": "Coffee", "starts_at": "2026-06-07T10:00:00Z"},
            ),
            purpose="event_draft",
            utterance="coffee Saturday 10am",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "T")
        self.assertEqual(out["tool_to_call"], "update_event_draft")

    def test_off_topic_forces_capture(self):
        out = enforce_routing(
            self._base(intent_class="off_topic", outcome="C", tool_to_call=None),
            purpose="profile_intake",
            utterance="need a nail tech",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "C")
        self.assertEqual(out["tool_to_call"], "capture_inquiry")
        self.assertTrue(should_execute_tool(out))

    def test_pending_confirmation_yes_publishes(self):
        out = enforce_routing(
            self._base(),
            purpose="event_draft",
            utterance="yes publish",
            session_ctx={
                "pending_confirmation": "Got it: Coffee · Sat · home. Publish?",
                "event_draft": {
                    "title": "Coffee",
                    "starts_at": "2026-06-07T10:00:00Z",
                    "venue_name": "home",
                },
            },
        )
        self.assertEqual(out["outcome"], "T")
        self.assertEqual(out["tool_to_call"], "publish_activity")
        self.assertTrue(out["tool_args"]["user_confirmed"])

    def test_frustrated_lowers_respond_threshold(self):
        out = enforce_routing(
            self._base(confidence=0.45, sentiment="frustrated", tool_to_call=None),
            purpose="event_draft",
            utterance="ugh nothing works",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "A")

    def test_placeholder_detection(self):
        self.assertTrue(is_placeholder("TBD"))
        self.assertTrue(is_placeholder(""))
        missing = event_missing_slots({"title": "TBD", "starts_at": "x", "venue_name": "y"})
        self.assertEqual(missing, ["title"])

    def test_update_draft_requires_partial_detail(self):
        missing = validate_tool_slots(
            "update_event_draft",
            {},
            purpose="event_draft",
            session_ctx={},
        )
        self.assertEqual(missing, ["event_detail"])

    def test_affirmative_detection(self):
        self.assertTrue(is_affirmative("yes"))
        self.assertTrue(is_affirmative("Yes, publish"))
        self.assertFalse(is_affirmative("maybe"))

    def test_lana_discovery_forces_zip_ask(self):
        out = enforce_routing(
            self._base(
                intent_class="discovery",
                outcome="R",
                confidence=0.95,
                tool_to_call=None,
            ),
            purpose="lana",
            utterance="find similar people near me",
            session_ctx={},
        )
        self.assertEqual(out["outcome"], "A")
        self.assertIn("zip", out["missing_slots"])

    def test_lana_companionship_not_overridden(self):
        out = enforce_routing(
            self._base(
                intent_class="companionship",
                outcome="R",
                confidence=0.95,
                tool_to_call=None,
            ),
            purpose="lana",
            utterance="how are you",
            session_ctx={"routing_phase": "listening"},
        )
        self.assertEqual(out["outcome"], "R")
        self.assertIsNone(out["tool_to_call"])


if __name__ == "__main__":
    unittest.main()
