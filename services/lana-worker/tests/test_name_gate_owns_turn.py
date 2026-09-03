"""The display-name gate owns the turn its answer arrives on.

Prod 2026-09-02, signup on ZIP 10451: "What should neighbors call you? First name is
fine." -> "Tex" was classified identity.complete_profile (confidence 1.0) and answered
by the layer-1 router with "Tex, your profile is all set up already" — the name echoed
straight from the message text and never written. The next chat asked for it again.
"""

import unittest
from unittest.mock import patch

from app.discovery_route import PHASE_NEED_DISPLAY_NAME, _try_layer1_intent_turn


def _call(msg: str, phase: str):
    slots = {"linear_intent": "identity.complete_profile", "confidence": 1.0}
    # Any layer-1 answer would prove the router claimed the turn; it must not get there.
    with patch("app.discovery_route.compose_reply", return_value="claimed"):
        return _try_layer1_intent_turn(
            msg=msg,
            slots=slots,
            session_ctx={"routing_phase": phase, "pending_post_verify": True},
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="zip-10451",
            phase=phase,
            user_id="user-1",
        )


class TestNameGateOwnsTurn(unittest.TestCase):
    def test_bare_name_at_the_gate_is_left_to_the_gate(self):
        self.assertIsNone(_call("Tex", PHASE_NEED_DISPLAY_NAME))

    def test_stated_name_at_the_gate_is_left_to_the_gate(self):
        self.assertIsNone(_call("my name is Tex", PHASE_NEED_DISPLAY_NAME))

    def test_non_name_at_the_gate_still_routes(self):
        self.assertIsNotNone(_call("is my profile done yet?", PHASE_NEED_DISPLAY_NAME))

    def test_same_name_outside_the_gate_still_routes(self):
        self.assertIsNotNone(_call("Tex", "listening"))


if __name__ == "__main__":
    unittest.main()
