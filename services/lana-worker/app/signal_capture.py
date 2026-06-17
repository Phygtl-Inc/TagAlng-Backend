"""LOOKING/SHARING 4-phase voice-first cascade (LANA_INTENTS §2.3–2.4)."""

from __future__ import annotations

import re
from typing import Any

from app.layer1_intents import (
    LOOKING_SHARING_INTENTS,
    SIGNAL_INTENT_BY_LINEAR,
    phrase_linear_intent,
    slots_linear_intent,
)

PHASE_SIGNAL_EXTRACT = "signal_extract"
PHASE_SIGNAL_CONFIRM = "signal_confirm_missing"
PHASE_SIGNAL_LISTENING = "signal_listening"

_WHEN_HINT = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"morning|afternoon|evening|weekend|weekday|daily|weekly|"
    r"today|tomorrow|am|pm|\d{1,2}\s*(?:am|pm))\b",
    re.I,
)
_KIDS_SIZED_CLOTHING_RE = re.compile(
    r"\b(?:boots?|rain\s*boots?|onesie|diaper|jacket|coat|shirt|pants|dress|"
    r"\d+t\b|size\s*\d+)\b",
    re.I,
)
_GENERAL_SWAP_ITEM_RE = re.compile(
    r"\b(?:laptop|computer|phone|tablet|furniture|chair|desk|sofa|"
    r"microwave|printer|monitor|ipad|macbook|hp|dell|lenovo|condition|"
    r"bicycl\w*|bike|scooter|wagon|tricycle|stroller|car\s*seat|crib|"
    r"highchair|pack\s*n\s*play)\b",
    re.I,
)
_ADULT_SIZE_RE = re.compile(
    r"\b(?:adults?|grown[- ]?ups?|for adults?|mens?|womens?)\b",
    re.I,
)
_SIZE_IN_DETAIL_RE = re.compile(
    r"\b(\d+t|\d+\s*year|size\s*\d+|\d+\s*(?:\"|in|inch)|"
    r"xs|s|m|l|xl|xxl|small|medium|large|adults?|grown[- ]?ups?)\b",
    re.I,
)
_SIZE_ANSWER_RE = re.compile(
    r"^\s*(?:\d+t|\d+\s*year(?:s| old)?|\d+\s*(?:\"|in|inch)?|size\s*\d+|"
    r"xs|s|m|l|xl|xxl|small|medium|large|adults?|grown[- ]?ups?|"
    r"newborn|infant|toddler|kids?|children?)\s*\.?!?\s*$",
    re.I,
)
_TOPIC_CHANGE_RE = re.compile(
    r"\b(?:buy|wanna|want to|looking for|need a|also looking|do you know|"
    r"buddy|teacher|tutor|pizza|swap|borrow|offer|host|bicycl\w*|bike|give away)\b",
    re.I,
)
_AFFIRMATIVE = frozenset({"yes", "yeah", "yep", "sure", "ok", "okay", "correct", "right"})


def _has_when_hint(text: str) -> bool:
    return bool(_WHEN_HINT.search(str(text or "")))


def _normalize_tip_detail(text: str) -> str:
    raw = str(text or "").strip()
    m = re.search(
        r"\b(?:know a good|know any good|recommend a|recommendation for a?|"
        r"looking for a?|need a?|find a?)\s+(.+?)[\?.!]*$",
        raw,
        re.I,
    )
    if m:
        return m.group(1).strip()[:500]
    m = re.search(
        r"\b(pediatrician|dentist|doctor|tutor|teacher|plumber|restaurant|pizza)\b",
        raw,
        re.I,
    )
    if m:
        return f"good {m.group(1).lower()}"
    return raw[:500]


def _normalize_swap_detail(text: str) -> str:
    """Strip intent verbs from swap detail so cards read like item nouns."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    normalized = re.sub(
        r"^\s*(?:i\s+)?(?:am\s+)?(?:looking\s+to\s+)?(?:want\s+to\s+|wanna\s+)?"
        r"(?:give(?:\s+)?away|give(?:\s+)?up|swap|trade)\s+(?:my\s+)?",
        "",
        raw,
        flags=re.I,
    ).strip()
    if normalized:
        return normalized[:500]
    return raw[:500]


def normalize_size_answer(text: str) -> str:
    """Normalize free-text size replies (adults, for adults, grown-up, 3T)."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if _ADULT_SIZE_RE.search(low):
        return "adult"
    if re.search(r"\b(?:kids?|children?|child|toddler|infant|newborn)\b", low):
        return "kids"
    m = _SIZE_IN_DETAIL_RE.search(raw)
    if m:
        bit = m.group(1).lower()
        if bit in ("adults", "grown-ups", "grownups", "grown ups"):
            return "adult"
        return m.group(1)
    if _SIZE_ANSWER_RE.match(raw):
        return raw
    if len(raw.split()) <= 4:
        return raw
    return ""


