"""Create-a-community capture — the recommendation capture's twin (C-CREATE-COMMUNITY).

Same four beats the tip capture has, same fork, same wire shape, so the FE renders it
with the components it already ships (see docs/CREATE_COMMUNITY_FLOW.md):

  P1  nothing yet            -> "What would you like to start a community around?"
  P2  type unknown           -> "What kind of place is it?" (circle-type chips)
  P3  the generated set      -> subject (Places picker) then Lana's own questions,
                                answered as cards or as chat — the user's pick
  P4  ready                  -> the assembled card + "Share with the community"

Why it is a separate module from `tip_share` even though the beats match: the two
PUBLISH into different worlds. A recommendation is one `local_signals` row scoped to a
block. A community is a canonical `places` row, a `circle_affiliations` link that makes
the creator a member, and one `place_features` row per answer — shared, permanent state
other people join. The questions are generated the same way (`build_step_set`); the
writes have nothing in common.

The place is MANDATORY and is never typed: the subject step returns a `google_place_id`
via /community-setup, because `add_circle` rejects a place-less create (2026-07-28
product decision) and an ungrounded community is invisible everywhere.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from app.reply_compose import compose_reply

logger = logging.getLogger(__name__)

# 5 steps + type + corrections. Same backstop the tip capture has: a set is ~7 steps, and
# past this the user is in a loop, not a conversation.
_COMMUNITY_TURN_CAP = 20

_CANCEL_RE = re.compile(
    r"\b(cancel|never\s*mind|nevermind|forget\s+it|stop|drop\s+it|not\s+now)\b", re.I
)
# "that's it / done" — jump to the ready card once the required steps are in.
_PASS_RE = re.compile(
    r"\b(that'?s\s+it|that\s+is\s+it|done|nothing\s+else|no\s+more|skip|pass|"
    r"i'?m\s+done|all\s+good|looks?\s+good)\b",
    re.I,
)
_PUBLISH_RE = re.compile(
    r"\b(share\s+(it\s+)?with\s+the\s+community|share\s+it|publish|post\s+it|"
    r"go\s+ahead|do\s+it|yes\s+please)\b",
    re.I,
)

# A create verb aimed at the WORD community. Structural and narrow on purpose — the
# classifier gets every other phrasing right, but a bare "I want to create a community"
# is the one utterance the hosting rule claims by name ("a bare 'I want to create an
# event' is STILL host_meet"), and losing it drops the user into the meet flow at the
# flow's own front door. Browse phrasings carry no create verb, so they never match:
# "show me communities around me", "communities I can join", "what communities am I in".
_CREATE_COMMUNITY_RE = re.compile(
    r"\b(?:creat\w*|start\w*|set(?:ting)?\s*up|add|make|open|launch\w*)\b[^.?!]{0,40}?"
    # NOT "a community event" — that is one gathering the community hosts, i.e. a meet.
    r"\bcommunit(?:y|ies)\b(?!\s+(?:event|meet|meetup|gathering|party))",
    re.I,
)


def looks_like_community_create(message: str) -> bool:
    """True when the words themselves say "turn something into a community"."""
    return bool(_CREATE_COMMUNITY_RE.search(str(message or "").strip()))


_VALUE_FIELDS = ("name", "circle_type", "blurb")

_EXTRACT_SYSTEM = """You extract structured fields about a LOCAL COMMUNITY a neighbor \
wants to create, and write the question set for it.

A community is a real PLACE people gather at — a bakery, a gym, a church, a school, a \
park. The neighbor is starting it so others can find and join it.

Return ONE compact JSON object with exactly these keys:
{"name","circle_type","blurb",<<STEPS_KEY>>"answers"}

