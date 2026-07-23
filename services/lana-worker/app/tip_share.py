"""In-chat "share a tip / recommendation" capture (the tip_share flow), mirroring the
pass-along flow: the LLM does STRUCTURED extraction, the questions are driven in code so
it stays on-script and never loops. Google Places supplies real nearby options for the
"who/where" when the tip is place-based.

Flow (matches the C-4-reco mock):
  P1  "What do you want to recommend?"          (nothing captured yet)
  P2  "Heard you." + colored chips (★ Recommendation / category / trait)
  P3  "Who or where? A name helps me find them" (Places options when place-based)
  P4  assembled card → "Pass the tip along" / "Send to a mom you know"
  →   saved to local_signals (tip_share); matcher pings neighbors asking for that category.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.reply_compose import compose_reply

_TRAIT_PROMPT = "What makes them great?"
_CATEGORY_SUGGESTIONS = ["Doctor / clinic", "Restaurant", "Park / playground", "Home service"]
_MAX_ENRICH = 2

_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|nvm|stop|forget it|not now|skip this|exit|quit)\b",
    re.IGNORECASE,
)
# The "Pass the tip along" CTA / any go-ahead to post it.
_PASS_RE = re.compile(
    r"\b(pass (?:the )?tip|pass it along|post it|share it|list it|that'?s it|"
    r"go ahead|done|send it)\b",
    re.IGNORECASE,
)
_TIP_TURN_CAP = 12

# Deterministic entry backstop (the "A tip to share" CTA), so it engages even without
# the FE intent_hint. Matches SHARING a recommendation — not seeking one.
_ENTRY_RE = re.compile(
    r"\b(tip to share|recommend(?:ation)?|i'?d? recommend|you should try|"
    r"a (?:great|good) (?:place|spot|doctor|dentist)|share a tip)\b",
    re.IGNORECASE,
)


def looks_like_tip_share_entry(message: str) -> bool:
    """True when the message looks like the user wants to share a recommendation."""
    return bool(_ENTRY_RE.search(str(message or "").strip()))


_TIP_VALUE_FIELDS = ("name", "category", "trait", "locality")

_EXTRACT_SYSTEM = """You extract structured fields about a local recommendation (a "tip") \
a neighbor wants to share, and propose ONE smart follow-up question.

Return ONE compact JSON object with exactly these keys:
{"name","category","trait","locality","place_based","ask"}

- name: the specific who/where being recommended, e.g. "Dr. Sarah", "Canvas Restaurant", "Lake Nona Park". null if not stated.
- category: what kind of recommendation, e.g. "pediatric dentist","restaurant","playground","plumber","pediatrician". null if unclear.
- trait: why it's good / the standout detail, e.g. "twin-friendly","amazing tacos","gentle with toddlers". null if not stated.
- locality: neighborhood/area if mentioned, e.g. "Lake Nona". null otherwise.
- place_based: true if this is a PLACE or business you could find on a map (restaurant, park, clinic, salon);
  false if it's a person/word-of-mouth service with no fixed public listing (a nanny, a handyman by referral).
- ask: the single MOST useful follow-up to make this a strong tip, TAILORED to what's still unknown,
  with tappable answers that fit (e.g. cuisine for a restaurant, age-fit for a doctor). Shape:
  {"field": <short snake_case key>, "question": <one short question>, "options": [2-4 short answers]}.
  Return null for `ask` when name + category + trait is already enough. Do NOT ask for a phone number.

