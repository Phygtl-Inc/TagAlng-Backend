"""Auth turns must never be answered by decide_turn.

Prod/dev 2026-08-07: "sign out" got the AI-composed "All set — take care, Asjid."
with a "Come back anytime" chip. No `auth_action`, so derive_ui_intent returned
`chat` instead of `sign_out`, the PWA never rendered LanaLogoutConfirm, and the
user stayed signed in. Saying it again looped: `await_logout` is armed by the
handler that never ran.

This is fallout from enabling the policy gate, not from anything auth-side.
Before 2fb311a (2026-07-30) `_utterance_is_unsafe_backstop` returned the raw
(matched, kind) tuple — always truthy — so the gate's branch was dead and the
legacy engines answered every typed turn, logout included. Fixing that exposed
every action intent with no escape in the gate. Same shape as the tip-ask
regression covered by test_tip_ask_consent.TestPolicyGate.
"""

import unittest

from app.guest_login import _logout_ctx, wants_login
from app.lana_unified_pipeline import looks_like_logout
from app.ui_intent import UI_INTENT_CHAT, UI_INTENT_SIGN_OUT, derive_ui_intent


class TestAuthTurnsKeptOffPolicy(unittest.TestCase):
    """The gate's escape clause is `not looks_like_logout(msg)` — this pins it."""

    LOGOUT_PHRASES = (
        "sign out",
        "Sign out",
        "sign me out",
        "log out",
        "log me out",
        "logout",
        "signout",
    )

    def test_logout_phrases_escape_the_policy(self) -> None:
        for phrase in self.LOGOUT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertTrue(looks_like_logout(phrase))

    def test_ordinary_turns_still_belong_to_the_policy(self) -> None:
        for phrase in (
            "who's around to meet?",
            "i like italian pizza",
            "find pizza spots nearby",
            "what can you do",
            # Substring-only matches must not steal a conversational turn.
            "we ate at the Sign of the Bear",
            "my login for the gym app broke",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(looks_like_logout(phrase))

    def test_login_matcher_is_too_loose_for_this_gate(self) -> None:
        """Why login is NOT escaped alongside logout, despite the same exposure.

        `wants_login` carries a bare `\\blogin\\b`, so it fires on ordinary chat.
        Escaping on it would route a conversational turn to the auth engine —
        a worse bug than the one being fixed. Tighten the matcher first; this
        test is the tripwire that says when that has happened.
        """
        self.assertTrue(wants_login("log me in"))
        self.assertTrue(wants_login("my login for the gym app broke"))  # false positive


class TestLogoutCtxDrivesTheSignOutChrome(unittest.TestCase):
    """The FE renders LanaLogoutConfirm off ui_intent — that contract is what
    actually broke, so assert it end-to-end from the handler's own ctx."""

    def test_logout_ctx_yields_sign_out(self) -> None:
        self.assertEqual(derive_ui_intent(_logout_ctx({})), UI_INTENT_SIGN_OUT)

    def test_plain_chat_ctx_does_not(self) -> None:
        self.assertEqual(
            derive_ui_intent({"routing_phase": "listening"}), UI_INTENT_CHAT
        )


if __name__ == "__main__":
    unittest.main()
