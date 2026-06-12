"""Flash extraction for discovery funnel slots and goals (replaces regex identity/heuristics)."""

from __future__ import annotations

import os
import re
from typing import Any

from app.orchestrator.llm import llm_configured, llm_json
from app.turn_timing import TurnTimer

_ZIP_IN_TEXT = re.compile(r"\b(\d{5})\b")

_SYSTEM = (
    "You are the ONLY router for TagAlng Lana discovery vs chat on each user message. "
    "Output only valid JSON. "
    "Discovery funnel = ZIP, giving self-description for matching, preview matches, verify phone, RSVP. "
    "When routing_phase=listening and user wants to meet/find/show/connect with neighbors or people "
    "(any phrasing, including British 'neighbours') → goal=peers, in_discovery=true. "
    "When user is frustrated and demands to see people/users/neighbors on their block → goal=peers, in_discovery=true. "
    "Non-funnel chat = goal chat or none, in_discovery=false — companionship AI answers (profile questions, "
    "what are my claims, what's my name, random questions, meta, off-topic). "
    "identity_snippet = self-description for matching from the latest message OR synthesized from RECENT TURNS "
    "when routing_phase=need_identity or when the latest message is ZIP-only but user already described themselves earlier. "
    "Never set identity_snippet from questions or meta. "
    "When routing_phase=need_identity: user answering the identity step (even one word like 'British') → "
    "goal=continue, in_discovery=true; set identity_snippet from their answer enriched with RECENT TURNS if helpful. "
    "If the user only sent a ZIP code with no prior self-description in RECENT TURNS, identity_snippet must be null. "
    "Mid-funnel pushback or topic change in preview → in_discovery=false, goal=chat. "
    "When routing_phase=preview: questions about the neighbors shown (e.g. 'do you have Brazilian moms?', "
    "'are these Brazilian?', 'why moms not dads?') → in_discovery=false, goal=chat — NOT peers. "
    "Never set identity_snippet from questions — only from new self-description. "
    "Pushback or frustration about match quality → goal=chat. "
    "Only goal=peers + in_discovery=true in preview when user gives NEW self-description for matching "
    "(different identity_snippet than session) and explicitly wants a fresh search — not for questions. "
    "When routing_phase=preview and phone_verified=false: user wants to sign up, create an account, "
    "join, get verified, see names, connect, or complete registration "
    "(e.g. 'sign me up', 'did you sign me up', 'I want to join', 'how do I verify') "
    "→ goal=verify, in_discovery=true — discovery code will collect phone, NOT profile chat. "
    "Do NOT classify signup/verify intent as goal=chat. "
    "If phone_verified=true, signup/verify requests → goal=chat (already verified). "
    "goal: peers = find/show neighbors; activities = browse events; both; verify = phone signup gate; rsvp; "
    "chat = companionship / profile read / any non-funnel question; "
    "continue = user is answering the current funnel step (supplying ZIP or identity snippet); "
    "none = not discovery."
)


def discovery_ai_enabled() -> bool:
    flag = os.environ.get("LANA_DISCOVERY_AI_SLOTS", "1").strip().lower()
    return flag not in ("0", "false", "off") and llm_configured()


def _extract_model() -> str:
    return os.environ.get("VERTEX_EXTRACT_MODEL", "gemini-2.5-flash")


