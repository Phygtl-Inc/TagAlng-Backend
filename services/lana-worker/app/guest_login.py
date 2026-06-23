"""In-chat returning-user login during Meet Lana (early_chat)."""

from __future__ import annotations

import re
from typing import Any

GUEST_STEP_LOGIN_PHONE = "await_login_phone"
GUEST_STEP_LOGIN_OTP = "await_login_otp"
GUEST_STEP_LOGOUT = "await_logout"

# Release a stuck login step after this many replies that are neither a valid
# email/code nor a recognized cancel — so a user who can't or won't continue is
# never trapped re-reading the same prompt (the loop guard exempts auth phases).
LOGIN_STEP_MAX_PROMPTS = 3

_LOGIN_INTENT_RE = re.compile(
    r"\b(log\s*(?:me\s+)?in|login|sign\s*(?:me\s+)?in|signin|existing\s+account|"
    r"already\s+have\s+(?:an?\s+)?account|i\s+have\s+an\s+account|returning\s+user)\b",
    re.I,
)
_LOGOUT_INTENT_RE = re.compile(
    r"\b(log\s*out|logout|sign\s*out|signout|log\s*off)\b",
    re.I,
)
_CANCEL_RE = re.compile(
    r"\b(never\s*mind|cancel|sign\s*up|new\s+account|meet\s+lana|no\s+thanks?|nope|skip|stop|"
    r"not\s+(?:right\s+)?now|maybe\s+later|later|without\s+(?:it|that|the\s+code)|"
    r"keep\s+going|don'?t\s+want|do\s+not\s+want|"
    r"build\s+(?:my\s+)?profile|profile)\b",
    re.I,
)
_OTP_RE = re.compile(r"\b(\d{6})\b")
_PHONE_DIGITS_RE = re.compile(r"\+?[\d\s().-]{10,18}")


_LOGOUT_CANCEL_RE = re.compile(
    r"\b(stay\s+logged\s+in|stay\s+signed\s+in|keep\s+me\s+signed\s+in|remain\s+logged\s+in|"
    r"don'?t\s+(?:log|sign)\s*out|changed\s+my\s+mind|not\s+logging\s+out)\b",
    re.I,
)


def wants_login(text: str) -> bool:
    return bool(_LOGIN_INTENT_RE.search(str(text or "").strip()))


def wants_logout(text: str) -> bool:
    return bool(_LOGOUT_INTENT_RE.search(str(text or "").strip()))


def wants_cancel_login(text: str) -> bool:
    return bool(_CANCEL_RE.search(str(text or "").strip()))


def wants_cancel_logout(text: str) -> bool:
    s = str(text or "").strip()
    lower = s.lower().rstrip(".!")
    if lower in ("no", "nope", "nah"):
        return True
    return bool(_LOGOUT_CANCEL_RE.search(s)) or wants_cancel_login(s)


def _exit_login_ctx(session_ctx: dict[str, Any]) -> dict[str, Any]:
    """Leave login sub-flow; return to unified listening."""
    out = {
        **session_ctx,
        "auth_intent": None,
        "guest_step": None,
        "routing_phase": "listening",
        "requires_login_otp": False,
        "login_otp_token": None,
    }
    out.pop("login_phone", None)
    out.pop("login_email_attempts", None)
    out.pop("login_otp_attempts", None)
    return out


def _exit_logout_ctx(session_ctx: dict[str, Any]) -> dict[str, Any]:
    """User cancelled logout — return to normal chat (clear sign_out chrome)."""
    out = {
        **session_ctx,
        "auth_intent": None,
        "guest_step": None,
        "routing_phase": "listening",
        "requires_login_otp": False,
        "login_otp_token": None,
    }
    out.pop("login_phone", None)
    out.pop("auth_action", None)
    # Stale discovery/intro surface from before logout must not re-show peer cards.
    out.pop("intro_proposal", None)
    out.pop("pending_intro_offer", None)
    out.pop("peer_matches", None)
    return out


def _logout_ctx(session_ctx: dict[str, Any]) -> dict[str, Any]:
    """Signed-in logout — FE reads ui_intent sign_out + auth_action logout on this turn."""
    out = {
        **session_ctx,
        "auth_intent": "logout",
        "guest_step": GUEST_STEP_LOGOUT,
        "routing_phase": GUEST_STEP_LOGOUT,
        "requires_login_otp": False,
        "login_otp_token": None,
        "requires_phone_verification": False,
    }
    out.pop("login_phone", None)
    return out


