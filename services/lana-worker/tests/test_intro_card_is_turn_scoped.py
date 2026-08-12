"""An intro card must not hijack every later turn's UI.

derive_ui_intent checks the intro surface FIRST — ahead of hosting, items, tips, looks.
It used to key on `intro_proposal`, which persists in session context as state for the
"don't re-propose" guards, so one intro meant every subsequent turn returned
propose_neighbor_intro: the host setup carousel (and every other card) silently stopped
rendering for the rest of the session, while Lana's text still said "add them below".
"""

import unittest

from app.turn_surfaces import TURN_SCOPED_SURFACES, clear_turn_surfaces
from app.ui_intent import (
    UI_INTENT_EVENT_SETUP,
    UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
    derive_ui_intent,
)

_HOSTING = {"event_host_active": True, "host_stage": "setup"}


class TestIntroCardIsTurnScoped(unittest.TestCase):
    def test_intro_turn_still_shows_the_intro_card(self):
        ctx = {"intro_proposal": {"intro_id": "i1"}, "intro_proposed_now": True}
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_PROPOSE_NEIGHBOR_INTRO)

    def test_hosting_after_an_intro_shows_the_setup_carousel(self):
        # The intro payload is still in session state (the re-propose guards read it),
        # but the card's turn is over — hosting must own this turn's UI.
        ctx = {"intro_proposal": {"intro_id": "i1"}, **_HOSTING}
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_EVENT_SETUP)

    def test_one_turn_flag_is_cleared_by_the_surface_sweep(self):
        self.assertIn("intro_proposed_now", TURN_SCOPED_SURFACES)
        ctx = {"intro_proposed_now": True}
        clear_turn_surfaces(ctx)
        self.assertIsNone(ctx["intro_proposed_now"])

    def test_persistent_payload_is_not_swept(self):
        # discovery_route reads intro_proposal on LATER turns to avoid double-proposing.
        self.assertNotIn("intro_proposal", TURN_SCOPED_SURFACES)


if __name__ == "__main__":
    unittest.main()
