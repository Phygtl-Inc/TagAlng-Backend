import unittest

from app.ui_intent import (
    UI_INTENT_CHAT,
    UI_INTENT_COLLECT_OTP,
    UI_INTENT_COLLECT_PHONE,
    UI_INTENT_COLLECT_ZIP,
    UI_INTENT_CONFIRM_PROFILE,
    UI_INTENT_SHOW_PEER_PREVIEW,
    UI_INTENT_SIGN_OUT,
    UI_INTENT_UPLOAD_PROFILE_PHOTO,
    UI_INTENT_OFFER_NEIGHBOR_INTRO,
    UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
    UI_INTENT_SHOW_PENDING_INTROS,
    UI_INTENT_SIGNAL_SAVED,
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

    def test_preview_peers_beat_activities_when_peer_intent(self) -> None:
        from app.ui_intent import UI_INTENT_SHOW_ACTIVITY_PREVIEW

        self.assertEqual(
            derive_ui_intent(
                {
                    "routing_phase": "preview",
                    "active_intent": "discovery.find_by_attrs",
                },
                peer_count=3,
                activity_count=2,
            ),
            UI_INTENT_SHOW_PEER_PREVIEW,
        )
        self.assertEqual(
            derive_ui_intent(
                {
                    "routing_phase": "preview",
                    "active_intent": "discovery.find_activities",
                },
                peer_count=3,
                activity_count=2,
            ),
            UI_INTENT_SHOW_ACTIVITY_PREVIEW,
        )

    def test_verify_signup_otp_on_preview_uses_collect_otp(self) -> None:
        self.assertEqual(
            derive_ui_intent(
                {
                    "routing_phase": "preview",
                    "requires_phone_verification": True,
                    "auth_action": {
                        "type": "verify_signup_otp",
                        "phone": "+15550999012",
                        "token": "000000",
                    },
                },
                peer_count=3,
                phone_verified=False,
            ),
            UI_INTENT_COLLECT_OTP,
        )

    def test_preview_verify_uses_collect_phone(self) -> None:
        self.assertEqual(
            derive_ui_intent(
                {"routing_phase": "preview", "requires_phone_verification": True},
                peer_count=3,
                phone_verified=False,
            ),
            UI_INTENT_COLLECT_PHONE,
        )

    def test_ready_to_complete(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "need_zip"}, ready_to_complete=True),
            UI_INTENT_CONFIRM_PROFILE,
        )

    def test_logout_phase(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "await_logout", "auth_intent": "logout"}),
            UI_INTENT_SIGN_OUT,
        )

    def test_profile_photo_phase(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "await_profile_photo"}),
            UI_INTENT_UPLOAD_PROFILE_PHOTO,
        )

    def test_listening(self) -> None:
        self.assertEqual(derive_ui_intent({}), UI_INTENT_CHAT)

    def test_offer_neighbor_intro(self) -> None:
        self.assertEqual(
            derive_ui_intent({"pending_intro_offer": {"candidate_user_id": "u1"}}),
            UI_INTENT_OFFER_NEIGHBOR_INTRO,
        )

    def test_duplicate_intro_beats_stale_offer(self) -> None:
        from app.ui_intent import UI_INTENT_SHOW_BLOCK_LOG

        self.assertEqual(
            derive_ui_intent(
                {
                    "recent_intro_duplicate": {
                        "candidate_user_id": "u1",
                        "candidate_nickname": "Natasha",
                    },
                    "pending_intro_offer": {
                        "candidate_user_id": "u1",
                        "candidate_nickname": "Natasha",
                    },
                }
            ),
            UI_INTENT_SHOW_BLOCK_LOG,
        )

    def test_propose_neighbor_intro(self) -> None:
        self.assertEqual(
            derive_ui_intent({"intro_proposal": {"intro_id": "i1"}}),
            UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
        )
        self.assertEqual(
            derive_ui_intent(
                {
                    "intro_proposal": {"intro_id": "i1"},
                    "pending_intro_offer": {"candidate_user_id": "u1"},
                }
            ),
            UI_INTENT_PROPOSE_NEIGHBOR_INTRO,
        )

    def test_show_pending_intros(self) -> None:
        self.assertEqual(
            derive_ui_intent(
                {
                    "active_intent": "social.list_intros",
                    "pending_intros": [],
                }
            ),
            UI_INTENT_SHOW_PENDING_INTROS,
        )

    def test_stale_block_log_without_active_intent_is_chat(self) -> None:
        from app.ui_intent import UI_INTENT_SHOW_BLOCK_LOG

        self.assertEqual(
            derive_ui_intent(
                {
                    "active_intent": "looking.meet",
                    "block_log_entries": [{"entry_id": "e1"}],
                    "signal_saved": {"detail_text": "walking buddy"},
                }
            ),
            UI_INTENT_SIGNAL_SAVED,
        )
        self.assertEqual(
            derive_ui_intent(
                {
                    "active_intent": "help.what_can_you_do",
                    "block_log_entries": [{"entry_id": "e1"}],
                }
            ),
            UI_INTENT_CHAT,
        )
        self.assertEqual(
            derive_ui_intent(
                {
                    "active_intent": "discovery.block_log",
                    "block_log_entries": [{"entry_id": "e1"}],
                }
            ),
            UI_INTENT_SHOW_BLOCK_LOG,
        )


if __name__ == "__main__":
    unittest.main()