def _linear_from_message(msg: str, slots: dict[str, Any]) -> str:
    phrase = phrase_linear_intent(msg)
    if phrase in LOOKING_SHARING_INTENTS:
        return phrase
    linear = slots_linear_intent(slots)
    return linear if linear in LOOKING_SHARING_INTENTS else "looking.swap"


def draft_from_slots(slots: dict[str, Any], *, msg: str) -> dict[str, Any]:
    linear = _linear_from_message(msg, slots)
    intent = SIGNAL_INTENT_BY_LINEAR.get(linear, "swap_seek")
    detail = str(slots.get("signal_detail") or msg or "").strip()[:500]
    if intent == "tip_seek":
        detail = _normalize_tip_detail(detail)
    elif intent in ("swap_seek", "swap_offer"):
        detail = _normalize_swap_detail(detail)
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
        "confirm_attempts": {},
    }


def _swap_needs_kids_stage(detail: str) -> bool:
    if _GENERAL_SWAP_ITEM_RE.search(detail):
        return False
    if _ADULT_SIZE_RE.search(detail):
        return False
    return bool(_KIDS_SIZED_CLOTHING_RE.search(detail))


def _detail_has_size_hint(detail: str) -> bool:
    return bool(_SIZE_IN_DETAIL_RE.search(detail) or _ADULT_SIZE_RE.search(detail))


def _looks_like_size_answer(text: str) -> bool:
    t = str(text or "").strip()
    if not t or len(t) > 48:
        return False
    if _TOPIC_CHANGE_RE.search(t) and not normalize_size_answer(t):
        return False
    if normalize_size_answer(t):
        return True
    return bool(_SIZE_IN_DETAIL_RE.search(t) and len(t.split()) <= 5)


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


def _confirm_prompt(field: str, attempt: int) -> str:
    if field == "stage":
        if attempt <= 1:
            return "Quick one — is this for a kid or an adult?"
        return "No worries — kid or adult works. Just say it your way and I'll post it."
    if field == "detail":
        if attempt <= 1:
            return "Can you tell me a bit more — what item, and anything helpful like condition?"
        return "What are you looking for or offering? A short phrase is fine."
    if field == "when_hint":
        if attempt <= 1:
            return "When works for you — weekday morning, weekend, something else?"
        return "Any rough timing — mornings, weekends, flexible?"
    if field == "category":
        if attempt <= 1:
            return "What kind of tip is this — health, food, home, activities, or something else?"
        return "Which area fits best — health, food, home, activities, or other?"
    return "Tell me a bit more and I'll get this on your block."


def needs_confirm(draft: dict[str, Any]) -> tuple[bool, str, str]:
    """Return (needs_confirm, field_name, prompt)."""
    intent = str(draft.get("intent") or "")
    detail = str(draft.get("detail") or "").strip()
    category = draft.get("category")
    when_hint = draft.get("when_hint") or detail
    attempts = draft.get("confirm_attempts") if isinstance(draft.get("confirm_attempts"), dict) else {}

    if intent in ("swap_seek", "swap_offer"):
        if len(detail) < 8:
            field = "detail"
            return True, field, _confirm_prompt(field, int(attempts.get(field, 0)) + 1)
        if (
            _swap_needs_kids_stage(detail)
            and not draft.get("stage")
            and not _detail_has_size_hint(detail)
        ):
            field = "stage"
            return True, field, _confirm_prompt(field, int(attempts.get(field, 0)) + 1)
    if intent in ("meet_seek", "host_meet"):
        if not _has_when_hint(str(when_hint)):
            field = "when_hint"
            return True, field, _confirm_prompt(field, int(attempts.get(field, 0)) + 1)
    if intent in ("tip_seek", "tip_share"):
        if not category and not _infer_tip_category(detail):
            field = "category"
            return True, field, _confirm_prompt(field, int(attempts.get(field, 0)) + 1)
    return False, "", ""


def _apply_linear_correction(draft: dict[str, Any], linear_intent: str) -> dict[str, Any]:
    out = dict(draft)
    if linear_intent not in SIGNAL_INTENT_BY_LINEAR:
        return out
    out["linear_intent"] = linear_intent
    out["intent"] = SIGNAL_INTENT_BY_LINEAR[linear_intent]
    return out


