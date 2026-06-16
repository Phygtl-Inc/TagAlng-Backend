"""LOOKING/SHARING 4-phase voice-first cascade (LANA_INTENTS §2.3–2.4)."""

from __future__ import annotations

import re
from typing import Any

from app.layer1_intents import LOOKING_SHARING_INTENTS, SIGNAL_INTENT_BY_LINEAR, slots_linear_intent

PHASE_SIGNAL_EXTRACT = "signal_extract"
PHASE_SIGNAL_CONFIRM = "signal_confirm_missing"
PHASE_SIGNAL_LISTENING = "signal_listening"

_WHEN_HINT = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|weekend|weekday|daily|weekly|"
    r"today|tomorrow|am|pm|\d{1,2}\s*(?:am|pm))\b",
    re.I,
)
_AFFIRMATIVE = frozenset({"yes", "yeah", "yep", "sure", "ok", "okay", "correct", "right"})


def _has_when_hint(text: str) -> bool:
    return bool(_WHEN_HINT.search(str(text or "")))


def draft_from_slots(slots: dict[str, Any], *, msg: str) -> dict[str, Any]:
    linear = slots_linear_intent(slots) or "looking.swap"
    intent = SIGNAL_INTENT_BY_LINEAR.get(linear, "swap_seek")
    return {
        "linear_intent": linear,
        "intent": intent,
        "detail": str(slots.get("signal_detail") or msg or "").strip()[:500],
        "category": str(slots.get("signal_category") or "").strip() or None,
        "stage": str(slots.get("signal_stage") or "").strip() or None,
        "when_hint": str(slots.get("signal_when") or "").strip() or None,
        "phase": PHASE_SIGNAL_EXTRACT,
        "confirm_field": None,
    }


def needs_confirm(draft: dict[str, Any]) -> tuple[bool, str, str]:
    """Return (needs_confirm, field_name, prompt)."""
    intent = str(draft.get("intent") or "")
    detail = str(draft.get("detail") or "").strip()
    category = draft.get("category")
    when_hint = draft.get("when_hint") or detail

    if intent in ("swap_seek", "swap_offer"):
        if len(detail) < 8:
            return True, "detail", "Can you be a bit more specific — size, brand, or condition?"
        if not draft.get("stage") and not re.search(r"\b(\d+t|\d+\s*year|size\s*\d+)\b", detail, re.I):
            return True, "stage", "What size or stage — e.g. 3T, size 5, adult medium?"
    if intent in ("meet_seek", "host_meet"):
        if not _has_when_hint(str(when_hint)):
            return True, "when_hint", "When works for you — weekday morning, weekend, something else?"
    if intent in ("tip_seek", "tip_share"):
        if not category:
            return True, "category", "What category — health, food, activities, home, something else?"
    return False, "", ""


def apply_confirm_answer(draft: dict[str, Any], msg: str) -> dict[str, Any]:
    out = dict(draft)
    field = str(out.get("confirm_field") or "")
    text = str(msg or "").strip()[:500]
    if not text:
        return out
    if field == "detail":
        out["detail"] = text
    elif field == "stage":
        out["stage"] = text
        if text.lower() not in out["detail"].lower():
            out["detail"] = f"{out['detail']} ({text})".strip()
    elif field == "when_hint":
        out["when_hint"] = text
        out["detail"] = f"{out['detail']} — {text}".strip(" —")
    elif field == "category":
        out["category"] = text[:120]
    out["confirm_field"] = None
    out["phase"] = PHASE_SIGNAL_LISTENING
    return out


def draft_ready_to_save(draft: dict[str, Any]) -> bool:
    return bool(str(draft.get("detail") or "").strip()) and str(
        draft.get("phase") or ""
    ) in (PHASE_SIGNAL_LISTENING, PHASE_SIGNAL_EXTRACT)


def advance_signal_draft(
    draft: dict[str, Any],
    *,
    msg: str,
) -> tuple[dict[str, Any], str | None, bool]:
    """
    Advance cascade. Returns (updated_draft, confirm_prompt, ready_to_save).
    """
    phase = str(draft.get("phase") or PHASE_SIGNAL_EXTRACT)
    if phase == PHASE_SIGNAL_CONFIRM:
        updated = apply_confirm_answer(draft, msg)
        need, field, prompt = needs_confirm(updated)
        if need:
            updated["phase"] = PHASE_SIGNAL_CONFIRM
            updated["confirm_field"] = field
            return updated, prompt, False
        updated["phase"] = PHASE_SIGNAL_LISTENING
        return updated, None, True

    need, field, prompt = needs_confirm(draft)
    if need:
        out = dict(draft)
        out["phase"] = PHASE_SIGNAL_CONFIRM
        out["confirm_field"] = field
        return out, prompt, False

    out = dict(draft)
    out["phase"] = PHASE_SIGNAL_LISTENING
    return out, None, True


def is_signal_lane_intent(slots: dict[str, Any]) -> bool:
    linear = slots_linear_intent(slots)
    return linear in LOOKING_SHARING_INTENTS if linear else False


def is_affirmative_save(msg: str) -> bool:
    return str(msg or "").strip().lower() in _AFFIRMATIVE