- name: the place being made into a community, verbatim as they said it, e.g. "Rosetta's \
Bakery", "CF Fitness", "Lake Nona Park". null if not stated.
- circle_type: EXACTLY one of <<TYPES>>, or null if genuinely unclear. <<TYPE_RULES>>
- blurb: why people gather there, in THEIR words, e.g. "best sourdough on the block, \
everyone ends up there Saturday mornings". null if not stated.
- answers: object mapping any of the CURRENT SET FIELDS listed below to what the user \
ALREADY said, verbatim-ish and short. Omit a field rather than guess it. {{}} when \
nothing was said. NEVER write the "subject" field here — a place is only ever set by the \
map picker, never by text.
<<STEPS_SPEC>>
Use null for any string the text does not support. Never invent a value."""

_STEPS_SPEC = """- steps: the question set for THIS community — 4-6 questions, in the \
order to ask them.
  Each: {"field": short snake_case key, "label": 1-2 word eyebrow, "question": ONE short
  question ending in "?", "placeholder": a short example answer for THIS place,
  "options": [2-4 short taps] when the answer really is a small closed set}.
  The FIRST step is ALWAYS {"field": "subject"} — WHICH place it is, phrased for this kind
  of place: "Which bakery is it?", "Which gym?", "Where does the group meet?".
  Then the type's own basics: <<FLOOR>>.
  Then questions specific to THIS place that a neighbour deciding whether to show up would
  FILTER on — the facts that settle it. A bakery: which morning is busiest, is there a
  communal table, is it cash only, can you bring kids. A gym: which classes, is there a
  beginners' slot, do you need a membership. Answerable in a few words, and prefer
  `options` — a closed set becomes a filter, a paragraph never does.
  BANNED (they read well and filter nothing): "what's the vibe", "why do you like it",
  "tell me more", "anything else", anything already in CURRENT DRAFT (the user's own blurb
  is captured — do not ask for it again), and any two questions taking the same answer.
  Never ask for a home address, a full name, or anything private.
  Do NOT include "who should feel welcome here" — that closing step is added for you.
  Return [] when CURRENT SET FIELDS below already lists a set."""


def _extract_system(*, want_steps: bool) -> str:
    """The extraction prompt with the type list + the current set's fields injected.

    `want_steps` drops the whole question-set spec once the set exists: the set is written
    ONCE per community, so re-requesting it every turn would both burn tokens and let the
    wording drift under a user halfway through answering it.
    """
    from app.circles_capture import CIRCLE_TYPES
    from app.community_question_sets import (
        COMMUNITY_FLOOR_RULES,
        COMMUNITY_TYPE_RULES,
    )

    return (
        _EXTRACT_SYSTEM.replace("<<TYPES>>", "|".join(sorted(CIRCLE_TYPES)))
        .replace("<<TYPE_RULES>>", COMMUNITY_TYPE_RULES)
        .replace("<<STEPS_KEY>>", '"steps",' if want_steps else "")
        .replace(
            "<<STEPS_SPEC>>",
            _STEPS_SPEC.replace("<<FLOOR>>", COMMUNITY_FLOOR_RULES) if want_steps else "",
        )
    )


def step_set_of(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The community's question set: the one Lana wrote for it, or the type's static set
    until she has (and if generation fails, forever — the flow never blocks on it)."""
    generated = draft.get("step_set")
    if isinstance(generated, list) and generated:
        return generated
    from app.community_question_sets import community_steps_for

    return community_steps_for(draft.get("circle_type"))


