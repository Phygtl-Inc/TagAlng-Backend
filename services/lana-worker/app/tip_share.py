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
import uuid
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
_TIP_TURN_CAP = 24  # 8 carousel steps + name/type + corrections

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


_TIP_VALUE_FIELDS = ("name", "category", "trait", "locality", "reco_type")

_EXTRACT_SYSTEM = """You extract structured fields about a local recommendation (a "tip") \
a neighbor wants to share, and propose ONE smart follow-up question.

Return ONE compact JSON object with exactly these keys:
{"name","category","trait","locality","reco_type","place_based","answers",<<STEPS_KEY>>"ask"}

- name: the specific who/where being recommended, e.g. "Dr. Sarah", "Canvas Restaurant", "Lake Nona Park". null if not stated.
- category: what kind of recommendation, e.g. "pediatric dentist","restaurant","playground","plumber","pediatrician". null if unclear.
- trait: why it's good / the standout detail, e.g. "twin-friendly","amazing tacos","gentle with toddlers". null if not stated.
- locality: neighborhood/area if mentioned, e.g. "Lake Nona". null otherwise.
- reco_type: EXACTLY one of <<TYPES>>, or null if genuinely unclear. <<TYPE_RULES>>
- answers: object mapping any of the CURRENT TYPE FIELDS listed below to what the user ALREADY said,
  verbatim-ish and short. Omit a field rather than guess it. {{}} when nothing was said.
<<STEPS_SPEC>>- place_based: true if this is a PLACE or business you could find on a map (restaurant, park, clinic, salon);
  false if it's a person/word-of-mouth service with no fixed public listing (a nanny, a handyman by referral).
- ask: the single MOST useful follow-up to make this a strong tip, TAILORED to what's still unknown,
  with tappable answers that fit (e.g. cuisine for a restaurant, age-fit for a doctor). Shape:
  {"field": <short snake_case key>, "question": <one short question>, "options": [2-4 short answers]}.
  Return null for `ask` when name + category + trait is already enough. Do NOT ask for a phone number.

Use null for any string the text does not support, false for place_based when unsure. Never invent a value."""


_STEPS_SPEC = """- steps: the question set for THIS recommendation — 6-8 questions, in the order to ask them.
  Each: {"field": short snake_case key, "label": 1-2 word eyebrow, "question": ONE short
  question ending in "?", "placeholder": a short example answer for THIS subject,
  "options": [2-4 short taps] when the answer really is a small closed set}.
  The FIRST step is ALWAYS {"field": "subject"} — what/who is being recommended, phrased
  for THIS subject: "Which trampoline park?", "What's the kettle called?", "What's the
  doctor's name?". Never "who or where". Write it even when the name is already known; it
  is shown answered rather than asked.
  Then the type's own basics: <<FLOOR>>.
  Then at least THREE questions that are specific to this subject and that a neighbour
  would FILTER or SEARCH on later — the facts that decide whether this recommendation is
  for them. A pediatrician: which ages, does she take walk-ins, which insurance, weekend
  hours. A biryani place: how spicy, halal, delivery, wait at peak. A stroller: which car
  seat it clicks into, weight, fits a subway turnstile. Answerable in a few words, and
  prefer `options` — a closed set becomes a filter, a paragraph never does.
  BANNED (they read well and filter nothing): "what stood out", "what did you like", "why
  is it good", "anything else", "tell me more", anything already in CURRENT TIP DRAFT (the
  user's own trait is captured — do not ask for it again), and any two questions that would
  take the same answer.
  Never ask for a home address, a full name, or anything private.
  Do NOT include "can neighbours ask you more" or "what did others say" — both are added
  for you. Return [] when CURRENT TYPE FIELDS below already lists a set.
"""


