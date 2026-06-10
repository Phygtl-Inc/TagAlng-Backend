import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.auth import AuthSession, require_home_block_for_purpose, verify_auth
from app.profile_intake import GUEST_PROFILE_OPENING, lana_profile_guest_opening


class RequireHomeBlockForPurposeTests(unittest.TestCase):
    def test_profile_intake_without_block_ok(self) -> None:
        auth = AuthSession(
            user_id="u1",
            is_anonymous=True,
            phone_verified=False,
            home_block_id=None,
        )
        self.assertIsNone(require_home_block_for_purpose(auth, "profile_intake"))

    def test_event_draft_without_block_raises(self) -> None:
        auth = AuthSession(
            user_id="u1",
            is_anonymous=False,
            phone_verified=True,
            home_block_id=None,
        )
        with self.assertRaises(HTTPException) as ctx:
            require_home_block_for_purpose(auth, "event_draft")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "home_block_required")

    def test_returns_block_when_assigned(self) -> None:
        auth = AuthSession(
            user_id="u1",
            is_anonymous=False,
            phone_verified=True,
            home_block_id="8a2a1072b5affff",
        )
        self.assertEqual(
            require_home_block_for_purpose(auth, "event_draft"),
            "8a2a1072b5affff",
        )


class VerifyAuthTests(unittest.TestCase):
    @patch("app.auth.create_client")
    @patch("app.auth.httpx.Client")
    def test_reads_anonymous_and_phone_flags(self, mock_client_cls, mock_create_client) -> None:
        mock_http = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_http
        mock_http.get.return_value.status_code = 200
        mock_http.get.return_value.json.return_value = {
            "id": "anon-uuid",
            "is_anonymous": True,
        }

        mock_sb = MagicMock()
        mock_create_client.return_value = mock_sb
        mock_sb.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"home_block_id": None, "phone_verified_at": None}
        ]

        with patch.dict(
            "app.auth.os.environ",
            {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_ANON_KEY": "anon-key",
                "SUPABASE_SERVICE_ROLE_KEY": "service-key",
            },
        ):
            auth = verify_auth("Bearer test-jwt")

        self.assertEqual(auth.user_id, "anon-uuid")
        self.assertTrue(auth.is_anonymous)
        self.assertFalse(auth.phone_verified)
        self.assertIsNone(auth.home_block_id)


class GuestOpeningTests(unittest.TestCase):
    def test_guest_opening_is_instant_and_on_script(self) -> None:
        opening, status, ctx, _ui = lana_profile_guest_opening()
        self.assertEqual(opening, GUEST_PROFILE_OPENING)
        self.assertEqual(status, "continue")
        self.assertTrue(ctx.get("guest_intake"))
