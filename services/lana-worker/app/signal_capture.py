"""LOOKING/SHARING 4-phase voice-first cascade (LANA_INTENTS §2.3–2.4)."""

from __future__ import annotations

import re
from typing import Any

from app.layer1_intents import (
    LOOKING_SHARING_INTENTS,
    SIGNAL_INTENT_BY_LINEAR,
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
_WHERE_IN_DETAIL_RE = re.compile(
    r"\b(?:near|around|in|on|at)\s+[\w]|cross[\s-]?street|neighborhood|"
    r"\b(?:block|downtown|uptown)\b",
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
_CANCEL_RE = re.compile(
    r"\b(?:cancel|never\s*mind|nevermind|forget (?:it|this|that)|"
    r"get me out|drop (?:it|this|that)|start over|nvm|abort|"
    r"stop (?:this|asking|it)|out of the .* loop)\b",
    re.I,
)
_AFFIRMATIVE = frozenset({"yes", "yeah", "yep", "sure", "ok", "okay", "correct", "right"})
_TIP_CATEGORIES = frozenset({"health", "food", "home", "activities", "education", "other"})


def normalize_category_answer(text: str) -> str:
    raw = str(text or "").strip().lower()
    if raw in _TIP_CATEGORIES:
        return raw
    if raw in ("school", "tutor", "teacher"):
        return "education"
    if raw in ("restaurant", "cafe", "pizza", "dining"):
        return "food"
    if raw in ("doctor", "dentist", "clinic", "medical"):
        return "health"
    return str(text or "").strip()[:120]


def _normalize_tip_share_detail(text: str) -> str:
    raw = str(text or "").strip()
    m = re.search(
        r"\b(?:dr\.?\s+)?([\w'.-]+)\s+is\s+(?:a\s+)?"
        r"(?:great|good|wonderful|amazing|excellent)\s+(\w+)",
        raw,
        re.I,
    )
    if m:
        name = str(m.group(1) or "").strip().title()
        role = str(m.group(2) or "").strip().lower()
        if not name.lower().startswith("dr"):
            name = f"Dr. {name}"
        return f"{name} · {role}"
    return raw[:500]


def _has_where_hint(text: str) -> bool:
    return bool(_WHERE_IN_DETAIL_RE.search(str(text or "")))


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
        r"\b(pediatrician|dentist|doctor|tutor|teacher|plumber|restaurant|resturant|pizza)\b",
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
    linear = slots_linear_intent(slots)
    if linear in LOOKING_SHARING_INTENTS:
        return linear
    return "looking.swap"


def draft_from_slots(slots: dict[str, Any], *, msg: str) -> dict[str, Any]:
    linear = _linear_from_message(msg, slots)
    intent = SIGNAL_INTENT_BY_LINEAR.get(linear, "swap_seek")
    detail = str(slots.get("signal_detail") or msg or "").strip()[:500]
    if intent == "tip_seek":
        detail = _normalize_tip_detail(detail)
    elif intent == "tip_share":
        detail = _normalize_tip_share_detail(detail or msg)
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
    if re.search(r"\b(pizza|restaurant|resturant|food|cafe|coffee|bakery)\b", low):
        return "food"
    if re.search(r"\b(pediatrician|doctor|dentist|therapist|clinic)\b", low):
        return "health"
    if re.search(r"\b(plumber|electrician|contractor|repair)\b", low):
        return "home"
    return None


def _compose_where_hint_ask(detail: str, attempt: int) -> str:
    """The where-ask for a tip being SHARED, grounded in the tip itself.

    The old line was "I have most of it. **Where, roughly?**" — contextless mechanic-talk
    that names neither what it has nor what it wants, and reads as a non-sequitur whenever
    the draft holds no real tip (dev QA 2026-08-04: a doctor ASK was misrouted into this
    cascade and the user got "Where, roughly?" with nothing to place). Grounding it in the
    detail makes the ask self-explanatory, and when the detail is vague the model naturally
    asks WHICH one they mean.
    """
    from app.reply_compose import compose_reply

    what = str(detail or "").strip()
    fallback = (
        "A neighborhood or cross-street is enough — or say skip."
        if attempt > 1
        else "Whereabouts is it — a neighborhood or cross-street is plenty."
    )
    if not what:
        return fallback
    return compose_reply(
        goal=(
            "The user is sharing a local recommendation with their neighbors and you need "
            "roughly WHERE it is before you post it. Ask for that in one short question, "
            "naming what they're recommending so the question explains itself. A "
            "neighborhood or cross-street is enough. If what they gave you is vague and "
            "names no specific place or provider, ask WHICH one they mean instead."
            + (" They already skipped past this once — keep it lighter and offer to skip."
               if attempt > 1 else "")
        ),
        facts=[f"What they're recommending: {what[:120]}"],
        fallback=fallback,
        max_sentences=1,
    )


def _confirm_prompt(field: str, attempt: int, *, detail: str = "") -> str:
    if field == "where_hint":
        return _compose_where_hint_ask(detail, attempt)
    if field == "stage":
        if attempt <= 1:
            return "Quick one — is this for a kid or an adult?"
        return "No worries — kid or adult works. Just say it your way and I'll post it."
    if field == "detail":
        if attempt <= 1:
            return "Can you be a bit more specific — what item, and anything helpful like condition?"
        return "What are you looking for or offering? A short phrase is fine."
    if field == "when_hint":
        if attempt <= 1:
            return "When works for you — weekday morning, weekend, something else?"
        return "Any rough timing — mornings, weekends, flexible?"
    if field == "category":
        if attempt <= 1:
            return "What kind of tip is this — health, food, home, activities, or something else?"
        return "Which area fits best — health, food, home, activities, or other?"
    return "Tell me a bit more and I'll get this out to your neighbors."


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
    # tip_seek: the detail IS the searchable thing (e.g. "tax preparer", "plumber"), so don't
    # force a category — asking "health/food/home?" after the user clearly named what they want
    # is pure friction, and it was where the subject got overwritten. Infer the category
    # silently when possible; never block the seek on it. tip_share still asks so a SHARED tip
    # gets bucketed for others to find.
    if intent == "tip_share":
        if not category and not _infer_tip_category(detail):
            field = "category"
            return True, field, _confirm_prompt(field, int(attempts.get(field, 0)) + 1)
    if intent in ("tip_seek", "tip_share"):
        if intent == "tip_share" and not draft.get("where_hint") and not _has_where_hint(detail):
            field = "where_hint"
            return (
                True,
                field,
                _confirm_prompt(field, int(attempts.get(field, 0)) + 1, detail=detail),
            )
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
    elif field == "where_hint":
        out["where_hint"] = text
        if text.lower() not in str(out.get("detail") or "").lower():
            out["detail"] = f"{out['detail']} — {text}".strip(" —")
    elif field == "category":
        out["category"] = normalize_category_answer(text)
    elif not field and text:
        out["detail"] = text
    inferred = _infer_tip_category(str(out.get("detail") or ""))
    if inferred and not out.get("category"):
        out["category"] = inferred
    out["confirm_field"] = None
    out["phase"] = PHASE_SIGNAL_LISTENING
    return out


def _apply_ai_confirm_result(
    draft: dict[str, Any], ai: dict[str, Any], pending: str
) -> dict[str, Any]:
    """Store a parsed AI confirm result into the draft."""
    out = dict(draft)
    linear = str(ai.get("linear_intent") or "").strip()
    if linear in SIGNAL_INTENT_BY_LINEAR and pending != "category":
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
    elif field == "where_hint" and value:
        out["where_hint"] = value
        out["detail"] = f"{out.get('detail', '')} — {value}".strip(" —")
    elif field == "category" and value:
        out["category"] = normalize_category_answer(value)
    out["confirm_field"] = None
    out["phase"] = PHASE_SIGNAL_LISTENING
    return out


def apply_confirm_answer_ai_first(
    draft: dict[str, Any],
    msg: str,
    *,
    ai_verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a confirm-phase answer. Uses the AI verdict (verdict == 'answer') when the
    caller already fetched one; otherwise falls back to the deterministic parser. The
    caller is responsible for handling 'cancel'/'reroute' verdicts (they escape the
    cascade rather than filling a slot).
    """
    pending = str(draft.get("confirm_field") or "")
    if pending and ai_verdict and ai_verdict.get("verdict") == "answer":
        return _apply_ai_confirm_result(draft, ai_verdict, pending)
    return apply_confirm_answer(draft, msg)


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
    elif field == "where_hint":
        out["where_hint"] = text
        out["detail"] = f"{out.get('detail', '')} — {text}".strip(" —")
    elif field == "category":
        out["category"] = normalize_category_answer(text)
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
    ai_verdict: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None, bool]:
    """
    Advance cascade. Returns (updated_draft, confirm_prompt, ready_to_save).

    ai_verdict is the pre-fetched confirm-reply verdict (verdict == 'answer') so the
    dispatcher's single LLM read is reused here. cancel/reroute verdicts are handled by
    the caller before this runs — they never reach the slot-fill.
    """
    phase = str(draft.get("phase") or PHASE_SIGNAL_EXTRACT)
    attempts = dict(draft.get("confirm_attempts") or {})

    if phase == PHASE_SIGNAL_CONFIRM:
        pending = str(draft.get("confirm_field") or "")
        if pending:
            attempts[pending] = int(attempts.get(pending, 0)) + 1

        # AI verdict (if any) reads the reply; deterministic parser is the offline fallback.
        updated = apply_confirm_answer_ai_first(draft, msg, ai_verdict=ai_verdict)
        updated["confirm_attempts"] = attempts

        need, field, _prompt = needs_confirm(updated)
        # Still missing the same field after a re-ask → accept their words, don't loop.
        if need and field == pending and int(attempts.get(field, 0)) >= 2:
            updated = _force_accept_pending_field({**updated, "confirm_field": field}, msg)
            need, field, _prompt = needs_confirm(updated)

        if need:
            updated["phase"] = PHASE_SIGNAL_CONFIRM
            updated["confirm_field"] = field
            prompt = _confirm_prompt(
                field,
                int(attempts.get(field, 0)) + 1,
                detail=str(updated.get("detail") or ""),
            )
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


def _signal_draft_new_topic(
    text: str,
    draft: dict[str, Any],
    slots: dict[str, Any] | None,
) -> bool:
    """User pivoted away from the in-progress draft to a different ask."""
    linear = slots_linear_intent(slots) if slots else None
    if linear and linear != draft.get("linear_intent"):
        return True
    if slots:
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
    if re.search(r"\b(?:for my|my kid|my child)\b", text, re.I):
        return True
    return bool(_TOPIC_CHANGE_RE.search(text))


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
    if field == "category":
        return False
    if field == "when_hint" and len(text) <= 80:
        return False

    new_topic = _signal_draft_new_topic(text, draft, slots)

    # Size/stage step: a size answer keeps the draft; a pivot (different ask,
    # "for my kid", "do you know a good pizza shop") abandons it so the new
    # intent can route instead of looping on the same confirm prompt.
    if field == "stage":
        if not new_topic and normalize_size_answer(text):
            return False
        if not _looks_like_size_answer(text):
            return new_topic or len(text) > 32
        return new_topic

    if len(text) > 32:
        return True
    return new_topic


def is_signal_cancel(msg: str) -> bool:
    """User explicitly wants out of the signal-capture cascade."""
    return bool(_CANCEL_RE.search(str(msg or "").strip()))


def should_abort_signal_draft(
    msg: str,
    draft: dict[str, Any],
    slots: dict[str, Any] | None = None,
) -> bool:
    """Bail out of an in-progress confirm cascade when the user is no longer
    answering the confirm question — an explicit cancel, or a confident pivot
    to a different intent. Unlike should_abandon_signal_draft (listening/extract
    phases only), this is evaluated DURING the confirm phase so slot-fill cannot
    swallow a new ask. Plain slot answers (category word, size, "this weekend")
    must NOT trigger this.
    """
    text = str(msg or "").strip()
    if is_signal_cancel(text):
        return True
    if not slots:
        return False
    try:
        confidence = float(slots.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.55:
        return False
    linear = slots_linear_intent(slots)
    # Pivot to a *different* signal lane (e.g. mid-tip, user says "swap a shoe").
    if (
        linear
        and linear in LOOKING_SHARING_INTENTS
        and linear != str(draft.get("linear_intent") or "")
    ):
        return True
    # Pivot to a non-signal actionable intent (discovery / intro / block log / auth).
    goal = str(slots.get("goal") or "")
    if goal in (
        "peers",
        "both",
        "activities",
        "propose_intro",
        "list_intros",
        "show_block_log",
        "verify",
        "login",
        "logout",
    ):
        return True
    if linear in (
        "social.propose_intro",
        "social.list_intros",
        "discovery.block_log",
        "discovery.find_peers",
        "discovery.find_by_attrs",
        "discovery.find_in_block",
        "tier.send_nudge",
        "tier.respond_nudge",
    ):
        return True
    return False
