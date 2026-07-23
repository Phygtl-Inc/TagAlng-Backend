import unittest
from unittest.mock import patch

from app.guest_capabilities import (
    format_peer_matches,
    handle_guest_capability,
    wants_host_activity,
    wants_peer_find,
)
from app.guest_intake import GUEST_STEP_EARLY, GUEST_STEP_POST_VERIFY, lana_profile_guest_turn


class TestGuestCapabilityIntents(unittest.TestCase):
    def test_wants_peer_find(self) -> None:
        self.assertTrue(wants_peer_find("find people like me on the block"))
        self.assertTrue(wants_peer_find("who else is nearby like me"))
        self.assertTrue(wants_peer_find("i wanna meet my neighbours"))
        self.assertTrue(wants_peer_find("nothing just find me people"))
        self.assertTrue(wants_peer_find("fuck off and just show me users in my block"))
        self.assertFalse(wants_peer_find("I'm a Latino mom"))

    def test_wants_host_activity(self) -> None:
        self.assertTrue(wants_host_activity("I want to host an activity this weekend"))
        self.assertFalse(wants_host_activity("find neighbors like me"))

    def test_unverified_returns_none(self) -> None:
        self.assertIsNone(
            handle_guest_capability(
                "find people like me",
                phone_verified=False,
                home_block_id="block-1",
                user_jwt="jwt",
                guest_step=GUEST_STEP_POST_VERIFY,
            )
        )

    @patch("app.guest_capabilities.fetch_peer_matches")
    def test_verified_peer_find(self, mock_fetch) -> None:
        mock_fetch.return_value = [
            {
                "peer_user_id": "u2",
                "nickname": "Maria",
                "matching_peer_label": "Brazilian moms",
                "similarity_score": 0.87,
            }
        ]
        result = handle_guest_capability(
            "find people like me",
            phone_verified=True,
            home_block_id="block-1",
            user_jwt="jwt",
            guest_step=GUEST_STEP_POST_VERIFY,
        )
        self.assertIsNotNone(result)
        reply, extra = result
        # The reply stays short — names/traits live on the match cards, not the text.
        self.assertIn("1 neighbor", reply)
        self.assertNotIn("Maria", reply)
        self.assertEqual(extra.get("intent"), "peer_find")
        self.assertEqual(len(extra.get("peer_matches", [])), 1)

    def test_format_empty_peers(self) -> None:
        self.assertIn("Complete", format_peer_matches([]))


class TestGuestCapabilityGating(unittest.TestCase):
    @patch("app.guest_intake.lana_profile_turn")
    def test_signup_skips_peer_find(self, mock_turn) -> None:
        mock_turn.return_value = ("Tell me about yourself.", "continue", {}, {})
        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST",
            history=[],
            user_message="find people like me",
            session_ctx={"guest_step": GUEST_STEP_EARLY},
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=False,
        )
        mock_turn.assert_called_once()
        self.assertNotIn("peer_matches", ctx)
        self.assertEqual(ctx["guest_step"], GUEST_STEP_EARLY)
        self.assertIn("yourself", reply.lower())

    @patch("app.guest_capabilities.fetch_peer_matches")
    def test_post_verify_runs_peer_find(self, mock_fetch) -> None:
        mock_fetch.return_value = [
            {
                "nickname": "Beatriz",
                "matching_peer_label": "weekend activities",
                "similarity_score": 0.72,
            }
        ]
        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST",
            history=[],
            user_message="find people like me",
            session_ctx={"guest_step": GUEST_STEP_POST_VERIFY},
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=True,
            home_block_id="block-1",
        )
        self.assertIn("1 neighbor", reply)
        self.assertEqual(ctx.get("intent"), "peer_find")
        self.assertEqual(len(ctx.get("peer_matches", [])), 1)
        self.assertEqual(ctx["peer_matches"][0].get("nickname"), "Beatriz")


if __name__ == "__main__":
    unittest.main()
