"""In-chat email login: step handlers, the AI turn interpreter, auth actions.

The interpreter is AI-first; with no LLM configured (forced off in the flow
tests) it falls back to the format regexes, so the state machine under test is
deterministic. AI-verdict handling is tested separately with a mocked llm_json.
"""

import unittest
from unittest.mock import patch

from app.guest_login import (
    GUEST_STEP_LOGIN_OTP,
    GUEST_STEP_LOGIN_PHONE,
    extract_email,
    handle_guest_login,
    interpret_login_reply,
    wants_login,
    wants_logout,
)
from app.guest_intake import lana_profile_guest_turn

_NO_LLM = patch("app.orchestrator.llm.llm_configured", return_value=False)


class TestGuestLoginHelpers(unittest.TestCase):
    def test_wants_login(self) -> None:
        self.assertTrue(wants_login("I want to log in"))
        self.assertTrue(wants_login("log me in"))
        self.assertTrue(wants_login("sign me in"))
        self.assertTrue(wants_login("I already have an account"))
        self.assertFalse(wants_login("I'm a Latino mom"))

    def test_wants_logout(self) -> None:
        self.assertTrue(wants_logout("I want to logout"))
        self.assertTrue(wants_logout("sign out please"))
        self.assertFalse(wants_logout("find neighbors"))

    def test_wants_cancel_logout(self) -> None:
        from app.guest_login import wants_cancel_logout

        self.assertTrue(wants_cancel_logout("stay logged in"))
        self.assertTrue(wants_cancel_logout("no"))
        self.assertTrue(wants_cancel_logout("never mind"))
        self.assertFalse(wants_cancel_logout("find neighbors"))

    def test_extract_email(self) -> None:
        self.assertEqual(extract_email("It's Sofia@Yahoo.IT thanks"), "sofia@yahoo.it")
        self.assertIsNone(extract_email("no address here"))