Use null for any string the text does not support, false for place_based when unsure. Never invent a value."""


def _extract_tip_fields(
    *, history: list[dict[str, Any]], user_message: str, prev: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """LLM structured extraction. Returns (fields_found, ask). ({}, None) on failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return {}, None
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        known = {k: prev.get(k) for k in _TIP_VALUE_FIELDS}
        known["details"] = prev.get("details") or []
        payload = "\n\n".join(
            [
                "CURRENT TIP DRAFT (merge updates into this):\n"
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
        for k in _TIP_VALUE_FIELDS:
            v = data.get(k)
            if isinstance(v, str) and v.strip() and v.strip().lower() != "null":
                out[k] = v.strip()
        if isinstance(data.get("place_based"), bool):
            out["place_based"] = data["place_based"]
        ask = data.get("ask")
        if isinstance(ask, dict):
            q = str(ask.get("question") or "").strip()
            opts = [
                str(o).strip()
                for o in (ask.get("options") or [])
                if isinstance(o, str) and str(o).strip()
            ][:4]
            ask = {"field": str(ask.get("field") or "detail").strip(), "question": q, "options": opts} if (q and len(opts) >= 2) else None
        else:
            ask = None
        return out, ask
    except Exception:  # noqa: BLE001 - extraction is best-effort
        import logging

        logging.getLogger(__name__).exception("tip_share_extract_failed")
        return {}, None


def _has(draft: dict[str, Any], key: str) -> bool:
    return bool(str(draft.get(key) or "").strip())


def _build_chips(draft: dict[str, Any]) -> list[dict[str, str]]:
    """The 'Heard you' chips — ★ Recommendation + category + trait + details. Tap a chip
    to correct that field (FE sends `fix:<field>`)."""
    chips: list[dict[str, str]] = [{"label": "★ Recommendation", "tone": "amber", "field": "category"}]
    if _has(draft, "category"):
        chips.append({"label": str(draft["category"]), "tone": "sky", "field": "category"})
    if _has(draft, "trait"):
        chips.append({"label": str(draft["trait"]), "tone": "coral", "field": "trait"})
    for d in (draft.get("details") or []):
        if str(d).strip():
            chips.append({"label": str(d).strip(), "tone": "violet", "field": "details"})
    if _has(draft, "locality"):
        chips.append({"label": str(draft["locality"]), "tone": "green", "field": "locality"})
    return chips


def _question_for_field(field: str) -> tuple[str, list[str]]:
    if field == "name":
        return "Who or where? A name helps me find them.", []
    if field == "category":
        return "What kind of recommendation is it?", _CATEGORY_SUGGESTIONS
    if field == "trait":
        return _TRAIT_PROMPT, []
    if field == "details":
        return "What detail should I update?", []
    return "What should I change?", []


def _summary(draft: dict[str, Any]) -> str:
    bits = [str(draft.get("name") or "your recommendation").strip()]
    if _has(draft, "category"):
        bits.append(str(draft["category"]))
    if _has(draft, "trait"):
        bits.append(str(draft["trait"]))
    return " · ".join(bits)


def _detail_text(draft: dict[str, Any]) -> str:
    parts = [str(draft.get("name") or "").strip()]
    if _has(draft, "category"):
        parts.append(str(draft["category"]).strip())
    if _has(draft, "trait"):
        parts.append(str(draft["trait"]).strip())
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    if _has(draft, "locality"):
        parts.append(str(draft["locality"]).strip())
    return " · ".join([p for p in parts if p]) or str(draft.get("name") or "recommendation")


def _name_suggestions(draft: dict[str, Any], *, zip_code: str | None, block_id: str | None) -> list[str]:
    """Real nearby places (Google Places) matching the category — only for place-based
    tips. [] otherwise, so the flow falls back to free-type."""
    if not draft.get("place_based") or not _has(draft, "category"):
        return []
    try:
        from app.places import nearby_place_suggestions

        return nearby_place_suggestions(
            query=str(draft["category"]), zip_code=zip_code, block_id=block_id
        )
    except Exception:  # noqa: BLE001
        return []


def _save_tip(
    *, draft: dict[str, Any], user_jwt: str, block_id: str | None, zip_code: str | None
) -> dict[str, Any] | None:
    try:
        from app.local_signals import save_local_signal

        return save_local_signal(
            user_jwt,
            intent="tip_share",
            detail_text=_detail_text(draft),
            category=str(draft.get("category") or "").strip() or None,
            block_id=block_id,
            zip_code=zip_code,
        )
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("tip_share_save_failed")
        return None


# What this capture OWNS: the user sharing a local tip/recommendation (tip_share). Anything
# the AI confidently reads as a different lane — a search, a swap, out_of_scope, unsafe — is
# a pivot and releases. Self-maintaining via is_confident_off_lane (no foreign-list).
_TIP_SHARE_NATIVE_GOALS = frozenset({"save_signal"})
_TIP_SHARE_NATIVE_SIGNALS = frozenset({"tip_share"})
_TIP_SHARE_NATIVE_LINEARS = frozenset({"sharing.tip"})


def _is_tip_share_answer(
    message: str, session_ctx: dict[str, Any], slots: "dict[str, Any] | None"
) -> bool:
    """Is this turn a genuine answer/refine for the tip-share capture's current step?"""
    from app.lane_decision import is_confident_off_lane, is_meta_or_chat

    if is_meta_or_chat(slots):
        return False
    return not is_confident_off_lane(
        slots,
        native_goals=_TIP_SHARE_NATIVE_GOALS,
        native_signals=_TIP_SHARE_NATIVE_SIGNALS,
        native_linears=_TIP_SHARE_NATIVE_LINEARS,
    )


def tip_share_should_release(
    message: str, session_ctx: dict[str, Any], slots: "dict[str, Any] | None" = None
) -> bool:
    """Release the sticky tip-share flow on a semantic abandon or a confident pivot to
    another intent (AI's read, not keywords), so the user is never trapped."""
    from app.lane_decision import lane_should_continue

    return not lane_should_continue(
        message, session_ctx, slots, is_valid_answer=_is_tip_share_answer
    )


def reset_tip_share_state(session_ctx: dict[str, Any]) -> None:
    """Drop the tip-share flow + its half-built draft so the turn falls through to normal
    routing. Keys set to None (not popped) so the {**old, **new} session merge clears them."""
    for k in (
        "tip_share_active",
        "tip_draft",
        "tip_ready",
        "tip_pending_ask",
        "tip_asked_fields",
    ):
        session_ctx[k] = None
    session_ctx["tip_enrich_count"] = 0
    session_ctx["tip_turns"] = 0


def run_tip_share_turn(
    *,
    user_message: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_jwt: str,
    home_block_id: str | None,
) -> str:
    """Drive one tip-share capture turn. Mutates session_ctx (tip_draft,
    tip_share_active, tip_listed_now, routing_phase). Returns Lana's reply."""
    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("tip_draft") or {})
    zip_code = str(session_ctx.get("zip_code") or "").strip() or None
    session_ctx["tip_listed_now"] = False

    # ── Loop safety ──
    turns = int(session_ctx.get("tip_turns") or 0) + 1
    session_ctx["tip_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _TIP_TURN_CAP:
        for k in ("tip_share_active", "tip_draft", "tip_ready", "tip_pending_ask", "tip_enrich_count", "tip_asked_fields"):
            session_ctx[k] = None
        session_ctx["tip_turns"] = 0
        session_ctx["routing_phase"] = "listening"
        return "No problem — we can do that another time. What else can I help with?"

    # ── The "Pass the tip along" CTA on the ready card → save it ──
    if session_ctx.get("tip_ready") and _PASS_RE.search(msg):
        saved = _save_tip(draft=draft, user_jwt=user_jwt, block_id=home_block_id, zip_code=zip_code)
        for k in ("tip_share_active", "tip_ready", "tip_pending_ask"):
            session_ctx[k] = None
        session_ctx["tip_turns"] = 0
        session_ctx["tip_enrich_count"] = 0
        session_ctx["routing_phase"] = "listening"
        if not saved:
            session_ctx["tip_draft"] = None
            return "I couldn't post that just now — let's try again in a moment."
        matches = int(saved.get("matches_created") or 0)
        draft["signal_id"] = saved.get("signal_id")
        draft["listed"] = True
        draft["chips"] = _build_chips(draft)
        session_ctx["tip_draft"] = draft
        session_ctx["tip_listed_now"] = True
        summary = _summary(draft)
        tail = (
            f" {matches} neighbor{'s' if matches != 1 else ''} asking for this just got it."
            if matches
            else " I'll pass it on when a neighbor asks for one."
        )
        facts = [f"The tip just posted: {summary}"]
        if matches:
            facts.append(
                f"{matches} neighbor{'s' if matches != 1 else ''} asking for this just got it"
            )
        else:
            facts.append("Lana will pass the tip on when a neighbor asks for one")
        return compose_reply(
            goal=(
                "Confirm the user's tip was just posted and is now out to nearby neighbors — "
                "brief, warm celebration."
            ),
            facts=facts,
            fallback=f"🎉 Done — **{summary}** is posted for your neighbors.{tail}",
        )

    # ── Correction: chip tap "fix:<field>" → clear + re-ask that field ──
    fix = re.match(r"\s*fix:(\w+)\s*$", msg)
    if fix:
        field = fix.group(1)
        if field == "details":
            draft["details"] = []
            session_ctx["tip_pending_ask"] = "details"
            session_ctx["tip_enrich_count"] = 0
        elif field in _TIP_VALUE_FIELDS:
            draft.pop(field, None)
        session_ctx["tip_ready"] = None
        q, opts = _question_for_field(field)
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = opts
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Sure — {q}"

    # ── Capture a pending enrichment / name answer into the right place ──
    pending = session_ctx.get("tip_pending_ask")
    if pending and msg and not _PASS_RE.search(msg):
        details = list(draft.get("details") or [])
        if msg not in details:
            details.append(msg)
        draft["details"] = details
        session_ctx["tip_pending_ask"] = None

    # ── Extract fields + tailored follow-up ──
    ask: dict[str, Any] | None = None
    if msg:
        found, ask = _extract_tip_fields(history=history, user_message=msg, prev=draft)
        for k, v in found.items():
            draft[k] = v

    # ── P1: nothing yet → "What do you want to recommend?" ──
    if not _has(draft, "name") and not _has(draft, "category"):
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["routing_phase"] = "listening"
        return "Love that — what do you want to recommend?"

    chips = _build_chips(draft)

    # ── P3: need the who/where (name) — Places options when place-based ──
    if not _has(draft, "name"):
        sugg = _name_suggestions(draft, zip_code=zip_code, block_id=home_block_id)
        draft["chips"] = chips
        draft["suggestions"] = sugg
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["routing_phase"] = "listening"
        if sugg:
            return f"Heard you — **{_summary(draft)}**. Who or where? A few near you, or tell me:"
        return f"Heard you — **{_summary(draft)}**. Who or where? A name helps me find them."

    # ── need the category ──
    if not _has(draft, "category"):
        draft["chips"] = chips
        draft["suggestions"] = _CATEGORY_SUGGESTIONS
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. What kind of recommendation is it?"

    # ── AI-tailored enrichment (cuisine / age-fit / why-great), capped. Never re-ask a
    # field already asked: a non-matching answer ("great for toddlers" to "Which
    # community center?") makes the model re-propose the same question — an identical
    # re-ask loop. ──
    enrich_count = int(session_ctx.get("tip_enrich_count") or 0)
    asked_fields = set(session_ctx.get("tip_asked_fields") or [])
    if ask and ask["field"] not in asked_fields and enrich_count < _MAX_ENRICH:
        asked_fields.add(ask["field"])
        session_ctx["tip_asked_fields"] = list(asked_fields)
        session_ctx["tip_pending_ask"] = ask["field"]
        session_ctx["tip_enrich_count"] = enrich_count + 1
        draft["chips"] = chips
        draft["suggestions"] = ask["options"]
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. {ask['question']}"

    # ── P4: ready → assembled card + dual CTA (saved only when they confirm) ──
    draft["chips"] = chips
    draft["suggestions"] = []
    draft["ready"] = True
    session_ctx["tip_draft"] = draft
    session_ctx["tip_share_active"] = True
    session_ctx["tip_ready"] = True
    session_ctx["routing_phase"] = "listening"
    summary = _summary(draft)
    return compose_reply(
        goal=(
            "The tip draft is complete and shown as a card. Tell the user you'll pass it on when "
            "a neighbor asks, and prompt them to tap **Pass the tip along** (keep that button "
            "name verbatim, bolded) to post it, or send it to a neighbor they know."
        ),
        facts=[f"Tip ready: {summary}"],
        fallback=(
            f"Got it — **{summary}**. I'll pass it on when a neighbor asks. "
            "**Pass the tip along** to post it for your neighbors, or send it to a neighbor you know."
        ),
    )
