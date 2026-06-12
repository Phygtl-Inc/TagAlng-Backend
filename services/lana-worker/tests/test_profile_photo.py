import unittest
from unittest.mock import patch

from app.discovery_slots import slots_want_profile_photo
from app.profile_photo import (
    PHASE_AWAIT_PROFILE_PHOTO,
    handle_profile_photo_turn,
)
from app.ui_intent import UI_INTENT_UPLOAD_PROFILE_PHOTO, derive_ui_intent


class TestProfilePhotoSlots(unittest.TestCase):
    def test_slots_want_profile_photo_goal(self) -> None:
        self.assertTrue(
            slots_want_profile_photo(
                {"goal": "profile_photo", "confidence": 0.9, "profile_photo_action": "start"},
            )
        )
        self.assertFalse(
            slots_want_profile_photo({"goal": "chat", "confidence": 0.9}),
        )

    def test_slots_want_in_phase(self) -> None:
        self.assertTrue(
            slots_want_profile_photo({}, routing_phase=PHASE_AWAIT_PROFILE_PHOTO),
        )

    def test_asks_for_upload_ui(self) -> None:
        slots = {
            "goal": "profile_photo",
            "profile_photo_action": "start",
            "confidence": 0.95,
        }
        result = handle_profile_photo_turn(
            "I want to upload my picture",
            session_ctx={},
            slots=slots,
            user_id="user-1",
            phone_verified=True,
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        reply, ctx = result
        assert reply is not None
        self.assertIn("add photo", reply.lower())
        self.assertEqual(ctx["routing_phase"], PHASE_AWAIT_PROFILE_PHOTO)
        self.assertEqual(derive_ui_intent(ctx), UI_INTENT_UPLOAD_PROFILE_PHOTO)

    def test_guest_unverified_blocked(self) -> None:
        slots = {
            "goal": "profile_photo",
            "profile_photo_action": "start",
            "confidence": 0.95,
        }
        result = handle_profile_photo_turn(
            "upload my profile photo",
            session_ctx={},
            slots=slots,
            user_id="user-1",
            phone_verified=False,
            is_anonymous=True,
        )
        self.assertIsNotNone(result)
        reply, _ = result
        assert reply is not None
        self.assertIn("verify your phone", reply.lower())

    def test_accept_after_suggestion(self) -> None:
        slots = {
            "goal": "profile_photo",
            "profile_photo_action": "accept",
            "confidence": 0.9,
        }
        result = handle_profile_photo_turn(
            "yes",
            session_ctx={},
            slots=slots,
            user_id="user-1",
            phone_verified=True,
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        _, ctx = result
        self.assertEqual(ctx["routing_phase"], PHASE_AWAIT_PROFILE_PHOTO)

    def test_skip_exits(self) -> None:
        slots = {
            "goal": "profile_photo",
            "profile_photo_action": "skip",
            "confidence": 0.9,
        }
        result = handle_profile_photo_turn(
            "no thanks",
            session_ctx={"routing_phase": PHASE_AWAIT_PROFILE_PHOTO},
            slots=slots,
            user_id="user-1",
            phone_verified=True,
            is_anonymous=False,
        )
        self.assertIsNotNone(result)
        _, ctx = result
        self.assertEqual(ctx.get("routing_phase"), "listening")

    @patch("app.profile_photo.upload_profile_photo_bytes", return_value="https://x/avatar.jpg")
    def test_upload_bytes(self, _mock) -> None:
        from app.profile_photo import upload_profile_photo_bytes

        url = upload_profile_photo_bytes("uid", b"abc", "image/jpeg")
        self.assertEqual(url, "https://x/avatar.jpg")


if __name__ == "__main__":
    unittest.main()