@_NO_LLM
class TestGuestLoginTurn(unittest.TestCase):
    def test_early_chat_login_asks_email(self, _llm) -> None:
        reply, ctx = handle_guest_login(
            "I want to log in",
            step="early_chat",
            session_ctx={"guest_step": "early_chat"},
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("email", reply.lower())
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_PHONE)
        self.assertEqual(ctx["auth_intent"], "login")
        self.assertIsNone(ctx["auth_action"])

    def test_early_chat_inline_email_sends_code(self, _llm) -> None:
        reply, ctx = handle_guest_login(
            "log me in, I'm sofia@yahoo.it",
            step="early_chat",
            session_ctx={"guest_step": "early_chat"},
        )
        self.assertIn("6-digit", reply)
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_OTP)
        self.assertEqual(ctx["login_phone"], "sofia@yahoo.it")
        self.assertEqual(
            ctx["auth_action"],
            {"type": "send_login_otp", "email": "sofia@yahoo.it", "verify_type": "email"},
        )

    def test_email_then_otp_step(self, _llm) -> None:
        reply, ctx = handle_guest_login(
            "sofia@yahoo.it",
            step=GUEST_STEP_LOGIN_PHONE,
            session_ctx={"guest_step": GUEST_STEP_LOGIN_PHONE, "auth_intent": "login"},
        )
        self.assertIn("6-digit", reply)
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_OTP)
        self.assertEqual(ctx["login_phone"], "sofia@yahoo.it")
        self.assertTrue(ctx["requires_login_otp"])
        self.assertEqual(ctx["auth_action"]["type"], "send_login_otp")
        self.assertEqual(ctx["auth_action"]["email"], "sofia@yahoo.it")
        self.assertEqual(ctx["auth_action"]["verify_type"], "email")

    def test_cancel_login_exits_email_step(self, _llm) -> None:
        base = {"guest_step": GUEST_STEP_LOGIN_PHONE, "auth_intent": "login"}
        for msg in ("no no thanks", "I want to build my profile"):
            reply, ctx = handle_guest_login(msg, step=GUEST_STEP_LOGIN_PHONE, session_ctx=base)
            self.assertIn("what would you like", reply.lower())
            self.assertIsNone(ctx.get("auth_intent"))
            self.assertIsNone(ctx.get("guest_step"))
            self.assertEqual(ctx.get("routing_phase"), "listening")
            self.assertIsNone(ctx.get("auth_action"))

    def test_ai_cancel_exits_email_step(self, _llm) -> None:
        # "find neighbors" never matched the cancel word-list — the AI verdict
        # reads the pivot and releases the flow.
        _llm.return_value = True
        with (
            patch("app.orchestrator.llm.router_model", return_value="m"),
            patch(
                "app.orchestrator.llm.llm_json",
                return_value={"action": "cancel", "email": None, "code": None},
            ),
        ):
            reply, ctx = handle_guest_login(
                "find neighbors",
                step=GUEST_STEP_LOGIN_PHONE,
                session_ctx={"guest_step": GUEST_STEP_LOGIN_PHONE, "auth_intent": "login"},
            )
        self.assertIn("what would you like", reply.lower())
        self.assertIsNone(ctx.get("guest_step"))

    def test_otp_returns_token_and_verify_action(self, _llm) -> None:
        reply, ctx = handle_guest_login(
            "000000",
            step=GUEST_STEP_LOGIN_OTP,
            session_ctx={
                "guest_step": GUEST_STEP_LOGIN_OTP,
                "login_phone": "sofia@yahoo.it",
                "auth_intent": "login",
            },
        )
        self.assertIn("signing you in", reply.lower())
        self.assertEqual(ctx["login_otp_token"], "000000")
        self.assertEqual(
            ctx["auth_action"],
            {
                "type": "verify_login_otp",
                "email": "sofia@yahoo.it",
                "token": "000000",
                "verify_type": "email",
            },
        )

    def test_new_email_at_otp_step_switches_address(self, _llm) -> None:
        # The bug from QA 2026-07-23: a corrected email at the code step was
        # swallowed and the stale address re-prompted forever.
        reply, ctx = handle_guest_login(
            "elisabetta@dibartolo.de",
            step=GUEST_STEP_LOGIN_OTP,
            session_ctx={
                "guest_step": GUEST_STEP_LOGIN_OTP,
                "login_phone": "sofia@yahoo.it",
                "login_otp_attempts": 2,
                "auth_intent": "login",
            },
        )
        self.assertIn("elisabetta@dibartolo.de", reply)
        self.assertNotIn("sofia@yahoo.it", reply)
        self.assertEqual(ctx["login_phone"], "elisabetta@dibartolo.de")
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_OTP)
        self.assertEqual(ctx["auth_action"]["type"], "send_login_otp")
        self.assertEqual(ctx["auth_action"]["email"], "elisabetta@dibartolo.de")
        self.assertIsNone(ctx["login_otp_attempts"])  # fresh address — reset (None so the merge deletes it)

    def test_same_email_at_otp_step_resends(self, _llm) -> None:
        reply, ctx = handle_guest_login(
            "sofia@yahoo.it",
            step=GUEST_STEP_LOGIN_OTP,
            session_ctx={
                "guest_step": GUEST_STEP_LOGIN_OTP,
                "login_phone": "sofia@yahoo.it",
                "auth_intent": "login",
            },
        )
        self.assertIn("fresh code", reply)
        self.assertEqual(ctx["auth_action"]["type"], "send_login_otp")
        self.assertEqual(ctx["auth_action"]["email"], "sofia@yahoo.it")

    def test_otp_reprompt_offers_correction_and_caps(self, _llm) -> None:
        ctx_in = {
            "guest_step": GUEST_STEP_LOGIN_OTP,
            "login_phone": "sofia@yahoo.it",
            "auth_intent": "login",
        }
        reply, ctx = handle_guest_login("hmm", step=GUEST_STEP_LOGIN_OTP, session_ctx=ctx_in)
        self.assertIn("different email", reply)
        self.assertIsNone(ctx["auth_action"])  # nothing sent — no send claim
        self.assertEqual(ctx["login_otp_attempts"], 1)
        reply, ctx = handle_guest_login("hmm", step=GUEST_STEP_LOGIN_OTP, session_ctx=ctx)
        reply, ctx = handle_guest_login("hmm", step=GUEST_STEP_LOGIN_OTP, session_ctx=ctx)
        self.assertIn("whenever you're ready", reply)
        self.assertIsNone(ctx.get("guest_step"))  # released

    def test_lana_turn_login_intent(self, _llm) -> None:
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
        self.assertIn("email", reply.lower())

    def test_log_me_in_phrase(self, _llm) -> None:
        reply, ctx = handle_guest_login(
            "log me in",
            step="early_chat",
            session_ctx={"guest_step": "early_chat"},
        )
        self.assertIn("email", reply.lower())
        self.assertEqual(ctx["guest_step"], GUEST_STEP_LOGIN_PHONE)