def extract_otp_code(text: str) -> str | None:
    m = _OTP_RE.search(str(text or "").strip())
    return m.group(1) if m else None


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def extract_email(text: str) -> str | None:
    """Best-effort email address from a user message (lower-cased)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _EMAIL_RE.search(raw)
    return m.group(0).lower() if m else None


def _login_ctx(
    session_ctx: dict[str, Any],
    *,
    guest_step: str,
    login_phone: str | None = None,
    requires_login_otp: bool = False,
    login_otp_token: str | None = None,
) -> dict[str, Any]:
    out = {
        **session_ctx,
        "guest_intake": True,
        "guest_step": guest_step,
        "auth_intent": "login",
        "requires_phone_verification": False,
        "requires_login_otp": requires_login_otp,
        "routing_phase": guest_step,
    }
    if login_phone:
        out["login_phone"] = login_phone
    if login_otp_token:
        out["login_otp_token"] = login_otp_token
    return out


def handle_guest_login(
    user_message: str,
    *,
    step: str,
    session_ctx: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Scripted in-chat login. None if this turn is not part of login flow."""
    msg = str(user_message or "").strip()
    if not msg:
        return None

    if step == GUEST_STEP_LOGIN_PHONE:
        if wants_cancel_login(msg):
            return (
                "No problem — what would you like to do? Find neighbors, plan something, or tell me about yourself.",
                _exit_login_ctx(session_ctx),
            )
        email = extract_email(msg)
        if not email:
            # Cap re-prompts so a user who can't give an email isn't trapped here.
            attempts = int(session_ctx.get("login_email_attempts") or 0) + 1
            if attempts >= LOGIN_STEP_MAX_PROMPTS:
                return (
                    "No problem — find me when you're ready to sign in. "
                    "What would you like to do?",
                    _exit_login_ctx(session_ctx),
                )
            ctx = _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE)
            ctx["login_email_attempts"] = attempts
            return (
                "I didn't catch a valid email — something like you@example.com.",
                ctx,
            )
        ctx = _login_ctx(
            session_ctx,
            guest_step=GUEST_STEP_LOGIN_OTP,
            login_phone=email,
            requires_login_otp=True,
        )
        ctx.pop("login_email_attempts", None)  # valid email — reset
        return (
            f"Got it — I sent a 6-digit code to {email}. Enter it here when it arrives.",
            ctx,
        )

    if step == GUEST_STEP_LOGIN_OTP:
        if wants_cancel_login(msg):
            return (
                "Okay — what would you like to do next?",
                _exit_login_ctx(session_ctx),
            )
        otp = extract_otp_code(msg)
        if not otp:
            # Cap re-prompts: a user who keeps replying without a code (confused, or
            # quietly trying to bail) is released instead of looping the same line.
            attempts = int(session_ctx.get("login_otp_attempts") or 0) + 1
            if attempts >= LOGIN_STEP_MAX_PROMPTS:
                return (
                    "No problem — you can sign in whenever you're ready. "
                    "What would you like to do?",
                    _exit_login_ctx(session_ctx),
                )
            email = str(session_ctx.get("login_phone") or "your email")
            ctx = _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=str(session_ctx.get("login_phone") or "") or None,
                requires_login_otp=True,
            )
            ctx["login_otp_attempts"] = attempts
            return (f"Enter the 6-digit code we sent to {email}.", ctx)
        ctx = _login_ctx(
            session_ctx,
            guest_step=GUEST_STEP_LOGIN_OTP,
            login_phone=str(session_ctx.get("login_phone") or "") or None,
            requires_login_otp=True,
            login_otp_token=otp,
        )
        ctx.pop("login_otp_attempts", None)  # real code entered — reset
        return ("Perfect — signing you in now. One moment…", ctx)

    if step in ("early_chat", "intro_declined"):
        # If the user already gave their email this turn, send the code straight away
        # instead of re-asking and throwing the email they just typed away.
        email = extract_email(msg)
        if email:
            return (
                f"Got it — I sent a 6-digit code to {email}. Enter it here when it arrives.",
                _login_ctx(
                    session_ctx,
                    guest_step=GUEST_STEP_LOGIN_OTP,
                    login_phone=email,
                    requires_login_otp=True,
                ),
            )
        return (
            "Sure — what's the email on your account?",
            _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE),
        )

    return None
