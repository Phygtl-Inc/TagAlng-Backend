"""Flash extraction for discovery funnel slots and goals (replaces regex identity/heuristics)."""

from __future__ import annotations

import os
import re
from typing import Any

from app.orchestrator.llm import llm_configured, llm_json

_ZIP_IN_TEXT = re.compile(r"\b(\d{5})\b")

_SYSTEM = (
    "You extract discovery funnel slots for TagAlng Lana. "
    "Output only valid JSON. "
    "Decide whether THIS message should be handled by the discovery funnel (ZIP, identity, matches) "
    "vs normal AI chat (companionship). "
    "identity_snippet is ONLY explicit self-description (heritage, life stage, what they want) — "
    "never greetings, small talk, or questions to Lana (e.g. 'how are you', 'are you real', 'are you dumb'). "
    "If the user only sent a ZIP code, identity_snippet must be null. "
    "When routing_phase is need_zip or need_identity and the user asks about Lana, pushes back, "
    "or changes topic (e.g. 'are you real', 'what??', 'no I asked something else'), "
    "set in_discovery=false and goal=chat. "
    "goal: peers = find/show neighbors; activities = browse events on block; "
    "both = peers and activities; verify = how to verify phone or unlock full matches; "
    "rsvp = join/attend a specific event; chat = small talk, confusion, frustration, meta questions; "
    "continue = answering the current funnel step (ZIP or identity); "
    "none = not discovery (general companionship)."
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
) -> dict[str, Any]:
    """One Flash call: discovery yes/no, goal (peers/activities), zip, identity snippet."""
    if not discovery_ai_enabled():
        return _empty_slots()
    text = str(utterance or "").strip()
    if not text:
        return _empty_slots()
    try:
        raw = llm_json(
            model=_extract_model(),
            system=_SYSTEM,
            user_payload=(
                f"routing_phase: {routing_phase or 'listening'}\n"
                f"has_block: {has_block}\n"
                f"has_identity_in_session: {has_identity}\n\n"
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
            ),
            max_tokens=256,
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
        if phase in ("need_zip", "need_identity", "preview"):
            return slots.get("in_discovery") and conf >= 0.5
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
