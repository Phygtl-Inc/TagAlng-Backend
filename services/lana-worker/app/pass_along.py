"""In-chat "pass along an item" capture (the swap_offer flow), mirroring the event
host flow but deterministic: the LLM only does STRUCTURED extraction, the questions
and chips are driven in code so it stays on-script and never loops.

Flow (matches the C-4-swap mock):
  P1  "Tell me about your item."            (no item yet)
  P2  "Heard you." + colored entity chips + ask the ONE missing thing w/ options
  P3  photo prompt → list it
  →   saved to local_signals (swap_offer) with photo_url; matcher pings seekers.

Entry is deterministic via intent_hint="pass_along" (see main.py) → session flag
`pass_along_active`, same pattern as `event_host_active`.
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── tappable option sets (same idea as the event flow's quick replies) ──────────
_CONDITION_SUGGESTIONS = ["Brand new", "Like new", "Lightly used", "Well-loved"]
_INTENT_SUGGESTIONS = ["Free", "Looking to swap"]
_CATEGORY_SUGGESTIONS = ["Electronics", "Clothing", "Toys", "Furniture"]
# Cap on AI-tailored enrichment questions, so capture never drags on.
_MAX_ENRICH = 2


def _question_for_field(field: str) -> tuple[str, list[str]]:
    """The re-ask used when a chip is tapped to correct that field."""
    if field == "intent_type":
        return "Is it **free**, or are you looking to swap?", _INTENT_SUGGESTIONS
    if field == "condition":
        return "What condition is it in?", _CONDITION_SUGGESTIONS
    if field == "category":
        return "What kind of item is it?", _CATEGORY_SUGGESTIONS
    if field == "details":
        return "What detail should I update?", []
    return "What should I change?", []

# Loop safety: an explicit bail or a hard turn cap releases the flow (never trapped).
_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|nvm|stop|forget it|not now|skip this|exit|quit)\b",
    re.IGNORECASE,
)
_LIST_NOW_RE = re.compile(
    r"\b(list it|list now|post it|that'?s it|all done|done|go ahead|publish|"
    r"no photo|skip photo|without photo|just list)\b",
    re.IGNORECASE,
)
_PASS_ALONG_TURN_CAP = 12

# Deterministic entry detection (a backstop for the "Something to pass along" CTA,
# so the flow engages even if the FE doesn't send intent_hint). Matches OFFERING an
# item — not a "looking for" seek.
_ENTRY_RE = re.compile(
    r"\b(pass(?:ing)?\s+along|give\s+away|giving\s+away|hand(?:ing)?\s+(?:down|over)|"
    r"free to a good home|something to share|to pass (?:along|on))\b",
    re.IGNORECASE,
)


def looks_like_pass_along_entry(message: str) -> bool:
    """True when the message looks like the user wants to offer/pass along an item."""
    return bool(_ENTRY_RE.search(str(message or "").strip()))

_ITEM_VALUE_FIELDS = ("title", "category", "condition", "intent_type")

_EXTRACT_SYSTEM = """You extract structured fields about a second-hand item a neighbor \
wants to pass along (give away free, or swap), and propose ONE smart follow-up question.

Return ONE compact JSON object with exactly these keys:
{"title","category","condition","intent_type","other_items","ask"}

- title: short noun phrase for the item, e.g. "3T rain boots", "kids bicycle", "MacBook Air". null if not stated.
- category: broad bucket, e.g. "kids clothing","toys","furniture","electronics","books","baby gear","other". null if unclear.
- condition: e.g. "brand new","like new","lightly used","well-loved". null if not stated.
- intent_type: "free" if giving it away, "swap" if they want to trade, null if unclear.
- other_items: if the user named MORE THAN ONE distinct item to pass along ("a tennis racket and a stroller"),
  list the OTHER item titles here as an array of short strings (exclude the primary `title`). Empty array [] if only one item.
- ask: the single MOST useful follow-up to make this a strong listing, TAILORED to THIS item and
  to what's still unknown — and the tappable answers must fit the item:
    • kids clothing/shoes → size ("3T", "Size 8", "Newborn", …)
    • a bike → wheel size or rider age ("12\\"", "16\\"", "Kid", "Adult")
    • a laptop/phone → key spec ("8GB / 256GB", "16GB / 512GB", "Not sure")
    • furniture → dimensions or color
  Shape: {"field": <short snake_case key>, "question": <one short question>, "options": [2-4 short answers]}.
  Return null for `ask` when title + condition + free/swap is already enough (e.g. a simple toy or book),
  or when you can't propose item-appropriate options. Do NOT ask about price or condition here.

