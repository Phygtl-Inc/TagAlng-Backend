import unittest

from app.ui_intent import (
    UI_INTENT_CHAT,
    UI_INTENT_COLLECT_OTP,
    UI_INTENT_COLLECT_PHONE,
    UI_INTENT_COLLECT_ZIP,
    UI_INTENT_CONFIRM_PROFILE,
    UI_INTENT_SHOW_PEER_PREVIEW,
    derive_ui_intent,
)


class TestDeriveUiIntent(unittest.TestCase):
    def test_zip_phase(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "need_zip"}),
            UI_INTENT_COLLECT_ZIP,
        )

    def test_login_otp(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "await_login_otp"}),
            UI_INTENT_COLLECT_OTP,
        )

    def test_signup_phone(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "await_signup_phone"}),
            UI_INTENT_COLLECT_PHONE,
        )

    def test_preview_peers(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "preview"}, peer_count=2),
            UI_INTENT_SHOW_PEER_PREVIEW,
        )

    def test_ready_to_complete(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "need_zip"}, ready_to_complete=True),
            UI_INTENT_CONFIRM_PROFILE,
        )

    def test_listening(self) -> None:
        self.assertEqual(derive_ui_intent({}), UI_INTENT_CHAT)


if __name__ == "__main__":
    unittest.main()
