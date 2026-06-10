"""In-chat guest onboarding: joint moment → intro name → phone → rest of profile."""

from __future__ import annotations

import re
from typing import Any

from app.profile_intake import collect_profile_buckets, lana_profile_turn
from app.supabase_rpc import call_rpc

GUEST_STEP_EARLY = "early_chat"
GUEST_STEP_OFFERED = "offered_intro"
GUEST_STEP_INTRO_NAME = "awaiting_intro_name"
GUEST_STEP_PHONE = "await_phone"
GUEST_STEP_POST_VERIFY = "post_verify"
GUEST_STEP_DECLINED = "intro_declined"

_HERITAGE_RE = re.compile(
    r"\b(latino|latina|latinx|hispanic|brazil(?:ian)?|mexican|puerto\s*rican|"
    r"cuban|colombian|heritage|from\s+\w+|roots?\s+in)\b",
    re.I,
)
_STAGE_RE = re.compile(
    r"\b(mom|mother|mama|dad|father|parent|kids?|children|toddler|baby|babies|"
    r"married|single\s+mom|new\s+mom|family)\b",
    re.I,
)
_INTEREST_RE = re.compile(
    r"\b(looking\s+for|hope\s+to|want\s+to|meet|friends?|activities|get\s+together)\b",
    re.I,
)
_AFFIRMATIVE_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok(?:ay)?|please|introduce|sounds?\s+good|"
    r"let'?s\s+do\s+it|definitely|absolutely|go\s+ahead)\b",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"^(no|nah|nope|not\s+now|later|skip|pass|maybe\s+later|not\s+yet)\b",
    re.I,
)
_NAME_PATTERNS = (
    re.compile(r"(?:call\s+me|i'?m|name(?:'s| is))\s+([A-Za-z][A-Za-z\s'-]{0,28})", re.I),
    re.compile(r"^([A-Za-z][A-Za-z'-]{0,28})$"),
)


def parse_yes_no(text: str) -> bool | None:
    s = str(text or "").strip()
    if not s:
        return None
    if _AFFIRMATIVE_RE.search(s):
        return True
    if _NEGATIVE_RE.search(s):
        return False
    return None


def extract_intro_name(text: str) -> str | None:
    s = str(text or "").strip()
    if not s:
        return None
    for pat in _NAME_PATTERNS:
        m = pat.search(s)
        if m:
            name = m.group(1).strip().title()
            if name.lower() not in ("yes", "no", "ok", "okay", "sure"):
                return name[:30]
    return None


def _user_text(history: list[dict[str, Any]], extra: str = "") -> str:
    parts = [str(m.get("content") or "") for m in history if m.get("role") == "user"]
    if extra:
        parts.append(extra)
    return " ".join(parts)


def has_joint_moment_signals(history: list[dict[str, Any]], user_message: str) -> bool:
    text = _user_text(history, user_message)
    has_heritage = bool(_HERITAGE_RE.search(text))
    has_thread = bool(_STAGE_RE.search(text)) or bool(_INTEREST_RE.search(text))
    buckets = collect_profile_buckets(history=history, ui={}, topics_covered=[])
    if "heritage" in buckets:
        has_heritage = True
    if buckets & {"stage", "interest", "activity", "vicinity"}:
        has_thread = True
    return has_heritage and has_thread


def _candidate_nickname(jm: dict[str, Any]) -> str:
    card = jm.get("candidate") if isinstance(jm.get("candidate"), dict) else {}
    return str(card.get("nickname") or "Maria")


def _offer_message(jm: dict[str, Any]) -> str:
    copy = str(jm.get("lana_copy") or "").strip()
    if copy:
        return copy
    nick = _candidate_nickname(jm)
    return (
        f"{nick} told me she's looking for neighbors like you on the block. "
        f"Want me to put you two together?"
    )


def fetch_joint_moment(user_jwt: str, session_id: str) -> dict[str, Any]:
    raw = call_rpc(
        user_jwt,
        "get_joint_moment_candidate",
        {"p_session_id": session_id},
    )
    return raw if isinstance(raw, dict) else {}


def accept_joint_moment(user_jwt: str, joint_moment_id: str) -> dict[str, Any]:
    raw = call_rpc(
        user_jwt,
        "respond_joint_moment",
        {"p_joint_moment_id": joint_moment_id, "p_action": "accept"},
    )
    return raw if isinstance(raw, dict) else {}


def decline_joint_moment(user_jwt: str, joint_moment_id: str) -> dict[str, Any]:
    raw = call_rpc(
        user_jwt,
        "respond_joint_moment",
        {"p_joint_moment_id": joint_moment_id, "p_action": "decline"},
    )
    return raw if isinstance(raw, dict) else {}


def _ui_joint_moment() -> dict[str, Any]:
    return {
        "bucket": "interest",
        "focus_phrase": None,
        "highlights": [],
    }


def _merge_guest_ctx(
    session_ctx: dict[str, Any],
    *,
    guest_step: str,
    joint_moment: dict[str, Any] | None = None,
    intro_name: str | None = None,
    requires_phone: bool = False,
) -> dict[str, Any]:
    out = {**session_ctx, "guest_intake": True, "guest_step": guest_step}
    if joint_moment is not None:
        out["joint_moment"] = joint_moment
        if joint_moment.get("joint_moment_id"):
            out["joint_moment_id"] = joint_moment["joint_moment_id"]
    if intro_name:
        out["intro_name"] = intro_name
        out["display_name_saved"] = True
        out["profile_patch"] = {"nickname": intro_name}
    out["requires_phone_verification"] = requires_phone
    return out