Use null for any string the text does not support. Never invent a value, and never offer options that
don't fit the item."""


def _extract_item_fields(
    *, history: list[dict[str, Any]], user_message: str, prev: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """LLM structured extraction. Returns (fields_found, ask) where `ask` is the AI's
    item-tailored follow-up {field, question, options} or None. ({}, None) on any
    failure (extraction must never break the turn)."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return {}, None
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        known = {k: prev.get(k) for k in _ITEM_VALUE_FIELDS}
        known["details"] = prev.get("details") or []
        payload = "\n\n".join(
            [
                "CURRENT ITEM DRAFT (merge updates into this):\n"
                + json.dumps(known, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_EXTRACT_SYSTEM,
            user_payload=payload,
            max_tokens=320,
            temperature=0.2,
        )
        if not isinstance(data, dict):
            return {}, None
        out: dict[str, Any] = {}
        for k in _ITEM_VALUE_FIELDS:
            v = data.get(k)
            if isinstance(v, str) and v.strip() and v.strip().lower() != "null":
                out[k] = v.strip()
        # additional items the user bundled in one message (so we never silently drop one)
        others = data.get("other_items")
        if isinstance(others, list):
            clean = [str(o).strip() for o in others if isinstance(o, str) and str(o).strip()]
            if clean:
                out["other_items"] = clean[:4]
        # normalize intent_type
        it = str(out.get("intent_type") or "").lower()
        if it:
            out["intent_type"] = "free" if "free" in it else "swap" if "swap" in it else None
            if out["intent_type"] is None:
                out.pop("intent_type")
        # validate the AI's tailored follow-up
        ask = data.get("ask")
        if isinstance(ask, dict):
            q = str(ask.get("question") or "").strip()
            opts = [
                str(o).strip()
                for o in (ask.get("options") or [])
                if isinstance(o, str) and str(o).strip()
            ][:4]
            if q and len(opts) >= 2:
                ask = {"field": str(ask.get("field") or "detail").strip(), "question": q, "options": opts}
            else:
                ask = None
        else:
            ask = None
        return out, ask
    except Exception:  # noqa: BLE001 - extraction is best-effort
        import logging

        logging.getLogger(__name__).exception("pass_along_extract_failed")
        return {}, None


def _has(draft: dict[str, Any], key: str) -> bool:
    return bool(str(draft.get(key) or "").strip())


def _build_chips(draft: dict[str, Any]) -> list[dict[str, str]]:
    """The 'Heard you' entity chips — colored by type, tappable to correct (the FE
    sends `fix:<field>` on tap, which re-asks that field)."""
    chips: list[dict[str, str]] = []
    # Action umbrella (always, once we have an item).
    chips.append({"label": "Pass along", "tone": "coral", "field": "intent_type"})
    # AI-captured detail chips (size / specs / dimensions — whatever fit the item).
    for d in (draft.get("details") or []):
        if str(d).strip():
            chips.append({"label": str(d).strip(), "tone": "sky", "field": "details"})
    it = str(draft.get("intent_type") or "")
    if it:
        chips.append(
            {"label": "Free" if it == "free" else "Swap", "tone": "green", "field": "intent_type"}
        )
    if _has(draft, "condition"):
        chips.append({"label": str(draft["condition"]), "tone": "amber", "field": "condition"})
    if _has(draft, "category"):
        chips.append({"label": str(draft["category"]), "tone": "violet", "field": "category"})
    return chips


def _next_question(draft: dict[str, Any]) -> tuple[str | None, str, list[str]]:
    """The two UNIVERSAL questions every listing needs → (field, question, suggestions).
    Item-specific details (size, specs, …) are handled by the AI's tailored `ask`, not
    here. field is None when both universals are answered."""
    if not _has(draft, "intent_type"):
        return "intent_type", "Is it **free**, or are you looking to swap?", _INTENT_SUGGESTIONS
    if not _has(draft, "condition"):
        return "condition", "What condition is it in?", _CONDITION_SUGGESTIONS
    return None, "", []


def _summary(draft: dict[str, Any]) -> str:
    bits = [str(draft.get("title") or "your item").strip()]
    for d in (draft.get("details") or []):
        if str(d).strip():
            bits.append(str(d).strip())
    if _has(draft, "condition"):
        bits.append(str(draft["condition"]))
    it = str(draft.get("intent_type") or "")
    if it:
        bits.append("free" if it == "free" else "to swap")
    return " · ".join(bits)


def _detail_text(draft: dict[str, Any]) -> str:
    parts = [str(draft.get("title") or "").strip()]
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    if _has(draft, "condition"):
        parts.append(str(draft["condition"]).strip())
    it = str(draft.get("intent_type") or "")
    if it:
        parts.append("free" if it == "free" else "to swap")
    return " · ".join([p for p in parts if p]) or str(draft.get("title") or "item")


def _save_item(
    *, draft: dict[str, Any], user_jwt: str, block_id: str | None, zip_code: str | None
) -> dict[str, Any] | None:
    """Persist as a swap_offer in local_signals (so the seek↔offer matcher fires).
    Returns the saved payload, or None on failure (caller exits gracefully)."""
    try:
        from app.local_signals import save_local_signal

        return save_local_signal(
            user_jwt,
            intent="swap_offer",
            detail_text=_detail_text(draft),
            category=str(draft.get("category") or "").strip() or None,
            block_id=block_id,
            zip_code=zip_code,
            photo_url=str(draft.get("photo_url") or "").strip() or None,
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("pass_along_save_failed")
        return None


def run_pass_along_turn(
    *,
    user_message: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_jwt: str,
    home_block_id: str | None,
) -> str:
    """Drive one pass-along capture turn. Mutates session_ctx in place — sets
    `item_draft`, `pass_along_active`, `item_listed_now`, `routing_phase` — and
    returns Lana's reply text."""
    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("item_draft") or {})
    # Only the actual save turn sets this — clear any stale flag so the card doesn't
    # render the green "listed" state during capture/photo.
    session_ctx["item_listed_now"] = False

    # ── Loop safety: explicit bail / turn cap releases the flow ──
    turns = int(session_ctx.get("pass_along_turns") or 0) + 1
    session_ctx["pass_along_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _PASS_ALONG_TURN_CAP:
        session_ctx["pass_along_active"] = False
        session_ctx["pass_along_turns"] = 0
        session_ctx["pass_along_pending_ask"] = None
        session_ctx["pass_along_enrich_count"] = 0
        session_ctx["pass_along_asked_fields"] = []
        session_ctx["pass_along_other_items"] = []
        session_ctx["item_draft"] = None
        session_ctx["routing_phase"] = "listening"
        return "No problem — we can do that another time. What else can I help with?"

    # ── Correction: a chip tap arrives as "fix:<field>" → clear it and re-ask THAT
    # field directly (never fall through to save), re-opening capture. ──
    fix = re.match(r"\s*fix:(\w+)\s*$", msg)
    if fix:
        field = fix.group(1)
        if field == "details":
            draft["details"] = []
            session_ctx["pass_along_pending_ask"] = "details"  # next reply re-fills it
            session_ctx["pass_along_enrich_count"] = 0
        elif field in _ITEM_VALUE_FIELDS:
            draft.pop(field, None)
        session_ctx["pass_along_photo_prompted"] = False  # don't short-circuit to save
        q, opts = _question_for_field(field)
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = opts
        session_ctx["item_draft"] = draft
        session_ctx["pass_along_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Sure — {q}"

    # ── Capture the answer to an AI-tailored enrichment question into `details`
    # (the only answers the field-extractor can't map to a known column). ──
    pending = session_ctx.get("pass_along_pending_ask")
    if pending and msg and not _LIST_NOW_RE.search(msg):
        details = list(draft.get("details") or [])
        if msg not in details:
            details.append(msg)
        draft["details"] = details
        session_ctx["pass_along_pending_ask"] = None

    # ── Extract structured fields + the AI's tailored follow-up. The bare CTA ("I have
    # something to pass along") yields no item, so we fall to P1; a richer entry
    # ("pass along my kids bicycle") gets the item pulled out right away. ──
    ask: dict[str, Any] | None = None
    if msg:
        found, ask = _extract_item_fields(history=history, user_message=msg, prev=draft)
        others = found.pop("other_items", None)
        for k, v in found.items():
            draft[k] = v
        # Remember extra items the user bundled in, so we can offer them after this one
        # is listed instead of silently dropping them ("tennis racket and a stroller").
        if others and not session_ctx.get("pass_along_other_items"):
            session_ctx["pass_along_other_items"] = others

    # ── P1: no item yet → ask what it is ──
    if not _has(draft, "title"):
        session_ctx["item_draft"] = draft
        session_ctx["pass_along_active"] = True
        session_ctx["routing_phase"] = "listening"
        return "Happy to pass it along for you — tell me about your item. What is it?"

    chips = _build_chips(draft)
    field, question, suggestions = _next_question(draft)

    # ── P2a: a universal field is missing (free/swap, condition) → ask it ──
    if field is not None:
        draft["chips"] = chips
        draft["suggestions"] = suggestions
        session_ctx["item_draft"] = draft
        session_ctx["pass_along_active"] = True
        session_ctx["pass_along_photo_prompted"] = False
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. {question}"

    # ── P2b: AI-tailored enrichment (size / specs / dimensions — item-appropriate
    # options the model picked), capped so capture stays short. Never re-ask a field
    # we already asked: when the user's answer doesn't parse as that attribute the model
    # re-proposes the same question, which caused an identical re-ask loop
    # ("What color?" → "What color?"). ──
    enrich_count = int(session_ctx.get("pass_along_enrich_count") or 0)
    asked_fields = set(session_ctx.get("pass_along_asked_fields") or [])
    if ask and ask["field"] not in asked_fields and enrich_count < _MAX_ENRICH:
        asked_fields.add(ask["field"])
        session_ctx["pass_along_asked_fields"] = list(asked_fields)
        session_ctx["pass_along_pending_ask"] = ask["field"]
        session_ctx["pass_along_enrich_count"] = enrich_count + 1
        session_ctx["pass_along_photo_prompted"] = False
        draft["chips"] = chips
        draft["suggestions"] = ask["options"]
        session_ctx["item_draft"] = draft
        session_ctx["pass_along_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. {ask['question']}"

    # ── P3: details complete → offer a photo once, then list ──
    has_photo = _has(draft, "photo_url")
    wants_list = bool(_LIST_NOW_RE.search(msg))
    if not has_photo and not wants_list and not session_ctx.get("pass_along_photo_prompted"):
        draft["chips"] = chips
        draft["suggestions"] = []
        session_ctx["item_draft"] = draft
        session_ctx["pass_along_active"] = True
        session_ctx["pass_along_photo_prompted"] = True
        session_ctx["routing_phase"] = "listening"
        return (
            f"Got it — **{_summary(draft)}**. Add a photo so neighbors can see it — "
            "tap **Add photo** below, or say *list it* to post without one."
        )

    # ── Save → listed ──
    saved = _save_item(
        draft=draft,
        user_jwt=user_jwt,
        block_id=home_block_id,
        zip_code=str(session_ctx.get("zip_code") or "").strip() or None,
    )
    session_ctx["pass_along_active"] = False
    session_ctx["pass_along_turns"] = 0
    session_ctx["pass_along_photo_prompted"] = False
    session_ctx["pass_along_pending_ask"] = None
    session_ctx["pass_along_enrich_count"] = 0
    session_ctx["routing_phase"] = "listening"
    if not saved:
        session_ctx["item_draft"] = None
        return (
            "I couldn't post that just now — let's try again in a moment. "
            "Is your neighborhood set up?"
        )
    matches = int(saved.get("matches_created") or 0)
    draft["signal_id"] = saved.get("signal_id")
    draft["listed"] = True
    draft["chips"] = chips
    draft["suggestions"] = []
    session_ctx["item_draft"] = draft
    session_ctx["item_listed_now"] = True
    tail = (
        f" {matches} neighbor{'s' if matches != 1 else ''} looking for this just got pinged."
        if matches
        else " I'll ping anyone on your block who's looking for it."
    )
    # If they bundled several items in one message, offer the next instead of dropping it.
    queued = list(session_ctx.get("pass_along_other_items") or [])
    session_ctx["pass_along_other_items"] = []
    extra = ""
    if queued:
        names = (
            queued[0] if len(queued) == 1
            else ", ".join(queued[:-1]) + f" and {queued[-1]}"
        )
        that = "that" if len(queued) == 1 else "those"
        extra = (
            f" You also mentioned {names} — want to pass {that} along too? "
            "Just tell me about the next one."
        )
    return f"🎉 Done — **{_summary(draft)}** is listed on your block.{tail}{extra}"
