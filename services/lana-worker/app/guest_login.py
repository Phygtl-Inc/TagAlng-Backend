"""In-chat returning-user login during Meet Lana (early_chat)."""

from __future__ import annotations

import re
from typing import Any

GUEST_STEP_LOGIN_PHONE = "await_login_phone"
GUEST_STEP_LOGIN_OTP = "await_login_otp"
GUEST_STEP_LOGOUT = "await_logout"

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
    r"not\s+now|build\s+(?:my\s+)?profile|profile)\b",
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


def extract_phone_e164(text: str) -> str | None:
    """Best-effort E.164 from user message (US-focused dev numbers)."""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = re.search(r"\+(\d{10,15})\b", raw.replace(" ", ""))
    if m:
        return f"+{m.group(1)}"
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if 10 <= len(digits) <= 15:
        return f"+{digits}"
    return None


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
        phone = extract_phone_e164(msg)
        if not phone:
            return (
                "I didn't catch a valid phone number — include country code if you can "
                "(e.g. +15550000000).",
                _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE),
            )
        return (
            f"Got it — I sent a 6-digit code to {phone}. Enter it here when it arrives.",
            _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=phone,
                requires_login_otp=True,
            ),
        )

    if step == GUEST_STEP_LOGIN_OTP:
        if wants_cancel_login(msg):
            return (
                "Okay — what would you like to do next?",
                _exit_login_ctx(session_ctx),
            )
        otp = extract_otp_code(msg)
        if not otp:
            phone = str(session_ctx.get("login_phone") or "your phone")
            return (
                f"Enter the 6-digit code we sent to {phone}.",
                _login_ctx(
                    session_ctx,
                    guest_step=GUEST_STEP_LOGIN_OTP,
                    login_phone=str(session_ctx.get("login_phone") or "") or None,
                    requires_login_otp=True,
                ),
            )
        return (
            "Perfect — signing you in now. One moment…",
            _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=str(session_ctx.get("login_phone") or "") or None,
                requires_login_otp=True,
                login_otp_token=otp,
            ),
        )

    if step in ("early_chat", "intro_declined"):
        return (
            "Sure — what's the phone number on your account?",
            _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE),
        )

    return None
