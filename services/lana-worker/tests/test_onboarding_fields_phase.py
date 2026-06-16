import unittest

from app.auth import AuthSession
from app.main import _onboarding_fields


class TestOnboardingFieldsPhase(unittest.TestCase):
    def test_phone_gate_forces_signup_phase_for_unverified_user(self) -> None:
        payload = _onboarding_fields(
            {"routing_phase": "listening", "requires_phone_verification": True},
            AuthSession(
                user_id="user-1",
                is_anonymous=True,
                phone_verified=False,
                home_block_id=None,
            ),
        )
        self.assertEqual(payload["routing_phase"], "await_signup_phone")

    def test_otp_turn_stays_on_collect_otp_not_phone(self) -> None:
        """After OTP posted to Lana, JWT may lag — do not rewind to phone gate."""
        payload = _onboarding_fields(
            {
                "routing_phase": "preview",
                "requires_phone_verification": True,
                "pending_post_verify": True,
                "auth_action": {
                    "type": "verify_signup_otp",
                    "phone": "+15550999012",
                    "token": "000000",
                    "verify_type": "phone_change",
                },
            },
            AuthSession(
                user_id="user-1",
                is_anonymous=True,
                phone_verified=False,
                home_block_id=None,
            ),
        )
        self.assertEqual(payload["routing_phase"], "await_signup_otp")
        self.assertEqual(payload["ui_intent"], "collect_otp")

    def test_link_phone_action_means_collect_otp(self) -> None:
        payload = _onboarding_fields(
            {
                "routing_phase": "preview",
                "requires_phone_verification": True,
                "auth_action": {
                    "type": "link_phone_signup",
                    "phone": "+15550999012",
                    "verify_type": "phone_change",
                },
            },
            AuthSession(
                user_id="user-1",
                is_anonymous=True,
                phone_verified=False,
                home_block_id=None,
            ),
        )
        self.assertEqual(payload["routing_phase"], "await_signup_otp")
        self.assertEqual(payload["ui_intent"], "collect_otp")

    def test_verified_user_keeps_original_phase(self) -> None:
        payload = _onboarding_fields(
            {"routing_phase": "preview", "requires_phone_verification": True},
            AuthSession(
                user_id="user-1",
                is_anonymous=False,
                phone_verified=True,
                home_block_id="b-1",
            ),
        )
        self.assertEqual(payload["routing_phase"], "preview")


if __name__ == "__main__":
    unittest.main()
