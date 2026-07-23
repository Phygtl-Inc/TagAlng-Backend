"""In-chat returning-user login during Meet Lana (early_chat)."""

from __future__ import annotations

import logging
import re
from typing import Any

_LOG = logging.getLogger(__name__)

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
        "auth_action": None,
    }
    out["login_phone"] = None  # None, not pop — a popped key resurrects from the stored ctx on merge
    out["login_email_attempts"] = None
    out["login_otp_attempts"] = None
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
    out["login_phone"] = None  # None, not pop — a popped key resurrects from the stored ctx on merge
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
    out["login_phone"] = None  # None, not pop — a popped key resurrects from the stored ctx on merge
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


def _send_otp_action(email: str) -> dict[str, Any]:
    """FE instruction: send (or resend) the login OTP to this address."""
    return {"type": "send_login_otp", "email": email, "verify_type": "email"}


def _verify_otp_action(email: str, token: str) -> dict[str, Any]:
    """FE instruction: verify this login OTP for this address."""
    return {
        "type": "verify_login_otp",
        "email": email,
        "token": token,
        "verify_type": "email",
    }


def _interpret_fallback(msg: str) -> dict[str, Any]:
    """No-LLM read of a login-flow reply, from the format regexes alone."""
    if wants_cancel_login(msg):
        return {"action": "cancel", "email": None, "code": None}
    email = extract_email(msg)
    code = extract_otp_code(msg)
    if email:
        return {"action": "email", "email": email, "code": code}
    if code:
        return {"action": "code", "email": None, "code": code}
    return {"action": "other", "email": None, "code": None}


def interpret_login_reply(
    msg: str,
    *,
    expecting: str,
    known_email: str | None = None,
) -> dict[str, Any]:
    """AI-first read of a sign-in turn: what is the user doing?

    The AI owns the intent verdict — give a code / give or correct an email /
    ask for a resend / bail out / something else — so a user can say "I typed
    the wrong email, use X" or cancel in any language (the cancel word-list is
    English-only). The format regexes validate whatever it returns and are the
    whole fallback when no LLM is configured.

    Returns ``{"action": "code"|"email"|"resend"|"cancel"|"other",
    "email": str|None, "code": str|None}``.
    """
    fallback = _interpret_fallback(msg)
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return fallback
        asked = (
            f"the 6-digit code they were sent at {known_email}"
            if expecting == "code" and known_email
            else "the 6-digit code they were sent"
            if expecting == "code"
            else "the email address on their account"
        )
        data = llm_json(
            model=router_model(),
            system=(
                "You read ONE user message from an email sign-in chat and decide "
                f"what the user is doing. Lana just asked them for {asked}. The "
                "user may write in ANY language. Set action to exactly one of: "
                "'code' — the message carries the 6-digit verification code (also return it in code); "
                "'email' — it gives an email address to use, new or corrected (also return it in email, lowercased); "
                "'resend' — they want the code sent again / say it never arrived, without giving an address; "
                "'cancel' — they want to stop signing in or do something else instead; "
                "'other' — anything else (questions, chatter, an answer that fits none of these). "
                'Return JSON {"action": "...", "email": "...or null", "code": "...or null"}.'
            ),
            user_payload=str(msg or "").strip()[:600],
            max_tokens=80,
            temperature=0.0,
        )
        if not isinstance(data, dict):
            return fallback
        action = str(data.get("action") or "").strip().lower()
        # The regexes validate the AI's extractions — a malformed address or
        # code is re-read from the message itself, never trusted as returned.
        email = extract_email(str(data.get("email") or "")) or extract_email(msg)
        code = extract_otp_code(str(data.get("code") or "")) or extract_otp_code(msg)
        if action == "email" and not email:
            action = "other"
        if action == "code" and not code:
            action = "other"
        if action not in ("code", "email", "resend", "cancel", "other"):
            return fallback
        return {"action": action, "email": email, "code": code}
    except Exception:  # noqa: BLE001 — a failed read degrades to the regexes
        _LOG.exception("login_reply_interpret_failed")
        return fallback


