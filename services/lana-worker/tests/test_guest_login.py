import unittest

from app.guest_login import (
    GUEST_STEP_LOGIN_OTP,
    GUEST_STEP_LOGIN_PHONE,
    extract_phone_e164,
    handle_guest_login,
    wants_login,
)
from app.guest_intake import lana_profile_guest_turn


class TestGuestLoginHelpers(unittest.TestCase):
    def test_wants_login(self) -> None:
        self.assertTrue(wants_login("I want to log in"))
        self.assertTrue(wants_login("I already have an account"))
        self.assertFalse(wants_login("I'm a Latino mom"))

    def test_extract_phone(self) -> None:
        self.assertEqual(extract_phone_e164("+15550000000"), "+15550000000")
        self.assertEqual(extract_phone_e164("5550000000"), "+15550000000")


class TestGuestLoginTurn(unittest.TestCase):
    def test_early_chat_login_asks_phone(self) -> None:
        reply, ctx = handle_guest_login(
            "I want to log in",
            step="early_chat",
            session_ctx={"guest_step": "early_chat"},
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("phone", reply.lower())
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_PHONE)
        self.assertEqual(ctx["auth_intent"], "login")

    def test_phone_then_otp_step(self) -> None:
        reply, ctx = handle_guest_login(
            "+15550000000",
            step=GUEST_STEP_LOGIN_PHONE,
            session_ctx={"guest_step": GUEST_STEP_LOGIN_PHONE, "auth_intent": "login"},
        )
        self.assertIn("6-digit", reply)
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_OTP)
        self.assertEqual(ctx["login_phone"], "+15550000000")
        self.assertTrue(ctx["requires_login_otp"])

    def test_otp_returns_token_for_fe(self) -> None:
        reply, ctx = handle_guest_login(
            "000000",
            step=GUEST_STEP_LOGIN_OTP,
            session_ctx={
                "guest_step": GUEST_STEP_LOGIN_OTP,
                "login_phone": "+15550000000",
                "auth_intent": "login",
            },
        )
        self.assertIn("signing you in", reply.lower())
        self.assertEqual(ctx["login_otp_token"], "000000")

    def test_lana_turn_login_intent(self) -> None:
        reply, _, ctx, _, _ = lana_profile_guest_turn(
            user_block="HOST",
            history=[],
            user_message="log in",
            session_ctx={"guest_step": "early_chat"},
            session_id="sess-1",
            user_jwt="jwt",
            phone_verified=False,
        )
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_PHONE)
        self.assertIn("phone", reply.lower())


if __name__ == "__main__":
    unittest.main()
