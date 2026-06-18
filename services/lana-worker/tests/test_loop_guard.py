import unittest

from app.loop_guard import (
    discovery_reply_is_stuck,
    reset_sticky_discovery_state,
    trailing_assistant_repeats,
)


def _history(*assistant_replies: str) -> list[dict]:
    """Build an alternating user/assistant history ending in assistant turns."""
    out: list[dict] = []
    for reply in assistant_replies:
        out.append({"role": "user", "content": "..."})
        out.append({"role": "assistant", "content": reply})
    return out


class TestTrailingRepeats(unittest.TestCase):
    def test_counts_trailing_identical(self) -> None:
        hist = _history("A", "I found 5 neighbors", "I found 5 neighbors")
        self.assertEqual(trailing_assistant_repeats(hist, "I found 5 neighbors"), 2)

    def test_zero_when_last_differs(self) -> None:
        hist = _history("I found 5 neighbors", "something else")
        self.assertEqual(trailing_assistant_repeats(hist, "I found 5 neighbors"), 0)

    def test_ignores_whitespace_and_case(self) -> None:
        hist = _history("I  Found 5  Neighbors")
        self.assertEqual(trailing_assistant_repeats(hist, "i found 5 neighbors"), 1)


class TestDiscoveryReplyIsStuck(unittest.TestCase):
    REPLY = "I found 5 neighbors on your block. Want me to introduce you?"

    def test_stuck_after_two_identical(self) -> None:
        hist = _history("hi", self.REPLY, self.REPLY)
        self.assertTrue(
            discovery_reply_is_stuck(hist, self.REPLY, {"routing_phase": "preview"})
        )

    def test_not_stuck_first_repeat(self) -> None:
        hist = _history("hi", self.REPLY)
        self.assertFalse(
            discovery_reply_is_stuck(hist, self.REPLY, {"routing_phase": "preview"})
        )

    def test_protected_auth_phase_never_stuck(self) -> None:
        hist = _history(self.REPLY, self.REPLY)
        self.assertFalse(
            discovery_reply_is_stuck(
                hist, self.REPLY, {"routing_phase": "await_signup_otp"}
            )
        )

    def test_short_reply_never_stuck(self) -> None:
        hist = _history("ok", "ok")
        self.assertFalse(
            discovery_reply_is_stuck(hist, "ok", {"routing_phase": "preview"})
        )

    def test_auth_action_never_stuck(self) -> None:
        hist = _history(self.REPLY, self.REPLY)
        self.assertFalse(
            discovery_reply_is_stuck(
                hist, self.REPLY, {"routing_phase": "preview", "auth_action": {"type": "logout"}}
            )
        )


class TestResetStickyState(unittest.TestCase):
    def test_clears_discovery_state(self) -> None:
        ctx = {
            "peer_matches": [{"peer_user_id": "u1"}],
            "signal_draft": {"phase": "signal_confirm_missing"},
            "active_intent": "discovery.find_peers",
            "routing_phase": "preview",
            "_discovery_slots": {"goal": "peers"},
            "_discovery_slots_for": "find people",
            "phone_verified": True,
        }
        reset_sticky_discovery_state(ctx)
        self.assertEqual(ctx["peer_matches"], [])
        self.assertIsNone(ctx["signal_draft"])
        self.assertIsNone(ctx["active_intent"])
        self.assertEqual(ctx["routing_phase"], "listening")
        self.assertNotIn("_discovery_slots", ctx)
        self.assertNotIn("_discovery_slots_for", ctx)
        # unrelated state preserved
        self.assertTrue(ctx["phone_verified"])


if __name__ == "__main__":
    unittest.main()