def _extract_fields(
    *,
    history: list[dict[str, Any]],
    user_message: str,
    prev: dict[str, Any],
    lang: str | None = None,
) -> dict[str, Any]:
    """LLM structured extraction. Returns fields_found ({} on failure)."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return {}
        convo = "\n".join(
            f"{m.get('role', '?')}: {str(m.get('content') or '').strip()}"
            for m in (history or [])[-8:]
            if str(m.get("content") or "").strip()
        )
        known = {k: prev.get(k) for k in _VALUE_FIELDS}
        known["answers"] = prev.get("answers") or {}
        want_steps = not prev.get("step_set")
        fields = [f"{s['field']}: {s['question']}" for s in step_set_of(prev)]
        lang_line = (
            f"WRITE EVERY label, question AND placeholder IN {lang}. The `field` keys stay "
            "snake_case English — they are storage keys, not copy."
            if want_steps and lang
            else ""
        )
        payload = "\n\n".join(
            [p for p in [lang_line] if p]
            + [
                "CURRENT DRAFT (merge updates into this):\n"
                + json.dumps(known, ensure_ascii=False),
                "CONVERSATION SO FAR:\n" + (convo or "(none)"),
                "CURRENT SET FIELDS (targets for `answers`):\n"
                + ("\n".join(fields) or "(type not known yet — return {} for answers)"),
                f"USER'S NEW MESSAGE:\n{user_message.strip()}",
            ]
        )
        data = llm_json(
            model=synthesizer_model(),
            system=_extract_system(want_steps=want_steps),
            user_payload=payload,
            max_tokens=900 if want_steps else 300,
            # Warmer only on the turn that WRITES the set — same reason the tip capture
            # does it: at 0.2 every bakery got the same four questions.
            temperature=0.5 if want_steps else 0.2,
        )
        if not isinstance(data, dict):
            return {}
        out: dict[str, Any] = {}
        for k in _VALUE_FIELDS:
            v = data.get(k)
            if isinstance(v, str) and v.strip() and v.strip().lower() != "null":
                out[k] = v.strip()
        if want_steps and isinstance(data.get("steps"), list):
            out["steps_raw"] = data["steps"]
        if isinstance(data.get("answers"), dict):
            from app.community_question_sets import COMMUNITY_SUBJECT_FIELD

            out["answers"] = {
                str(k): v.strip()
                for k, v in data["answers"].items()
                # The place is only ever set by the picker: a model-written "subject"
                # would be a typed place name, which cannot be grounded and would strand
                # the community invisible (see add_circle).
                if isinstance(v, str)
                and v.strip()
                and v.strip().lower() != "null"
                and str(k) != COMMUNITY_SUBJECT_FIELD
            }
        return out
    except Exception:  # noqa: BLE001 - extraction is best-effort
        logger.exception("community_capture_extract_failed")
        return {}


def _has(draft: dict[str, Any], key: str) -> bool:
    return bool(str(draft.get(key) or "").strip())


# The circle-type chips, in the order a neighbour recognises them. Labels only — the
# stored value is the taxonomy key, matched back by `match_type_label`.
_TYPE_LABELS: dict[str, str] = {
    "friends": "A hangout spot",
    "fitness": "A gym or studio",
    "neighborhood": "A local spot",
    "kids_activity": "Something for kids",
    "faith": "A place of worship",
    "school": "A school",
    "hobby": "A club or group",
    "support": "A support group",
    "heritage": "A cultural community",
    "other": "Something else",
}
TYPE_SUGGESTIONS = list(_TYPE_LABELS.values())


def match_type_label(message: str) -> str | None:
    """The circle type behind a tapped chip label (or a typed near-miss). None if no
    match — an unmatched answer belongs to the extractor, not to a keyword guess."""
    text = " ".join(str(message or "").strip().lower().split())
    if not text:
        return None
    for key, label in _TYPE_LABELS.items():
        if text == label.lower():
            return key
    return None


def _summary(draft: dict[str, Any]) -> str:
    bits = [str(draft.get("name") or "your community").strip()]
    if _has(draft, "blurb"):
        bits.append(str(draft["blurb"]))
    return " · ".join(bits)


def _build_chips(draft: dict[str, Any]) -> list[dict[str, str]]:
    """The 'Heard you' chips — ★ Community + type + blurb. Tap a chip to correct that
    field (FE sends `fix:<field>`), exactly as the tip capture's chips work."""
    chips: list[dict[str, str]] = [
        {"label": "★ Community", "tone": "amber", "field": "circle_type"}
    ]
    ctype = str(draft.get("circle_type") or "")
    if ctype in _TYPE_LABELS:
        chips.append({"label": _TYPE_LABELS[ctype], "tone": "sky", "field": "circle_type"})
    if _has(draft, "blurb"):
        chips.append({"label": str(draft["blurb"])[:40], "tone": "coral", "field": "blurb"})
    return chips


def _place_suggestions(draft: dict[str, Any], *, zip_code: str | None, block_id: str | None) -> list[str]:
    """Real nearby places of this community's kind, for the chat fork's subject step.

    The carousel fork has the Places picker; the chat fork has only chips, so without
    these a "which gym?" asked in chat comes back as typed prose that cannot be grounded.
    """
    ctype = str(draft.get("circle_type") or "")
    if not ctype:
        return []
    try:
        from app.circles_flow import _TYPE_SEARCH
        from app.places import nearby_place_suggestions

        _, keyword = _TYPE_SEARCH.get(ctype, (None, ctype.replace("_", " ")))
        return nearby_place_suggestions(
            query=str(draft.get("name") or keyword), zip_code=zip_code, block_id=block_id
        )
    except Exception:  # noqa: BLE001
        return []


