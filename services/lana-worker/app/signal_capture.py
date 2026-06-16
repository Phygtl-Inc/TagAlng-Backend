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
_KIDS_GEAR_RE = re.compile(
    r"\b(?:boots?|stroller|car\s*seat|crib|onesie|diaper|highchair|"
    r"rain\s*boots?|\d+t\b|\d+\s*year|infant|toddler|baby|newborn)\b",
    re.I,
)
_GENERAL_SWAP_ITEM_RE = re.compile(
    r"\b(?:laptop|computer|phone|tablet|furniture|chair|desk|sofa|"
    r"microwave|printer|monitor|ipad|macbook|hp|dell|lenovo|condition|"
    r"bicycl\w*|bike|scooter|wagon|tricycle)\b",
    re.I,
)
_SIZE_IN_DETAIL_RE = re.compile(
    r"\b(\d+t|\d+\s*year|size\s*\d+|\d+\s*(?:\"|in|inch)|"
    r"xs|s|m|l|xl|xxl|small|medium|large|adult)\b",
    re.I,
)
_SIZE_ANSWER_RE = re.compile(
    r"^\s*(?:\d+t|\d+\s*year(?:s| old)?|\d+\s*(?:\"|in|inch)?|size\s*\d+|"
    r"xs|s|m|l|xl|xxl|small|medium|large|adult|newborn|infant|toddler)\s*\.?!?\s*$",
    re.I,
)
_TOPIC_CHANGE_RE = re.compile(
    r"\b(?:buy|wanna|want to|looking for|need a|also looking|do you know|"
    r"buddy|teacher|tutor|pizza|swap|borrow|offer|host|bicycl\w*|bike)\b",
    re.I,
)
_AFFIRMATIVE = frozenset({"yes", "yeah", "yep", "sure", "ok", "okay", "correct", "right"})


def _has_when_hint(text: str) -> bool:
    return bool(_WHEN_HINT.search(str(text or "")))


def draft_from_slots(slots: dict[str, Any], *, msg: str) -> dict[str, Any]:
    linear = slots_linear_intent(slots) or "looking.swap"
    intent = SIGNAL_INTENT_BY_LINEAR.get(linear, "swap_seek")
    detail = str(slots.get("signal_detail") or msg or "").strip()[:500]
    category = str(slots.get("signal_category") or "").strip() or None
    if intent in ("tip_seek", "tip_share") and not category:
        category = _infer_tip_category(detail)
    return {
        "linear_intent": linear,
        "intent": intent,
        "detail": detail,
        "category": category,
        "stage": str(slots.get("signal_stage") or "").strip() or None,
        "when_hint": str(slots.get("signal_when") or "").strip() or None,
        "phase": PHASE_SIGNAL_EXTRACT,
        "confirm_field": None,
    }


def _swap_needs_kids_stage(detail: str) -> bool:
    if _GENERAL_SWAP_ITEM_RE.search(detail):
        return False
    return bool(_KIDS_GEAR_RE.search(detail))


def _looks_like_size_answer(text: str) -> bool:
    t = str(text or "").strip()
    if not t or len(t) > 32:
        return False
    if _TOPIC_CHANGE_RE.search(t):
        return False
    if _SIZE_ANSWER_RE.match(t):
        return True
    return bool(_SIZE_IN_DETAIL_RE.search(t) and len(t.split()) <= 4)


def _infer_tip_category(detail: str) -> str | None:
    low = str(detail or "").lower()
    if re.search(r"\b(teacher|tutor|school|lesson|math|reading)\b", low):
        return "education"
    if re.search(r"\b(pizza|restaurant|food|cafe|coffee|bakery)\b", low):
        return "food"
    if re.search(r"\b(pediatrician|doctor|dentist|therapist|clinic)\b", low):
        return "health"
    if re.search(r"\b(plumber|electrician|contractor|repair)\b", low):
        return "home"
    return None


def needs_confirm(draft: dict[str, Any]) -> tuple[bool, str, str]:
    """Return (needs_confirm, field_name, prompt)."""
    intent = str(draft.get("intent") or "")
    detail = str(draft.get("detail") or "").strip()
    category = draft.get("category")
    when_hint = draft.get("when_hint") or detail

    if intent in ("swap_seek", "swap_offer"):
        if len(detail) < 8:
            return True, "detail", "Can you be a bit more specific — brand, condition, or what you need?"
        if (
            _swap_needs_kids_stage(detail)
            and not draft.get("stage")
            and not _SIZE_IN_DETAIL_RE.search(detail)
        ):
            return True, "stage", "What size — e.g. 3T, size 5? (clothing/gear only)"
    if intent in ("meet_seek", "host_meet"):
        if not _has_when_hint(str(when_hint)):
            return True, "when_hint", "When works for you — weekday morning, weekend, something else?"
    if intent in ("tip_seek", "tip_share"):
        if not category and not _infer_tip_category(detail):
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
    elif not field and text:
        # Clarification reply without active field — treat as detail refresh
        out["detail"] = text
    inferred = _infer_tip_category(str(out.get("detail") or ""))
    if inferred and not out.get("category"):
        out["category"] = inferred
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


def clear_signal_draft(ctx: dict[str, Any]) -> None:
    """Mark signal_draft deleted so session merge does not resurrect a stale draft."""
    ctx.pop("signal_draft", None)
    ctx["signal_draft"] = None


def should_abandon_signal_draft(
    msg: str,
    draft: dict[str, Any],
    slots: dict[str, Any] | None = None,
) -> bool:
    """Drop in-progress draft when the user clearly started a new topic."""
    phase = str(draft.get("phase") or "")
    text = str(msg or "").strip()
    if phase == PHASE_SIGNAL_LISTENING:
        return True
    if phase != PHASE_SIGNAL_CONFIRM:
        return False
    field = str(draft.get("confirm_field") or "")
    if field == "stage" and not _looks_like_size_answer(text):
        return True
    if len(text) > 32:
        return True
    linear = slots_linear_intent(slots) if slots else None
    if linear and linear != draft.get("linear_intent"):
        return True
    if slots and phase == PHASE_SIGNAL_CONFIRM:
        new_detail = str(slots.get("signal_detail") or "").strip().lower()
        old_detail = str(draft.get("detail") or "").strip().lower()
        if (
            new_detail
            and old_detail
            and len(new_detail) > 8
            and new_detail not in old_detail
            and old_detail not in new_detail
        ):
            return True
    if re.search(
        r"\b(buddy|buddies|teacher|tutor|also looking|know a good|recommend|"
        r"bicycl\w*|bike|pizza|restaurant|shop|i wanna swap|want to swap|offering|"
        r"wanna buy|want to buy|looking for)\b",
        text,
        re.I,
    ):
        return True
    return False