def _format_history(history: list[dict[str, Any]] | None, *, limit: int = 8) -> str:
    if not history:
        return "(none)"
    lines: list[str] = []
    for turn in history[-limit:]:
        role = str(turn.get("role") or "user")
        content = str(turn.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "(none)"


def _empty_slots() -> dict[str, Any]:
    return {
        "in_discovery": False,
        "goal": "none",
        "zip": None,
        "identity_snippet": None,
        "confidence": 0.0,
    }


def ai_parse_discovery_turn(
    utterance: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool = False,
    timer: TurnTimer | None = None,
) -> dict[str, Any]:
    """One Flash call: discovery yes/no, goal (peers/activities), zip, identity snippet."""
    if not discovery_ai_enabled():
        return _empty_slots()
    text = str(utterance or "").strip()
    if not text:
        return _empty_slots()
    try:
        attempts_box: list[int] = []
        if timer:
            with timer.stage("llm_discovery_slots"):
                raw = llm_json(
                    model=_extract_model(),
                    system=_SYSTEM,
                    user_payload=_discovery_slot_payload(
                        text,
                        routing_phase=routing_phase,
                        history=history,
                        has_block=has_block,
                        has_identity=has_identity,
                        phone_verified=phone_verified,
                    ),
                    max_tokens=128,
                    temperature=0.0,
                    llm_attempts=attempts_box,
                )
            if attempts_box:
                timer.set_count("llm_discovery_slots_attempts", attempts_box[0])
        else:
            raw = llm_json(
                model=_extract_model(),
                system=_SYSTEM,
                user_payload=_discovery_slot_payload(
                    text,
                    routing_phase=routing_phase,
                    history=history,
                    has_block=has_block,
                    has_identity=has_identity,
                    phone_verified=phone_verified,
                ),
                max_tokens=128,
                temperature=0.0,
            )
        goal = str(raw.get("goal") or "none").lower()
        if goal not in (
            "peers",
            "activities",
            "both",
            "verify",
            "rsvp",
            "chat",
            "continue",
            "none",
        ):
            goal = "none"
        zip_val = raw.get("zip")
        zip_s = str(zip_val).strip() if zip_val else None
        if zip_s:
            m = _ZIP_IN_TEXT.search(zip_s)
            zip_s = m.group(1) if m else None
        ident = raw.get("identity_snippet")
        ident_s = str(ident).strip()[:400] if ident else None
        return {
            "in_discovery": bool(raw.get("in_discovery")),
            "goal": goal,
            "zip": zip_s,
            "identity_snippet": ident_s,
            "confidence": float(raw.get("confidence", 0.0)),
        }
    except Exception:
        return _empty_slots()


def _discovery_slot_payload(
    text: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool,
) -> str:
    return (
        f"routing_phase: {routing_phase or 'listening'}\n"
        f"has_block: {has_block}\n"
        f"has_identity_in_session: {has_identity}\n"
        f"phone_verified: {phone_verified}\n\n"
        "RECENT TURNS:\n"
        f"{_format_history(history)}\n\n"
        f"LATEST USER MESSAGE:\n{text}\n\n"
        "Return JSON:\n"
        "{\n"
        '  "in_discovery": true|false,\n'
        '  "goal": "peers"|"activities"|"both"|"verify"|"rsvp"|"chat"|"continue"|"none",\n'
        '  "zip": "5-digit string or null",\n'
        '  "identity_snippet": "string or null",\n'
        '  "confidence": 0.0-1.0\n'
        "}"
    )


def discovery_slots_for_turn(
    session_ctx: dict[str, Any],
    utterance: str,
    *,
    routing_phase: str,
    history: list[dict[str, Any]] | None,
    has_block: bool,
    has_identity: bool,
    phone_verified: bool = False,
    timer: TurnTimer | None = None,
) -> dict[str, Any]:
    """Parse discovery slots once per user message; reuse within the same turn."""
    text = str(utterance or "").strip()
    cache_key = str(session_ctx.get("_discovery_slots_for") or "")
    cached = session_ctx.get("_discovery_slots")
    if text and cache_key == text and isinstance(cached, dict):
        return cached
    slots = ai_parse_discovery_turn(
        text,
        routing_phase=routing_phase,
        history=history,
        has_block=has_block,
        has_identity=has_identity,
        phone_verified=phone_verified,
        timer=timer,
    )
    if text:
        session_ctx["_discovery_slots"] = slots
        session_ctx["_discovery_slots_for"] = text
    return slots


def slots_want_preview_refetch(
    slots: dict[str, Any],
    session_ctx: dict[str, Any],
) -> bool:
    """AI-only: re-run peer preview when user supplied new matching criteria (not questions)."""
    goal = str(slots.get("goal") or "none")
    if goal not in ("peers", "both") or not slots.get("in_discovery"):
        return False
    if float(slots.get("confidence", 0.0)) < 0.5:
        return False
    raw = slots.get("identity_snippet")
    if not raw:
        return False
    new_sn = str(raw).strip()[:400]
    if not new_sn:
        return False
    stored = str(session_ctx.get("identity_snippet") or "").strip()
    return not stored or new_sn.lower() != stored.lower()


def slots_want_discovery_handling(
    slots: dict[str, Any],
    *,
    routing_phase: str = "",
) -> bool:
    """AI decision: should discovery code handle this turn (not orchestrator)?"""
    goal = str(slots.get("goal") or "none")
    if goal in ("chat", "none"):
        return False
    conf = float(slots.get("confidence", 0.0))
    if goal in ("peers", "activities", "both", "verify", "rsvp"):
        if slots.get("in_discovery"):
            return conf >= 0.5
        return conf >= 0.65
    if goal == "continue":
        phase = routing_phase or "listening"
        if phase == "preview":
            # Follow-ups in preview (pushback, clarify) → orchestrator with peer context
            return False
        if phase in ("need_zip", "need_identity", "need_display_name"):
            return True
        return slots.get("in_discovery") and conf >= 0.6
    return False


def ai_wants_discovery(
    utterance: str,
    *,
    history: list[dict[str, Any]] | None = None,
    routing_phase: str = "",
) -> bool:
    slots = ai_parse_discovery_turn(
        utterance,
        routing_phase=routing_phase,
        history=history,
        has_block=False,
        has_identity=False,
    )
    return slots_want_discovery_handling(slots, routing_phase=routing_phase)