def apply_confirm_answer(draft: dict[str, Any], msg: str) -> dict[str, Any]:
    out = dict(draft)
    field = str(out.get("confirm_field") or "")
    text = str(msg or "").strip()[:500]
    if not text:
        return out
    if field == "detail":
        out["detail"] = text
    elif field == "stage":
        normalized = normalize_size_answer(text) or text
        out["stage"] = normalized
        if normalized.lower() not in out["detail"].lower():
            out["detail"] = f"{out['detail']} ({normalized})".strip()
    elif field == "when_hint":
        out["when_hint"] = text
        out["detail"] = f"{out['detail']} — {text}".strip(" —")
    elif field == "category":
        out["category"] = text[:120]
    elif not field and text:
        out["detail"] = text
    inferred = _infer_tip_category(str(out.get("detail") or ""))
    if inferred and not out.get("category"):
        out["category"] = inferred
    out["confirm_field"] = None
    out["phase"] = PHASE_SIGNAL_LISTENING
    return out


def _ai_assist_confirm(draft: dict[str, Any], msg: str) -> dict[str, Any]:
    """Use AI when regex misses a short confirm answer."""
    from app.signal_confirm_ai import interpret_signal_confirm_reply

    pending = str(draft.get("confirm_field") or "")
    if not pending:
        return draft
    ai = interpret_signal_confirm_reply(draft, msg)
    if not ai:
        return draft
    out = dict(draft)
    linear = str(ai.get("linear_intent") or "").strip()
    if linear in SIGNAL_INTENT_BY_LINEAR:
        out = _apply_linear_correction(out, linear)
    value = str(ai.get("value") or "").strip()
    field = str(ai.get("field") or pending).strip()
    if field == "stage" and value:
        out["stage"] = normalize_size_answer(value) or value
        stage_bit = out["stage"]
        if stage_bit.lower() not in str(out.get("detail") or "").lower():
            out["detail"] = f"{out.get('detail', '')} ({stage_bit})".strip()
    elif field == "detail" and value:
        out["detail"] = value
    elif field == "when_hint" and value:
        out["when_hint"] = value
        out["detail"] = f"{out.get('detail', '')} — {value}".strip(" —")
    elif field == "category" and value:
        out["category"] = value[:120]
    out["confirm_field"] = None
    out["phase"] = PHASE_SIGNAL_LISTENING
    return out


def _force_accept_pending_field(draft: dict[str, Any], msg: str) -> dict[str, Any]:
    """Last resort — accept neighbor's words rather than looping."""
    out = dict(draft)
    field = str(out.get("confirm_field") or "")
    text = str(msg or "").strip()[:500]
    if not field or not text:
        return out
    if field == "stage":
        normalized = normalize_size_answer(text) or text[:64]
        out["stage"] = normalized
        if normalized.lower() not in str(out.get("detail") or "").lower():
            out["detail"] = f"{out.get('detail', '')} ({normalized})".strip()
    elif field == "detail":
        out["detail"] = text
    elif field == "when_hint":
        out["when_hint"] = text
        out["detail"] = f"{out.get('detail', '')} — {text}".strip(" —")
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
    attempts = dict(draft.get("confirm_attempts") or {})

    if phase == PHASE_SIGNAL_CONFIRM:
        pending = str(draft.get("confirm_field") or "")
        if pending:
            attempts[pending] = int(attempts.get(pending, 0)) + 1

        updated = apply_confirm_answer(draft, msg)
        updated["confirm_attempts"] = attempts

        need, field, _prompt = needs_confirm(updated)
        if need and field == pending and int(attempts.get(field, 0)) >= 2:
            updated = _ai_assist_confirm({**updated, "confirm_field": field}, msg)
            need, field, _prompt = needs_confirm(updated)

        if need and field == pending and int(attempts.get(field, 0)) >= 3:
            updated = _force_accept_pending_field({**updated, "confirm_field": field}, msg)
            need, field, _prompt = needs_confirm(updated)

        if need:
            updated["phase"] = PHASE_SIGNAL_CONFIRM
            updated["confirm_field"] = field
            prompt = _confirm_prompt(field, int(attempts.get(field, 0)) + 1)
            return updated, prompt, False
        updated["phase"] = PHASE_SIGNAL_LISTENING
        return updated, None, True

    need, field, prompt = needs_confirm(draft)
    if need:
        out = dict(draft)
        out["phase"] = PHASE_SIGNAL_CONFIRM
        out["confirm_field"] = field
        out["confirm_attempts"] = attempts
        return out, prompt, False

    out = dict(draft)
    out["phase"] = PHASE_SIGNAL_LISTENING
    out["confirm_attempts"] = attempts
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
    if field == "stage" and normalize_size_answer(text):
        return False
    if field == "stage" and not _looks_like_size_answer(text):
        if phrase_linear_intent(text) in LOOKING_SHARING_INTENTS:
            return True
        if len(text) > 32:
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
        r"give away|wanna buy|want to buy|looking for)\b",
        text,
        re.I,
    ):
        return True
    return False