def _community_fields(draft: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The answered steps, self-describing — the same shape the recommendation card
    uses. An array and not {field: answer} because the questions are generated per
    community: store the answer alone and nothing can say later that "draws" was asked
    as "What gathers people here?"."""
    from app.community_question_sets import COMMUNITY_SUBJECT_FIELD
    from app.reco_question_sets import carousel

    out = [
        {
            "field": s["field"],
            "label": s["label"],
            "question": s["question"],
            "kind": s.get("kind") or "text",
            "answer": s["answer"],
        }
        for s in carousel(step_set_of(draft), draft.get("answers"))
        # SUBJECT out: it is the place, already the card's title.
        if s.get("answer") and s["field"] != COMMUNITY_SUBJECT_FIELD
    ]
    return out or None


def set_community_place(
    session_ctx: dict[str, Any], *, google_place_id: str
) -> dict[str, Any] | None:
    """Pin the community's place from a tapped Places result. Returns the place fields.

    The client sends ONLY the id it tapped: name / address / geo all come from Google
    here, exactly as circle grounding does it, so a caller can never mint or rename a
    place. Nothing is written to `places` yet — the canonical row is created at publish,
    so an abandoned draft leaves no shared state behind.
    """
    from app.places import place_details

    pid = str(google_place_id or "").strip()
    if not pid:
        return None
    details = place_details(pid)
    if not details or not str(details.get("name") or "").strip():
        return None
    draft = dict(session_ctx.get("community_draft") or {})
    from app.community_question_sets import COMMUNITY_SUBJECT_FIELD

    name = str(details["name"]).strip()
    draft["google_place_id"] = pid
    draft["place_address"] = details.get("address")
    draft["name"] = name
    # The subject step is answered BY the pin — that is what makes it an answer the
    # carousel can show ticked and `missing_required` can clear.
    draft["answers"] = {**(draft.get("answers") or {}), COMMUNITY_SUBJECT_FIELD: name}
    session_ctx["community_draft"] = draft
    return details


def publish_community(
    *, draft: dict[str, Any], user_id: str
) -> tuple[dict[str, Any] | None, str]:
    """Create the community for real: canonical place + the creator's grounded
    affiliation + one `place_features` row per answered step.

    (result, error_detail) — the reason comes back so the caller can recover from the one
    failure that is fixable in-turn (place_required) instead of just apologising.
    """
    gpid = str(draft.get("google_place_id") or "").strip()
    if not gpid:
        return None, "place_required"
    ctype = str(draft.get("circle_type") or "").strip()
    if not ctype:
        return None, "type_required"
    try:
        from app.circles_flow import add_circle

        result = add_circle(
            user_id,
            circle_type=ctype,
            detail=str(draft.get("name") or "").strip() or None,
            google_place_id=gpid,
            source="profile_add",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("community_publish_failed")
        return None, str(getattr(exc, "detail", "") or exc).lower()

    place_id = str(result.get("place_id") or "").strip()
    # Every answer becomes a place feature, so the community profile head reads back what
    # the creator said instead of an empty page. Best-effort per row: a feature that
    # fails to write must not lose the community that was just created.
    if place_id:
        from app.circles_capture import upsert_place_feature

        for row in _community_fields(draft) or []:
            try:
                upsert_place_feature(
                    place_id=place_id,
                    key=str(row["field"]),
                    value=str(row["answer"])[:200],
                    label=str(row.get("label") or "")[:40],
                    # The creator answering Lana's own question is a first-hand statement,
                    # not an inference — but it is not the owner's claim either, so it
                    # stays overwritable by a later `source='owner'` write.
                    confidence=0.9,
                    source="community_create",
                    contributed_by=user_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("community_feature_write_failed key=%s", row.get("field"))
        if str(draft.get("blurb") or "").strip():
            try:
                upsert_place_feature(
                    place_id=place_id,
                    key="blurb",
                    value=str(draft["blurb"])[:200],
                    label="Why people go",
                    confidence=0.9,
                    source="community_create",
                    contributed_by=user_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("community_blurb_write_failed")
    return {**result, "place_id": place_id}, ""


def reset_community_state(session_ctx: dict[str, Any]) -> None:
    """Drop the capture + its half-built draft so the turn falls through to normal
    routing. Keys set to None (not popped) so the {**old, **new} session merge clears
    them — a popped key is re-inherited from the stored context next turn."""
    for k in (
        "community_create_active",
        "community_draft",
        "community_ready",
        "community_pending_ask",
        "community_pending_question",
        "community_asked_fields",
    ):
        session_ctx[k] = None
    session_ctx["community_turns"] = 0


def community_capture_should_release(
    message: str, session_ctx: dict[str, Any], slots: "dict[str, Any] | None" = None
) -> bool:
    """Release the sticky capture on a semantic abandon or a confident pivot to another
    intent (the AI's read, not keywords), so the user is never trapped — the inversion
    every other lane already uses."""
    from app.lane_decision import lane_should_continue

    return not lane_should_continue(
        message, session_ctx, slots, is_valid_answer=_is_community_answer
    )


# What this capture OWNS. Anything the AI confidently reads as a different lane is a pivot
# and releases. Self-maintaining via is_confident_off_lane (no foreign-list to maintain).
_NATIVE_GOALS = frozenset({"create_community"})
_NATIVE_SIGNALS: frozenset[str] = frozenset()
_NATIVE_LINEARS = frozenset({"community.create"})


def _is_community_answer(
    message: str, session_ctx: dict[str, Any], slots: dict[str, Any] | None
) -> bool:
    """Is this turn a genuine answer/refine for the capture's current step?"""
    from app.lane_decision import is_confident_off_lane, is_meta_or_chat

    # Checked before the classifier's read: this capture asks tailored questions whose
    # answers are bare fragments ("Saturday mornings", "beginners"), and read alone those
    # look like a fresh search — which is how a sticky lane drops a half-built draft.
    if match_type_label(message):
        return True
    offered = session_ctx.get("community_offered") or []
    norm = " ".join(str(message or "").strip().lower().split())
    if norm and norm in {" ".join(str(o).strip().lower().split()) for o in offered}:
        return True
    if is_meta_or_chat(slots):
        return False
    return not is_confident_off_lane(
        slots,
        native_goals=_NATIVE_GOALS,
        native_signals=_NATIVE_SIGNALS,
        native_linears=_NATIVE_LINEARS,
    )


def run_community_capture_turn(
    *,
    user_message: str,
    session_ctx: dict[str, Any],
    history: list[dict[str, Any]],
    user_jwt: str,
    user_id: str | None,
    home_block_id: str | None,
) -> str:
    """Drive one create-a-community turn. Mutates session_ctx (community_draft,
    community_create_active, community_published_now, routing_phase). Returns the reply."""
    from app.community_question_sets import (
        COMMUNITY_SUBJECT_FIELD,
        normalize_community_type,
        validate_community_steps,
    )
    from app.discovery_route import resolve_block_id
    from app.reco_question_sets import carousel, missing_required, next_question

    msg = str(user_message or "").strip()
    draft: dict[str, Any] = dict(session_ctx.get("community_draft") or {})
    # Re-stamped below by whichever branch asks something. The chat fork sends one
    # question as prose, so without this the FE cannot tell WHICH step is open and renders
    # a text box for a `place` step instead of the Places picker.
    draft.pop("pending_field", None)
    # One id per community draft, for the whole draft's life. The FE keys its
    # cards-or-chat pick on this — never on the name, which arrives on the subject step
    # (the bug that made the tip fork leak between recommendations, dev QA 2026-09-04).
    if not draft.get("draft_id"):
        draft["draft_id"] = uuid.uuid4().hex[:12]
    zip_code = str(session_ctx.get("zip_code") or session_ctx.get("zip") or "").strip() or None
    block_id = resolve_block_id(session_ctx, home_block_id)
    session_ctx["community_published_now"] = False

    # ── Loop safety ──
    turns = int(session_ctx.get("community_turns") or 0) + 1
    session_ctx["community_turns"] = turns
    if _CANCEL_RE.search(msg) or turns > _COMMUNITY_TURN_CAP:
        reset_community_state(session_ctx)
        session_ctx["routing_phase"] = "listening"
        return compose_reply(
            goal="The user dropped the community they were setting up. Let it go warmly, in one line, and leave the door open.",
            facts=["The community was not created", "Nothing was shared with anyone"],
            fallback="No problem — I've let that go. Tell me when you want to start one.",
        )

    # ── Publish: the ready card's CTA ──
    if session_ctx.get("community_ready") and _PUBLISH_RE.search(msg):
        result, err = publish_community(draft=draft, user_id=str(user_id or ""))
        if not result:
            if err == "place_required":
                # Recoverable in-turn: re-open the subject step instead of apologising.
                session_ctx["community_pending_ask"] = COMMUNITY_SUBJECT_FIELD
                draft["pending_field"] = COMMUNITY_SUBJECT_FIELD
                draft["suggestions"] = _place_suggestions(
                    draft, zip_code=zip_code, block_id=block_id
                )
                session_ctx["community_draft"] = draft
                session_ctx["community_ready"] = None
                session_ctx["community_create_active"] = True
                session_ctx["routing_phase"] = "listening"
                return "Almost — I still need the spot on the map. Which one is it?"
            session_ctx["community_draft"] = draft
            session_ctx["community_create_active"] = True
            return compose_reply(
                goal="Creating the community failed on Lana's side. Apologise in one line and say you'll keep the draft so nothing is lost.",
                facts=["The community was not created", "The draft is kept"],
                fallback="I couldn't get that up just now — I've kept everything you told me. Want to try again?",
            )
        draft["published"] = True
        draft["community_id"] = result.get("place_id") or result.get("affiliation_id")
        draft["ready"] = True
        session_ctx["community_draft"] = draft
        session_ctx["community_published_now"] = True
        session_ctx["community_ready"] = None
        session_ctx["community_create_active"] = None
        session_ctx["community_turns"] = 0
        session_ctx["routing_phase"] = "listening"
        name = str(draft.get("name") or "your community")
        return compose_reply(
            goal=(
                "The community is live. Celebrate briefly and warmly, say neighbours can "
                "now find and join it, and that you'll point people to it when they ask."
            ),
            facts=[f"{name} is now a community neighbours can find and join"],
            fallback=f"🎉 **{name}** is up — neighbours can find it and ask to join. I'll point people to it.",
        )

    # ── Correction: chip tap "fix:<field>" → clear + re-ask that field ──
    fix = re.match(r"\s*fix:(\w+)\s*$", msg)
    if fix:
        field = fix.group(1)
        if field == "name":
            # The name IS the subject step (a pinned place), same as the tip capture.
            field = COMMUNITY_SUBJECT_FIELD
            draft.pop("name", None)
            draft.pop("google_place_id", None)
        step = None
        if field == "circle_type":
            draft.pop("circle_type", None)
            # The set was written FOR the old type — a new type needs new questions.
            draft.pop("step_set", None)
            draft.pop("steps", None)
        elif field == "blurb":
            draft.pop("blurb", None)
        else:
            step = next((s for s in step_set_of(draft) if s["field"] == field), None)
            if step:
                draft["answers"] = {
                    k: v for k, v in (draft.get("answers") or {}).items() if k != field
                }
                session_ctx["community_asked_fields"] = [
                    f for f in (session_ctx.get("community_asked_fields") or []) if f != field
                ]
                session_ctx["community_pending_ask"] = field
        session_ctx["community_ready"] = None
        if step:
            question = str(step["question"])
            options = list(step.get("options") or [])
            if step.get("kind") == "place":
                options = _place_suggestions(draft, zip_code=zip_code, block_id=block_id)
            draft["pending_field"] = field
        elif field == "circle_type":
            question, options = "What kind of place is it?", TYPE_SUGGESTIONS
            session_ctx["community_pending_ask"] = "circle_type"
        else:
            question, options = "What should I change?", []
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = options
        session_ctx["community_draft"] = draft
        session_ctx["community_offered"] = options
        session_ctx["community_create_active"] = True
        session_ctx["community_pending_question"] = question
        session_ctx["routing_phase"] = "listening"
        return f"Sure — {question}"

    # ── A tapped circle-type chip, before the extractor sees it ──
    tapped_type = match_type_label(msg)
    if tapped_type and not _has(draft, "circle_type"):
        draft["circle_type"] = tapped_type
        session_ctx["community_pending_ask"] = None

    # ── Capture a pending answer into the right place ──
    pending = str(session_ctx.get("community_pending_ask") or "")
    if pending and msg and not _PASS_RE.search(msg) and not tapped_type:
        if pending == COMMUNITY_SUBJECT_FIELD:
            # A place is only ever set by the picker (/community-setup), never by text:
            # a typed name cannot be grounded. Left pending so the step is re-asked.
            pass
        elif pending == "circle_type":
            resolved = normalize_community_type(msg)
            if resolved:
                draft["circle_type"] = resolved
                session_ctx["community_pending_ask"] = None
        elif pending in {s["field"] for s in step_set_of(draft)}:
            draft["answers"] = {**(draft.get("answers") or {}), pending: msg}
            session_ctx["community_pending_ask"] = None

    # ── Extract fields (+ the set, once) ──
    if msg and not tapped_type:
        from app.i18n import lang_display_name, session_lang

        code = session_lang(session_ctx)
        found = _extract_fields(
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

    # ── P1: nothing yet ──
    if not _has(draft, "name") and not _has(draft, "circle_type") and not _has(draft, "blurb"):
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = []
        session_ctx["community_draft"] = draft
        session_ctx["community_create_active"] = True
        session_ctx["community_pending_question"] = "Which place should I add as a community?"
        session_ctx["routing_phase"] = "listening"
        return compose_reply(
            goal=(
                "Ask which place the user wants to turn into a community, and mention they "
                "can say what draws people to it. Warm, one or two short lines."
            ),
            facts=["Nothing captured yet", "A community is always a real place"],
            fallback="Love that — which place should I add as a community? Name the spot and what draws people to it.",
        )

    chips = _build_chips(draft)

    # ── P2: the type — it selects the question set, so nothing can be asked without it ──
    if not _has(draft, "circle_type"):
        draft["chips"] = chips
        draft["suggestions"] = TYPE_SUGGESTIONS
        session_ctx["community_draft"] = draft
        session_ctx["community_offered"] = TYPE_SUGGESTIONS
        session_ctx["community_create_active"] = True
        session_ctx["community_pending_ask"] = "circle_type"
        session_ctx["community_pending_question"] = "What kind of place is it?"
        session_ctx["routing_phase"] = "listening"
        return f"Heard you — **{_summary(draft)}**. What kind of place is it?"

    # ── The question set is written ONCE, here — after the type, because the type picks
    # the set and the questions are about THIS place ("which morning is busiest at
    # Rosetta's?"), not about bakeries in general. ──
    if not draft.get("step_set"):
        draft["step_set"] = validate_community_steps(
            draft.pop("steps_raw", None), draft["circle_type"]
        )
    step_set = step_set_of(draft)

    # ── P3: walk the set ──
    if step_set:
        steps = carousel(step_set, draft.get("answers"))
        draft["steps"] = steps
        draft["missing"] = missing_required(step_set, draft.get("answers"))
        asked = set(session_ctx.get("community_asked_fields") or [])
        # "that's it" goes STRAIGHT to the ready card once the required steps are in.
        done_early = bool(_PASS_RE.search(msg)) and not draft["missing"]
        step = None if done_early else next_question(
            step_set, draft.get("answers"), asked=asked
        )
        if step:
            asked.add(step["field"])
            session_ctx["community_asked_fields"] = list(asked)
            session_ctx["community_pending_ask"] = step["field"]
            draft["pending_field"] = step["field"]
            draft["chips"] = chips
            # A generated set writes no options for a map step, and the chat fork has no
            # Places picker to fall back on — so real nearby places arrive as suggestions.
            draft["suggestions"] = list(step.get("options") or []) or (
                _place_suggestions(draft, zip_code=zip_code, block_id=block_id)
                if step.get("kind") == "place"
                else []
            )
            session_ctx["community_draft"] = draft
            session_ctx["community_offered"] = draft["suggestions"]
            session_ctx["community_create_active"] = True
            session_ctx["community_pending_question"] = step["question"]
            session_ctx["routing_phase"] = "listening"
            answered = sum(1 for s in steps if s.get("answer"))
            return (
                f"Heard you — **{_summary(draft)}**. {step['question']} "
                f"({answered + 1}/{len(steps)})"
            )

    # ── P4: ready → the assembled card + the share CTA (nothing is created until they
    # confirm: a community is shared state other people join). ──
    draft["chips"] = chips
    draft["suggestions"] = []
    draft["ready"] = True
    session_ctx["community_draft"] = draft
    session_ctx["community_create_active"] = True
    session_ctx["community_ready"] = True
    session_ctx["community_pending_question"] = None  # nothing outstanding on the card
    session_ctx["routing_phase"] = "listening"
    name = str(draft.get("name") or "your community")
    return compose_reply(
        goal=(
            "The community draft is complete and shown as a card. Tell the user it's ready "
            "and prompt them to tap **Share with the community** (keep that button name "
            "verbatim, bolded) so neighbours can find and join it."
        ),
        facts=[f"Community ready: {name}"],
        fallback=(
            f"It's ready to share — **{name}**. One last look, then "
            "**Share with the community** and neighbours can find and join it."
        ),
    )