def compose_offscript_reply(*, goal: str, facts: list[str], fallback: str) -> str:
    """One short Lana line AI-authored from true facts, for a turn that went
    off-script mid-verification (a question, chatter, an answer that fits
    nothing) — so she responds to what the user actually said instead of
    repeating a canned re-prompt. The static line is the no-LLM fallback only.
    English-canonical; the final-mile localizer renders the session language."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return fallback
        data = llm_json(
            model=synthesizer_model(),
            system=(
                "You are Lana, a warm neighborhood concierge, in the middle of an "
                f"email verification with a user. {goal} Ground the reply ONLY in "
                "the facts given — one or two short sentences, warm and casual, "
                'never robotic. Return JSON {"message": "..."}.'
            ),
            user_payload="\n".join(f"- {f}" for f in facts),
            max_tokens=120,
            temperature=0.4,
        )
        msg = str((data or {}).get("message") or "").strip() if isinstance(data, dict) else ""
        return msg or fallback
    except Exception:  # noqa: BLE001 — the static line beats a failed turn
        _LOG.exception("login_offscript_compose_failed")
        return fallback


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
        # One-shot FE instruction — cleared here so a stale send/verify from a
        # prior turn never re-fires; action turns stamp a fresh one after.
        "auth_action": None,
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
        read = interpret_login_reply(msg, expecting="email")
        if read["action"] == "cancel":
            return (
                "No problem — what would you like to do? Find neighbors, plan something, or tell me about yourself.",
                _exit_login_ctx(session_ctx),
            )
        email = read["email"]
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
            reply = compose_offscript_reply(
                goal=(
                    "The user replied with something that isn't an email address. "
                    "Respond briefly to what they actually said, then ask again for "
                    "the email on their account — and mention they can say stop to "
                    "do this later."
                ),
                facts=[
                    f'They said: "{msg[:300]}"',
                    "You asked for the email address on their account to sign them in",
                    "They can also say stop to sign in later",
                ],
                fallback="I didn't catch a valid email — something like you@example.com.",
            )
            return (reply, ctx)
        ctx = _login_ctx(
            session_ctx,
            guest_step=GUEST_STEP_LOGIN_OTP,
            login_phone=email,
            requires_login_otp=True,
        )
        ctx["login_email_attempts"] = None  # valid email — reset (None, not pop: merge)
        ctx["auth_action"] = _send_otp_action(email)
        return (
            f"Got it — I'm sending a 6-digit code to {email}. Enter it here when it arrives.",
            ctx,
        )

    if step == GUEST_STEP_LOGIN_OTP:
        current = str(session_ctx.get("login_phone") or "").strip().lower()
        read = interpret_login_reply(
            msg, expecting="code", known_email=current or None
        )
        action = read["action"]
        if action == "cancel":
            return (
                "Okay — what would you like to do next?",
                _exit_login_ctx(session_ctx),
            )
        if action == "email" and read["email"] and read["email"] != current:
            # Correcting the address mid-flow — switch to it and send there,
            # instead of re-prompting for a code sent to the wrong inbox.
            email = read["email"]
            ctx = _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=email,
                requires_login_otp=True,
            )
            ctx["login_otp_attempts"] = None  # fresh address — reset (None, not pop: merge)
            ctx["auth_action"] = _send_otp_action(email)
            return (
                f"Okay — I'm sending the code to {email} instead. Enter it here when it arrives.",
                ctx,
            )
        if action == "resend" or (action == "email" and read["email"] == current):
            if not current:
                return (
                    "Sure — what's the email on your account?",
                    _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE),
                )
            ctx = _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=current,
                requires_login_otp=True,
            )
            ctx["auth_action"] = _send_otp_action(current)
            return (
                f"On it — I'm sending a fresh code to {current}. Enter it here when it arrives.",
                ctx,
            )
        otp = read["code"]
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
            email = current or "your email"
            ctx = _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=current or None,
                requires_login_otp=True,
            )
            ctx["login_otp_attempts"] = attempts
            reply = compose_offscript_reply(
                goal=(
                    "The user replied with something that isn't the verification "
                    "code. Respond briefly to what they actually said, then remind "
                    "them you need the 6-digit code you sent — and that they can "
                    "give a different email, ask for a resend, or say stop."
                ),
                facts=[
                    f'They said: "{msg[:300]}"',
                    f"A 6-digit sign-in code was sent to {email}",
                    "They can give a different email, ask you to resend it, or say stop",
                ],
                fallback=(
                    f"Enter the 6-digit code I sent to {email} — or give me a "
                    "different email to use."
                ),
            )
            return (reply, ctx)
        ctx = _login_ctx(
            session_ctx,
            guest_step=GUEST_STEP_LOGIN_OTP,
            login_phone=current or None,
            requires_login_otp=True,
            login_otp_token=otp,
        )
        ctx["login_otp_attempts"] = None  # real code entered — reset (None, not pop: merge)
        if current:
            ctx["auth_action"] = _verify_otp_action(current, otp)
        return ("Perfect — signing you in now. One moment…", ctx)

    if step in ("early_chat", "intro_declined"):
        # If the user already gave their email this turn, send the code straight away
        # instead of re-asking and throwing the email they just typed away.
        email = extract_email(msg)
        if email:
            ctx = _login_ctx(
                session_ctx,
                guest_step=GUEST_STEP_LOGIN_OTP,
                login_phone=email,
                requires_login_otp=True,
            )
            ctx["auth_action"] = _send_otp_action(email)
            return (
                f"Got it — I'm sending a 6-digit code to {email}. Enter it here when it arrives.",
                ctx,
            )
        return (
            "Sure — what's the email on your account?",
            _login_ctx(session_ctx, guest_step=GUEST_STEP_LOGIN_PHONE),
        )

    return None
