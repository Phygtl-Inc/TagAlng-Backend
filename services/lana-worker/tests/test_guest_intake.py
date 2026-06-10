import unittest
from unittest.mock import patch

from app.guest_intake import (
    GUEST_STEP_INTRO_NAME,
    GUEST_STEP_OFFERED,
    GUEST_STEP_PHONE,
    GUEST_STEP_POST_VERIFY,
    extract_intro_name,
    has_joint_moment_signals,
    parse_yes_no,
)


class TestGuestIntakeHelpers(unittest.TestCase):
    def test_parse_yes_no(self) -> None:
        self.assertTrue(parse_yes_no("yes please"))
        self.assertTrue(parse_yes_no("Yeah"))
        self.assertFalse(parse_yes_no("not now"))
        self.assertIsNone(parse_yes_no("maybe"))

    def test_extract_intro_name(self) -> None:
        self.assertEqual(extract_intro_name("Linda"), "Linda")
        self.assertEqual(extract_intro_name("call me Linda"), "Linda")
        self.assertEqual(extract_intro_name("I'm Linda"), "Linda")

    def test_joint_moment_signals_latino_mom(self) -> None:
        history = []
        self.assertTrue(
            has_joint_moment_signals(
                history,
                "I'm a Latino mom in Lake Nona, new here about 3 months.",
            )
        )

    def test_joint_moment_signals_need_both(self) -> None:
        self.assertFalse(has_joint_moment_signals([], "I like coffee"))
        self.assertFalse(has_joint_moment_signals([], "I'm from Brazil"))

    def test_joint_moment_signals_pakistan_dad(self) -> None:
        self.assertTrue(
            has_joint_moment_signals(
                [],
                "I am a pakistan person who is new to this block and i am a dad",
            )
        )


class TestGuestIntakeTurn(unittest.TestCase):
    @patch("app.guest_intake.fetch_joint_moment")
    def test_offers_intro_on_latino_mom_message(self, mock_fetch) -> None:
        from app.guest_intake import lana_profile_guest_turn

        mock_fetch.return_value = {
            "joint_moment_id": "jm-1",
            "candidate": {"nickname": "Maria"},
            "lana_copy": "Maria is looking for Brazilian moms. Want an intro?",
            "is_demo": True,
        }
        reply, status, ctx, _, jm = lana_profile_guest_turn(
            user_block="HOST CONTEXT",
            history=[{"role": "user", "content": "opening"}],
            user_message="I'm a Latino mom in Lake Nona, new here 3 months.",
            session_ctx={"guest_step": "early_chat"},
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=False,
        )
        self.assertEqual(status, "continue")
        self.assertEqual(ctx["guest_step"], GUEST_STEP_OFFERED)
        self.assertIn("Maria", reply)
        self.assertIsNotNone(jm)

    @patch("app.guest_intake.accept_joint_moment")
    def test_yes_asks_intro_name(self, mock_accept) -> None:
        from app.guest_intake import lana_profile_guest_turn

        mock_accept.return_value = {"status": "accepted"}
        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST CONTEXT",
            history=[],
            user_message="yes",
            session_ctx={
                "guest_step": GUEST_STEP_OFFERED,
                "joint_moment_id": "jm-1",
                "joint_moment": {"candidate": {"nickname": "Maria"}},
            },
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=False,
        )
        self.assertEqual(ctx["guest_step"], GUEST_STEP_INTRO_NAME)
        self.assertIn("Maria", reply)
        self.assertIn("call you", reply.lower())

    @patch("app.guest_intake.accept_joint_moment")
    def test_yes_skips_name_when_already_given(self, mock_accept) -> None:
        from app.guest_intake import lana_profile_guest_turn

        mock_accept.return_value = {"status": "accepted"}
        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST CONTEXT",
            history=[
                {"role": "user", "content": "pakistan dad new to block"},
                {"role": "user", "content": "Asjid"},
            ],
            user_message="yes",
            session_ctx={
                "guest_step": GUEST_STEP_OFFERED,
                "joint_moment_id": "jm-1",
                "joint_moment": {"candidate": {"nickname": "Maria"}},
            },
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=False,
        )
        self.assertEqual(ctx["guest_step"], GUEST_STEP_PHONE)
        self.assertEqual(ctx.get("intro_name"), "Asjid")
        self.assertIn("verify", reply.lower())

    def test_name_then_phone_prompt(self) -> None:
        from app.guest_intake import lana_profile_guest_turn

        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST CONTEXT",
            history=[],
            user_message="Linda",
            session_ctx={
                "guest_step": GUEST_STEP_INTRO_NAME,
                "joint_moment": {"candidate": {"nickname": "Maria"}},
            },
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=False,
        )
        self.assertEqual(ctx["guest_step"], GUEST_STEP_PHONE)
        self.assertTrue(ctx["requires_phone_verification"])
        self.assertIn("verify", reply.lower())

    def test_after_phone_asks_kids(self) -> None:
        from app.guest_intake import lana_profile_guest_turn

        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST CONTEXT",
            history=[],
            user_message="done",
            session_ctx={
                "guest_step": GUEST_STEP_PHONE,
                "intro_name": "Linda",
            },
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=True,
        )
        self.assertEqual(ctx["guest_step"], GUEST_STEP_POST_VERIFY)
        self.assertIn("kids", reply.lower())


if __name__ == "__main__":
    unittest.main()
