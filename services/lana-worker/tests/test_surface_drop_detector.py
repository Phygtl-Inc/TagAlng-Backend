"""The detector for silently dropped turn surfaces.

Four times in one QA session (2026-08-21) a lane built a surface correctly and something
downstream threw it away with no error and no log: peer_matches and activity_previews to
the intent allowlists in _onboarding_fields, and policy_chips to clear_turn_surfaces. Each
one was found only by screenshot, because Lana's prose promised cards the screen did not
have. These tests hold the detector that makes the next one show up in the log instead.
"""

import logging
import unittest
from unittest.mock import patch

from app.auth import AuthSession


def _auth() -> AuthSession:
    return AuthSession(
        user_id="u1", is_anonymous=False, phone_verified=True, home_block_id="b1"
    )


def _fields(ctx: dict):
    from app.main import _onboarding_fields

    # The surfaces under test are all read straight off ctx; everything else this
    # function reaches for is absent, which is the normal shape of a plain chat turn.
    with patch("app.peer_discovery_surface.stamp_peer_discovery_ctx"):
        return _onboarding_fields(ctx, _auth())


class TestSurfaceDropDetector(unittest.TestCase):
    _ROW = {
        "peer_user_id": "u2",
        "nickname": "Ann",
        "matching_peer_label": "Reads a lot",
        "similarity_score": None,
        "preview": False,
    }

    def test_it_fires_when_a_gate_eats_peer_rows(self) -> None:
        # An intent that is not on the peer allowlist: exactly the shape the community
        # roster shipped in before discovery.communities was added to it.
        ctx = {"peer_matches": [dict(self._ROW)], "active_intent": "discovery.not_a_peer_lane"}
        with self.assertLogs("app.main", level=logging.WARNING) as logs:
            out = _fields(ctx)
        self.assertEqual(out["peer_matches"], [])
        joined = " ".join(logs.output)
        self.assertIn("surface_dropped kind=peer_matches", joined)
        # The intent that dropped them is in the line — that is where the fix goes.
        self.assertIn("discovery.not_a_peer_lane", joined)

    def test_it_stays_quiet_when_the_rows_ship(self) -> None:
        from app.ui_intent import PEER_DISCOVERY_ACTIVE_INTENTS

        ctx = {
            "peer_matches": [dict(self._ROW)],
            "active_intent": sorted(PEER_DISCOVERY_ACTIVE_INTENTS)[0],
        }
        with patch("app.main._warn_surface_dropped") as warn:
            out = _fields(ctx)
        self.assertEqual(len(out["peer_matches"]), 1)
        warn.assert_not_called()

    def test_it_fires_when_activity_cards_are_eaten(self) -> None:
        ctx = {
            "activity_previews": [{"title": "Sushi & Social Meetup", "activity_id": "e1"}],
            "active_intent": "discovery.not_an_activity_lane",
        }
        with self.assertLogs("app.main", level=logging.WARNING) as logs:
            out = _fields(ctx)
        self.assertEqual(out["activity_previews"], [])
        self.assertIn("surface_dropped kind=activity_previews", " ".join(logs.output))

    def test_it_fires_when_a_wiped_surface_is_never_re_attached(self) -> None:
        """clear_turn_surfaces nulls every turn-scoped key and each lane re-attaches by
        name; the clarifier's answer buttons were nulled a line after being set."""
        from app.turn_surfaces import clear_turn_surfaces

        ctx = {"policy_chips": [{"label": "Barnes & Noble", "send": "who is in it"}]}
        clear_turn_surfaces(ctx)
        self.assertEqual(ctx["_wiped_turn_surfaces"], ["policy_chips"])
        with self.assertLogs("app.main", level=logging.WARNING) as logs:
            _fields(ctx)
        self.assertIn("surface_dropped kind=wiped:policy_chips", " ".join(logs.output))

    def test_a_re_attached_surface_does_not_report(self) -> None:
        from app.turn_surfaces import clear_turn_surfaces

        ctx = {"policy_chips": [{"label": "Barnes & Noble", "send": "who is in it"}]}
        clear_turn_surfaces(ctx)
        # What the lane does next: put back what it stamped.
        ctx["policy_chips"] = [{"label": "Barnes & Noble", "send": "who is in it"}]
        with patch("app.main._warn_surface_dropped") as warn:
            _fields(ctx)
        warn.assert_not_called()

    def test_a_clean_turn_is_silent(self) -> None:
        with patch("app.main._warn_surface_dropped") as warn:
            _fields({"active_intent": "discovery.communities"})
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