def _extract_system(*, want_steps: bool) -> str:
    """The extraction prompt with the type list + the current type's fields injected.

    `want_steps` drops the whole question-set spec once the set exists: the set is written
    ONCE per recommendation, so re-requesting it every turn would both burn tokens and let
    the wording drift under a user who is halfway through answering it.
    """
    from app.reco_question_sets import FLOOR_RULES, RECO_TYPES, TYPE_RULES

    return (
        _EXTRACT_SYSTEM.replace("<<TYPES>>", "|".join(RECO_TYPES))
        .replace("<<TYPE_RULES>>", TYPE_RULES)
        .replace("<<STEPS_KEY>>", '"steps",' if want_steps else "")
        .replace("<<STEPS_SPEC>>", _STEPS_SPEC.replace("<<FLOOR>>", FLOOR_RULES) if want_steps else "")
    )


def _extract_tip_fields(
    *,
    history: list[dict[str, Any]],
    user_message: str,
    prev: dict[str, Any],
    lang: str | None = None,
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
        known["answers"] = prev.get("answers") or {}
        step_set = step_set_of(prev)
        want_steps = not prev.get("step_set")
        fields = [f"{s['field']}: {s['question']}" for s in step_set]
        lang_line = (
            f"WRITE EVERY label, question AND placeholder IN {lang}. The `field` keys stay "
            "snake_case English — they are storage keys, not copy."
            if want_steps and lang
            else ""
        )
        payload = "\n\n".join(
            [
                p
                for p in [lang_line]
                if p
            ]
            + [
                "CURRENT TIP DRAFT (merge updates into this):\n"
                + json.dumps(known, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                "CURRENT TYPE FIELDS (targets for `answers`):\n"
                + ("\n".join(fields) or "(type not known yet — return {} for answers)"),
                f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_extract_system(want_steps=want_steps),
            user_payload=payload,
            max_tokens=1100 if want_steps else 320,
            # Warmer only on the turn that WRITES the set: at 0.2 every doctor got the same
            # five questions (dev QA 2026-09-03). Extraction on that turn is the easy part —
            # one sentence, nothing to disambiguate yet.
            temperature=0.5 if want_steps else 0.2,
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
        if want_steps and isinstance(data.get("steps"), list):
            out["steps_raw"] = data["steps"]
        if isinstance(data.get("answers"), dict):
            out["answers"] = {
                str(k): v.strip()
                for k, v in data["answers"].items()
                if isinstance(v, str) and v.strip() and v.strip().lower() != "null"
            }
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


def step_set_of(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The recommendation's question set: the one Lana wrote for it, or the type's static
    set until she has (and if generation fails, forever — the flow never blocks on it)."""
    generated = draft.get("step_set")
    if isinstance(generated, list) and generated:
        return generated
    from app.reco_question_sets import steps_for

    return steps_for(draft.get("reco_type"))


def _reco_tallies(*, user_jwt: str, block_id: str | None, name: Any) -> list[dict[str, Any]]:
    """What OTHER neighbours already logged about this same subject, for the closing
    "others also said · tap to agree" step. Best-effort: no tallies, no step."""
    if not block_id or not str(name or "").strip():
        return []
    try:
        from app.local_signals import fetch_reco_tallies

        return fetch_reco_tallies(user_jwt, block_id=block_id, subject=str(name))
    except Exception:  # noqa: BLE001
        return []


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
    # No `name` case: the name is the subject STEP, and `fix:name` is remapped to it.
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
    from app.reco_question_sets import SUBJECT_FIELD, TAIL_FIELDS, carousel

    parts = [str(draft.get("name") or "").strip()]
    if _has(draft, "category"):
        parts.append(str(draft["category"]).strip())
    if _has(draft, "trait"):
        parts.append(str(draft["trait"]).strip())
    # TAIL_FIELDS out: the consent toggle and the agree row are how the AUTHOR answered
    # Lana, not part of the recommendation — "Neighbours: Keep it to the card" read as a
    # tip detail on the neighbour's card, and detail_text is also the dedupe key.
    parts += [
        f"{s['label']}: {s['answer']}"
        for s in carousel(step_set_of(draft), draft.get("answers"))
        # SUBJECT out as well as the tail: it is `name`, already the first part above.
        if s.get("answer") and s["field"] not in (*TAIL_FIELDS, SUBJECT_FIELD)
    ]
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    if _has(draft, "locality"):
        parts.append(str(draft["locality"]).strip())
    return " · ".join([p for p in parts if p]) or str(draft.get("name") or "recommendation")


def _name_suggestions(draft: dict[str, Any], *, zip_code: str | None, block_id: str | None) -> list[str]:
    """Real nearby places (Google Places) matching the category — only for place-based
    tips. [] otherwise, so the flow falls back to free-type."""
    from app.reco_question_sets import subject_is_place

    if not _has(draft, "category"):
        return []
    # `place_based` is the extractor's guess and it says false for a restaurant often
    # enough; the TYPE is the reliable signal for the subject step.
    if not draft.get("place_based") and not subject_is_place(draft.get("reco_type")):
        return []
    try:
        from app.places import nearby_place_suggestions

        return nearby_place_suggestions(
            query=str(draft["category"]), zip_code=zip_code, block_id=block_id
        )
    except Exception:  # noqa: BLE001
        return []


def _reco_fields(draft: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The answered steps, self-describing. An array and not {field: answer} because the
    questions are generated per recommendation: store the answer alone and nothing can ever
    say again that "helped_with" was asked as "What did she help with?" — the reader card
    would have answers with no labels."""
    from app.reco_question_sets import SUBJECT_FIELD, carousel

    out = [
        {
            "field": s["field"],
            "label": s["label"],
            "question": s["question"],
            "kind": s.get("kind") or "text",
            "answer": s["answer"],
        }
        for s in carousel(step_set_of(draft), draft.get("answers"))
        if s.get("answer") and s["field"] != SUBJECT_FIELD
    ]
    return out or None


def _description(draft: dict[str, Any]) -> str | None:
    """Why it is worth recommending, in the author's OWN words — the italic line on the
    card. The trait plus any corrections they added, and nothing generated: a description
    Lana wrote would be Lana vouching for a place she has never been to."""
    parts = [str(draft.get("trait") or "").strip()]
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    return " · ".join([p for p in parts if p]) or None


def _save_tip(
    *, draft: dict[str, Any], user_jwt: str, block_id: str | None, zip_code: str | None
) -> tuple[dict[str, Any] | None, str]:
    """(saved_row, error_detail). The reason comes back so the caller can recover from the
    one failure that is fixable in-turn (block_required) instead of just apologising."""
    try:
        from app.local_signals import save_local_signal

        return save_local_signal(
            user_jwt,
            intent="tip_share",
            detail_text=_detail_text(draft),
            category=str(draft.get("category") or "").strip() or None,
            block_id=block_id,
            zip_code=zip_code,
            reco_type=draft.get("reco_type"),
            reco_fields=_reco_fields(draft),
            # What the agree-row tallies group on — the subject, normalized once at write
            # time so a lookup is an index hit and not a scan over every recommendation.
            reco_subject=str(draft.get("name") or "").strip() or None,
            # The card head as fields. Same values detail_text joins into a sentence —
            # stored separately so a reader renders them instead of splitting on " · ".
            reco_name=str(draft.get("name") or "").strip() or None,
            reco_place=str(draft.get("locality") or "").strip() or None,
            reco_description=_description(draft),
        ), ""
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("tip_share_save_failed")
        return None, str(getattr(exc, "detail", "") or exc).lower()


# What this capture OWNS: the user sharing a local tip/recommendation (tip_share). Anything
# the AI confidently reads as a different lane — a search, a swap, out_of_scope, unsafe — is
# a pivot and releases. Self-maintaining via is_confident_off_lane (no foreign-list).
_TIP_SHARE_NATIVE_GOALS = frozenset({"save_signal"})
_TIP_SHARE_NATIVE_SIGNALS = frozenset({"tip_share"})
_TIP_SHARE_NATIVE_LINEARS = frozenset({"sharing.tip"})


def _reply_is_an_offered_option(message: str, session_ctx: dict[str, Any]) -> bool:
    """The user answered with one of the options Lana just put in front of them (typed or
    chip-tapped). Picking Lana's OWN suggestion is definitionally an answer to Lana's own
    question, whatever a stateless read of the bare words ("family doctor") looks like —
    the same rule the browse clarifier uses for offered chips. Deliberately an EXACT match:
    the safety here comes from the strictness, so anything the user composed themselves
    still goes to the classifier and can still pivot the lane."""
    msg = re.sub(r"[\s.!?,]+", " ", str(message or "").strip().lower()).strip()
    if not msg:
        return False
    draft = session_ctx.get("tip_draft")
    options = (draft or {}).get("suggestions") if isinstance(draft, dict) else None
    for opt in options or []:
        norm = re.sub(r"[\s.!?,]+", " ", str(opt or "").strip().lower()).strip()
        if norm and norm == msg:
            return True
    return False


def _is_tip_share_answer(
    message: str, session_ctx: dict[str, Any], slots: "dict[str, Any] | None"
) -> bool:
    """Is this turn a genuine answer/refine for the tip-share capture's current step?"""
    from app.lane_decision import is_confident_off_lane, is_meta_or_chat

    # Checked before the classifier's read: the share flow asks tailored questions whose
    # answers are bare category fragments, and read on their own those fragments look like
    # a fresh recommendation ASK — which released the lane mid-share and answered the
    # user's own tip with Google listings (dev QA 2026-08-05).
    if _reply_is_an_offered_option(message, session_ctx):
        return True
    if is_meta_or_chat(slots):
        return False
    return not is_confident_off_lane(
        slots,
        native_goals=_TIP_SHARE_NATIVE_GOALS,
        native_signals=_TIP_SHARE_NATIVE_SIGNALS,
        native_linears=_TIP_SHARE_NATIVE_LINEARS,
    )


def _pending_step(session_ctx: dict[str, Any]) -> dict[str, Any] | None:
    """The step whose question the previous turn asked, or None."""
    field = str(session_ctx.get("tip_pending_ask") or "")
    draft = session_ctx.get("tip_draft")
    if not field or not isinstance(draft, dict):
        return None
    return next((s for s in step_set_of(draft) if s.get("field") == field), None)


def tip_share_should_release(
    message: str, session_ctx: dict[str, Any], slots: "dict[str, Any] | None" = None
) -> bool:
    """Release the sticky tip-share flow on a semantic abandon or a confident pivot to
    another intent (AI's read, not keywords), so the user is never trapped."""
    from app.lane_decision import lane_should_continue

    # A no to Lana's OWN closing yes/no ("Can neighbours ask you more?" → "naah, I don't
    # want them to") is the ANSWER to that step, but it reads as an abandon to the
    # classifier — which released the lane on the very last question and dropped a finished
    # tip into the old signal cascade (dev QA 2026-09-02). Scoped to the toggle: every other
    # step takes free text, where a real abandon still has to release.
    step = _pending_step(session_ctx)
    if step and step.get("kind") == "toggle":
        return False
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
        "tip_pending_question",
        "tip_asked_fields",
        "tip_need_zip",
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
    from app.discovery_route import resolve_block_id
    from app.reco_question_sets import SUBJECT_FIELD

    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("tip_draft") or {})
    # Re-stamped below by whichever branch asks something. The chat fork sends one question
    # as prose, so without this the FE cannot tell WHICH step is open and renders a text box
    # for a `place` step instead of the Places picker.
    draft.pop("pending_field", None)
    # One id per recommendation, for the whole draft's life — see TipDraft.draft_id. Cleared
    # with the draft itself, so the next recommendation gets a new one and the FE knows the
    # cards-or-chat pick does not carry over.
    if not draft.get("draft_id"):
        draft["draft_id"] = uuid.uuid4().hex[:12]
    zip_code = str(session_ctx.get("zip_code") or session_ctx.get("zip") or "").strip() or None
    # The block the tip is posted to — resolved the way every other save path resolves it,
    # not the raw home block. A session whose block lives in the session (browsed an area,
    # or signed up and the home block landed after this turn started) passes home_block_id
    # None, and save_local_signal raises block_required: the tip died at the finish line
    # with the ready card already on screen (dev QA 2026-09-03).
    block_id = resolve_block_id(session_ctx, home_block_id)
    session_ctx["tip_listed_now"] = False

    # ── Loop safety ──
    turns = int(session_ctx.get("tip_turns") or 0) + 1
    session_ctx["tip_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _TIP_TURN_CAP:
        for k in ("tip_share_active", "tip_draft", "tip_ready", "tip_pending_ask", "tip_pending_question", "tip_enrich_count", "tip_asked_fields", "tip_need_zip"):
            session_ctx[k] = None
        session_ctx["tip_turns"] = 0
        session_ctx["routing_phase"] = "listening"
        return "No problem — we can do that another time. What else can I help with?"

    # ── The ZIP asked for after a block_required post failure ──
    # A tip is posted TO a block and this account has none: the ZIP the user typed was read
    # as a card answer by the extractor, so every retry failed the same way (dev QA
    # 2026-09-03, "block_required" twice in a row). Answering the ask posts the tip on the
    # spot — they already tapped the CTA once.
    posting = bool(_PASS_RE.search(msg))
    if session_ctx.get("tip_need_zip") and msg:
        from app.discovery_route import (
            ensure_home_block_for_verified_user,
            extract_zip,
        )

        zip5 = extract_zip(msg)
        if zip5:
            session_ctx["zip_code"] = zip5
            session_ctx["tip_need_zip"] = None
            zip_code = zip5
            try:
                block_id = (
                    ensure_home_block_for_verified_user(user_jwt, session_ctx=session_ctx)
                    or block_id
                )
            except Exception:  # noqa: BLE001
                pass
            posting = True

    # ── The "Pass the tip along" CTA on the ready card → save it ──
    if session_ctx.get("tip_ready") and posting:
        saved, err = _save_tip(
            draft=draft, user_jwt=user_jwt, block_id=block_id, zip_code=zip_code
        )
        if not saved and "block_required" in err:
            # The one fixable failure: no home block yet. Same helper the pipeline runs for
            # a freshly verified user, then one retry — re-asking for a ZIP they already
            # gave at signup, on top of a finished card, is not a recovery.
            from app.discovery_route import ensure_home_block_for_verified_user

            try:
                block_id = ensure_home_block_for_verified_user(
                    user_jwt, session_ctx=session_ctx
                )
            except Exception:  # noqa: BLE001
                block_id = None
            if block_id:
                saved, err = _save_tip(
                    draft=draft, user_jwt=user_jwt, block_id=block_id, zip_code=zip_code
                )
        if not saved:
            # The card and the draft STAY. The copy has always said "let's try again" while
            # dropping the draft in the same breath, so there was nothing left to try: the
            # next message fell through to the old signal cascade and the whole answered
            # question set was gone (dev QA 2026-09-03).
            session_ctx["tip_draft"] = draft
            session_ctx["tip_share_active"] = True
            session_ctx["tip_ready"] = True
            session_ctx["routing_phase"] = "listening"
            no_area = "block_required" in err
            session_ctx["tip_need_zip"] = True if no_area else None
            return compose_reply(
                goal=(
                    "Ask which 5-digit ZIP they are in — you cannot post a tip without "
                    "knowing which area it belongs to. Say the card is still here and "
                    "that you will post it the moment they tell you: they do NOT have to "
                    "tap anything again."
                    if no_area
                    else "The tip could not be posted. Say so plainly, tell them the card "
                    "is still here, and ask them to tap **Pass the tip along** to try "
                    "again (keep that button name verbatim, bolded)."
                ),
                facts=[
                    f"The tip is still a draft: {_summary(draft)}",
                    "Nothing was lost — the card is still on screen",
                ]
                + (
                    ["Lana does not know which area to post it to yet"]
                    if no_area
                    else []
                ),
                fallback=(
                    "Almost — which ZIP are you in? Tell me and I'll post this straight "
                    "away."
                    if no_area
                    else "I couldn't post that just now — the card is still here, tap "
                    "**Pass the tip along** to try again."
                ),
            )
        for k in ("tip_share_active", "tip_ready", "tip_pending_ask", "tip_pending_question"):
            session_ctx[k] = None
        session_ctx["tip_turns"] = 0
        session_ctx["tip_enrich_count"] = 0
        session_ctx["routing_phase"] = "listening"
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
        if field == "name":
            # The name IS the subject step now, so a name chip re-opens that step with its
            # own generated question instead of the old type-blind "Who or where?".
            field = SUBJECT_FIELD
            draft.pop("name", None)
        if field == "details":
            draft["details"] = []
            session_ctx["tip_pending_ask"] = "details"
            session_ctx["tip_enrich_count"] = 0
        elif field in _TIP_VALUE_FIELDS:
            draft.pop(field, None)
        step = None
        if field not in _TIP_VALUE_FIELDS and field != "details":
            step = next((s for s in step_set_of(draft) if s["field"] == field), None)
            if step:
                draft["answers"] = {
                    k: v for k, v in (draft.get("answers") or {}).items() if k != field
                }
                session_ctx["tip_asked_fields"] = [
                    f for f in (session_ctx.get("tip_asked_fields") or []) if f != field
                ]
                # The re-answer has to land back on THIS step. Without the pending ask it
                # went to the extractor's mercy: the reply to a re-opened "What did she help
                # with?" was a bare fragment with nothing marking which step it belonged to.
                session_ctx["tip_pending_ask"] = field
        session_ctx["tip_ready"] = None
        # A step is re-asked with its OWN question. `_question_for_field` only knows the four
        # draft-level fields, so every step tap used to come back as "What should I change?" —
        # which reads like Lana forgot what the user just tapped.
        q, opts = (
            (str(step["question"]), list(step.get("options") or []))
            if step
            else _question_for_field(field)
        )
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = opts
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["tip_pending_question"] = q
        session_ctx["routing_phase"] = "listening"
        return f"Sure — {q}"

    # ── Capture a pending enrichment / name answer into the right place ──
    pending = session_ctx.get("tip_pending_ask")
    if pending and msg and not _PASS_RE.search(msg):
        step_fields = {s["field"] for s in step_set_of(draft)}
        if pending in step_fields:
            draft["answers"] = {**(draft.get("answers") or {}), str(pending): msg}
        else:
            details = list(draft.get("details") or [])
            if msg not in details:
                details.append(msg)
            draft["details"] = details
        session_ctx["tip_pending_ask"] = None

    # ── Extract fields + tailored follow-up ──
    ask: dict[str, Any] | None = None
    if msg:
        from app.i18n import lang_display_name, session_lang

        code = session_lang(session_ctx)
        found, ask = _extract_tip_fields(
            history=history,
            user_message=msg,
            prev=draft,
            lang=lang_display_name(code) if code else None,
        )
        merged_answers = {**(draft.get("answers") or {}), **(found.pop("answers", None) or {})}
        for k, v in found.items():
            draft[k] = v
        if merged_answers:
            draft["answers"] = merged_answers

    # ── subject ⇄ name ──
    # The subject step IS the name ask (see `head_step`), so its answer is the name — and a
    # name already in the opening sentence pre-answers the step, which is what stops the
    # carousel asking for what the user just said. The subject wins on conflict: the
    # extractor's read of "the new trampoline park" is a placeholder, the answer to the
    # step is the real thing.
    subject = str((draft.get("answers") or {}).get(SUBJECT_FIELD) or "").strip()
    if subject:
        draft["name"] = subject
    elif _has(draft, "name"):
        draft["answers"] = {
            SUBJECT_FIELD: str(draft["name"]).strip(),
            **(draft.get("answers") or {}),
        }

    # ── P1: nothing yet → "What do you want to recommend?" ──
    if not _has(draft, "name") and not _has(draft, "category"):
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["tip_pending_question"] = "What do you want to recommend?"
        session_ctx["routing_phase"] = "listening"
        return "Love that — what do you want to recommend?"

    chips = _build_chips(draft)

    # ── need the category ──
    if not _has(draft, "category"):
        draft["chips"] = chips
        draft["suggestions"] = _CATEGORY_SUGGESTIONS
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["tip_pending_question"] = "What kind of recommendation is it?"
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. What kind of recommendation is it?"

    # ── AI-tailored enrichment (cuisine / age-fit / why-great), capped. Never re-ask a
    # field already asked: a non-matching answer ("great for toddlers" to "Which
    # community center?") makes the model re-propose the same question — an identical
    # re-ask loop. ──
    from app.reco_question_sets import (
        carousel,
        missing_required,
        next_question,
        validate_steps,
    )

    reco_type = draft.get("reco_type")
    # ── The question set is written ONCE, here. Deliberately after the name/category gates
    # rather than the turn the type lands: the questions and their example placeholders are
    # about Dr. Sarah, not about dentists in general, and the tallies for the closing
    # "others also said" step need the subject to group on. `steps_raw` is whatever the
    # extractor proposed (possibly several turns ago); validate_steps is what makes it
    # askable, and falls back to the type's static set when there is nothing usable. ──
    if reco_type and not draft.get("step_set"):
        draft["step_set"] = validate_steps(
            draft.pop("steps_raw", None),
            reco_type,
            tallies=_reco_tallies(
                user_jwt=user_jwt, block_id=block_id, name=draft.get("name")
            ),
        )
    step_set = step_set_of(draft) if reco_type else []
    if step_set:
        steps = carousel(step_set, draft.get("answers"))
        draft["steps"] = steps
        draft["missing"] = missing_required(step_set, draft.get("answers"))
        asked = set(session_ctx.get("tip_asked_fields") or [])
        # "that's it / done" mid-carousel goes STRAIGHT to the ready card once the
        # required steps are in. A typed set is 7-8 steps deep, so without this the only
        # way out of a set the user considers finished is to answer every optional or
        # abandon the tip.
        done_early = bool(_PASS_RE.search(msg)) and not draft["missing"]
        # `asked` goes IN so the walk advances past optionals already offered.
        step = None if done_early else next_question(
            step_set, draft.get("answers"), asked=asked
        )
        if step:
            asked.add(step["field"])
            session_ctx["tip_asked_fields"] = list(asked)
            session_ctx["tip_pending_ask"] = step["field"]
            draft["pending_field"] = step["field"]
            draft["chips"] = chips
            # A generated set writes no options for a map step, and the chat fork has no
            # Places picker to fall back on the way the carousel does — so a "where is it?"
            # asked in chat comes back as typed prose nobody can navigate to. Real nearby
            # places, same call the name step makes.
            draft["suggestions"] = list(step.get("options") or []) or (
                _name_suggestions(draft, zip_code=zip_code, block_id=block_id)
                if step.get("kind") == "place"
                else []
            )
            session_ctx["tip_draft"] = draft
            session_ctx["tip_share_active"] = True
            session_ctx["tip_pending_question"] = step["question"]
            session_ctx["routing_phase"] = "listening"
            answered = sum(1 for s in steps if s.get("answer"))
            return (
                f"Heard you — **{_summary(draft)}**. {step['question']} "
                f"({answered + 1}/{len(steps)})"
            )

    enrich_count = int(session_ctx.get("tip_enrich_count") or 0)
    asked_fields = set(session_ctx.get("tip_asked_fields") or [])
    if not step_set and ask and ask["field"] not in asked_fields and enrich_count < _MAX_ENRICH:
        asked_fields.add(ask["field"])
        session_ctx["tip_asked_fields"] = list(asked_fields)
        session_ctx["tip_pending_ask"] = ask["field"]
        session_ctx["tip_enrich_count"] = enrich_count + 1
        draft["chips"] = chips
        draft["suggestions"] = ask["options"]
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        # The question verbatim, so next turn's router knows the bare fragment coming back
        # ("family doctor") is its ANSWER and not a fresh recommendation ask.
        session_ctx["tip_pending_question"] = str(ask["question"])
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. {ask['question']}"

    # ── P4: ready → assembled card + dual CTA (saved only when they confirm) ──
    draft["chips"] = chips
    draft["suggestions"] = []
    draft["ready"] = True
    session_ctx["tip_draft"] = draft
    session_ctx["tip_share_active"] = True
    session_ctx["tip_ready"] = True
    session_ctx["tip_pending_question"] = None  # nothing outstanding on the ready card
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
