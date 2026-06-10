"""In-chat returning-user login during Meet Lana (early_chat)."""

from __future__ import annotations

import re
from typing import Any

GUEST_STEP_LOGIN_PHONE = "await_login_phone"
GUEST_STEP_LOGIN_OTP = "await_login_otp"

_LOGIN_INTENT_RE = re.compile(
    r"\b(log\s*in|login|sign\s*in|signin|existing\s+account|already\s+have\s+(?:an?\s+)?account|"
    r"i\s+have\s+an\s+account|returning\s+user)\b",
    re.I,
)
_CANCEL_RE = re.compile(r"\b(never\s*mind|cancel|sign\s*up|new\s+account|meet\s+lana)\b", re.I)
_OTP_RE = re.compile(r"\b(\d{6})\b")
_PHONE_DIGITS_RE = re.compile(r"\+?[\d\s().-]{10,18}")


def wants_login(text: str) -> bool:
    return bool(_LOGIN_INTENT_RE.search(str(text or "").strip()))


def wants_cancel_login(text: str) -> bool:
    return bool(_CANCEL_RE.search(str(text or "").strip()))


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
                "No problem — tell me your life stage and what you're hoping to find on the block.",
                {**session_ctx, "guest_intake": True, "guest_step": "early_chat", "auth_intent": None},
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
                "Okay — we can start fresh. Who are you, and what are you hoping to find here?",
                {**session_ctx, "guest_intake": True, "guest_step": "early_chat", "auth_intent": None},
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

    if step in ("early_chat", "intro_declined") and wants_login(msg):
        return (
            "Sure — what's the phone number on your account?",
            _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE),
        )

    return None