class TestInterpretLoginReply(unittest.TestCase):
    """AI verdict handling — llm_json mocked; regex fallback when unconfigured."""

    @_NO_LLM
    def test_fallback_reads_formats(self, _llm) -> None:
        self.assertEqual(
            interpret_login_reply("123456", expecting="code")["action"], "code"
        )
        read = interpret_login_reply("use a@b.co please", expecting="code")
        self.assertEqual(read["action"], "email")
        self.assertEqual(read["email"], "a@b.co")
        self.assertEqual(
            interpret_login_reply("never mind", expecting="code")["action"], "cancel"
        )
        self.assertEqual(
            interpret_login_reply("hmm what?", expecting="code")["action"], "other"
        )

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_ai_cancel_in_any_language(self, _llm, _model) -> None:
        # "ya no quiero iniciar sesión" — the English-only cancel word-list
        # can't read this; the AI verdict can.
        with patch(
            "app.orchestrator.llm.llm_json",
            return_value={"action": "cancel", "email": None, "code": None},
        ):
            read = interpret_login_reply("ya no quiero iniciar sesión", expecting="code")
        self.assertEqual(read["action"], "cancel")

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_ai_resend(self, _llm, _model) -> None:
        with patch(
            "app.orchestrator.llm.llm_json",
            return_value={"action": "resend", "email": None, "code": None},
        ):
            read = interpret_login_reply("it never arrived", expecting="code")
        self.assertEqual(read["action"], "resend")

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_ai_email_is_validated_and_lowercased(self, _llm, _model) -> None:
        with patch(
            "app.orchestrator.llm.llm_json",
            return_value={"action": "email", "email": "SOFIA@YAHOO.IT", "code": None},
        ):
            read = interpret_login_reply("wrong one, use SOFIA@YAHOO.IT", expecting="code")
        self.assertEqual(read["email"], "sofia@yahoo.it")

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_ai_hallucinated_email_degrades_to_other(self, _llm, _model) -> None:
        # AI claims an email but neither its value nor the message has one.
        with patch(
            "app.orchestrator.llm.llm_json",
            return_value={"action": "email", "email": "not-an-address", "code": None},
        ):
            read = interpret_login_reply("sure use my usual", expecting="code")
        self.assertEqual(read["action"], "other")

    @patch("app.orchestrator.llm.router_model", return_value="m")
    @patch("app.orchestrator.llm.llm_configured", return_value=True)
    def test_ai_failure_falls_back(self, _llm, _model) -> None:
        with patch("app.orchestrator.llm.llm_json", side_effect=RuntimeError("down")):
            read = interpret_login_reply("123456", expecting="code")
        self.assertEqual(read["action"], "code")
        self.assertEqual(read["code"], "123456")


if __name__ == "__main__":
    unittest.main()