def lana_profile_guest_turn(
    *,
    user_block: str,
    history: list[dict[str, Any]],
    user_message: str,
    session_ctx: dict[str, Any],
    session_id: str,
    user_jwt: str,
    phone_verified: bool,
    ctx_pack: dict[str, Any] | None = None,
    timer: Any | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Guest profile intake with in-chat joint moment → name → phone → rest."""
    step = str(session_ctx.get("guest_step") or GUEST_STEP_EARLY)
    joint_moment: dict[str, Any] | None = session_ctx.get("joint_moment")
    jm_id = session_ctx.get("joint_moment_id")

    # --- offered_intro: yes / no ---
    if step == GUEST_STEP_OFFERED and jm_id:
        choice = parse_yes_no(user_message)
        nick = _candidate_nickname(joint_moment or {})
        if choice is True:
            accept_joint_moment(user_jwt, str(jm_id))
            reply = f"Love it! What should {nick} call you when I introduce you?"
            ctx = _merge_guest_ctx(session_ctx, guest_step=GUEST_STEP_INTRO_NAME)
            return reply, "continue", ctx, _ui_joint_moment(), joint_moment
        if choice is False:
            decline_joint_moment(user_jwt, str(jm_id))
            reply = (
                "No problem — we can keep exploring your block. "
                "What are you hoping to find here?"
            )
            ctx = _merge_guest_ctx(session_ctx, guest_step=GUEST_STEP_DECLINED)
            ctx.pop("joint_moment", None)
            ctx.pop("joint_moment_id", None)
            return reply, "continue", ctx, _ui_joint_moment(), None
        reply = f"Just say yes if you'd like an intro to {nick}, or no to keep exploring."
        ctx = _merge_guest_ctx(session_ctx, guest_step=GUEST_STEP_OFFERED)
        return reply, "continue", ctx, _ui_joint_moment(), joint_moment

    # --- awaiting_intro_name ---
    if step == GUEST_STEP_INTRO_NAME:
        name = extract_intro_name(user_message)
        nick = _candidate_nickname(joint_moment or {})
        if not name:
            reply = f"What should {nick} call you? Just your first name is fine."
            ctx = _merge_guest_ctx(session_ctx, guest_step=GUEST_STEP_INTRO_NAME)
            return reply, "continue", ctx, _ui_joint_moment(), joint_moment
        reply = (
            f"Perfect, {name}! Before I introduce you to {nick}, "
            "verify your phone — use the button below to enter your number."
        )
        ctx = _merge_guest_ctx(
            session_ctx,
            guest_step=GUEST_STEP_PHONE,
            intro_name=name,
            requires_phone=True,
        )
        return reply, "continue", ctx, _ui_joint_moment(), joint_moment

    # --- await_phone ---
    if step == GUEST_STEP_PHONE:
        if phone_verified:
            reply = (
                "You're verified! Quick one — how many kids do you have, "
                "and what are their ages?"
            )
            ctx = _merge_guest_ctx(session_ctx, guest_step=GUEST_STEP_POST_VERIFY)
            ctx["requires_phone_verification"] = False
            return reply, "continue", ctx, _ui_joint_moment(), joint_moment
        reply = (
            "Almost there — verify your phone with the button below, "
            "then send me a quick message and we'll keep going."
        )
        ctx = _merge_guest_ctx(
            session_ctx,
            guest_step=GUEST_STEP_PHONE,
            requires_phone=True,
        )
        return reply, "continue", ctx, _ui_joint_moment(), joint_moment

    # --- early_chat: offer joint moment when signals are strong enough ---
    if step in (GUEST_STEP_EARLY, GUEST_STEP_DECLINED) and not jm_id:
        if has_joint_moment_signals(history, user_message):
            jm = fetch_joint_moment(user_jwt, session_id)
            if jm.get("joint_moment_id"):
                reply = _offer_message(jm)
                ctx = _merge_guest_ctx(
                    session_ctx,
                    guest_step=GUEST_STEP_OFFERED,
                    joint_moment=jm,
                )
                return reply, "continue", ctx, _ui_joint_moment(), jm

    # --- LLM path (early_chat, intro_declined, post_verify) ---
    merged_ctx = {**(session_ctx or {}), "guest_intake": True, "guest_step": step}
    reply, status, turn_ctx, ui_raw = lana_profile_turn(
        user_block,
        history,
        user_message,
        ctx_pack=ctx_pack,
        session_ctx=merged_ctx,
        timer=timer,
    )
    turn_ctx["guest_intake"] = True
    turn_ctx["guest_step"] = step

    if step == GUEST_STEP_POST_VERIFY:
        turn_ctx["requires_phone_verification"] = False
    elif step == GUEST_STEP_PHONE:
        turn_ctx["requires_phone_verification"] = True

    if step in (GUEST_STEP_EARLY, GUEST_STEP_POST_VERIFY, GUEST_STEP_DECLINED):
        if status == "ready_to_complete" and step != GUEST_STEP_POST_VERIFY:
            status = "continue"
            if "complete" in reply.lower():
                reply = reply.split("When you're ready")[0].strip() or reply

    if joint_moment and turn_ctx.get("joint_moment_id") is None:
        turn_ctx["joint_moment"] = joint_moment
        turn_ctx["joint_moment_id"] = joint_moment.get("joint_moment_id")

    return reply, status, turn_ctx, ui_raw, joint_moment
