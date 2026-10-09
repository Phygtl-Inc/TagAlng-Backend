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

from app.reply_compose import compose_reply, readback

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


_VALUE_FIELDS = ("name", "circle_type", "blurb", "parent")

_EXTRACT_SYSTEM = """You extract structured fields about a LOCAL COMMUNITY a neighbor \
wants to create, and write the question set for it.

A community is usually a real PLACE people gather at — a bakery, a gym, a church, a \
school, a park. It can also have NO place at all: a community around a topic, a creator or \
a following, which people join from a link rather than by going somewhere. The neighbor is \
starting it so others can find and join it.

Return ONE compact JSON object with exactly these keys:
{"name","circle_type","blurb","parent",<<STEPS_KEY>>"answers"}

- name: what is being made into a community, verbatim as they said it — usually a place \
("Rosetta's Bakery", "CF Fitness", "Lake Nona Park"), or for a placeless one the community's \
own name ("Iron Man Training"). null if not stated.
- circle_type: EXACTLY one of <<TYPES>>, or null if genuinely unclear. <<TYPE_RULES>>
- blurb: why people gather there, in THEIR words, e.g. "best sourdough on the block, \
everyone ends up there Saturday mornings". null if not stated.
- parent: the BIGGER community this one sits INSIDE, verbatim as they said it, only when \
they say it is part of one — a club at a school, a chapter of a group, a team inside a \
gym, initials and nicknames kept as written ("a club inside UMD" → "UMD", "the youth \
choir, part of St. Brigid's" → "St. Brigid's", "the Orlando chapter of Iron Man Training" \
→ "Iron Man Training"). null when they \
only say where it meets or which area it is in ("in Lake Nona", "at the park"): a place \
or a neighbourhood is never a parent. null when not stated.
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
  of place: "Which bakery is it?", "Which gym?", "Where do you all meet up?".
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
    "creator": "A creator community",
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
    if _has(draft, "parent"):
        # Tapping it (fix:parent) drops the parent — the community goes up on its own.
        shown = (draft.get("parent_place") or {}).get("place_name") or draft["parent"]
        chips.append({"label": f"Part of {shown}"[:40], "tone": "sky", "field": "parent"})
    return chips


def _place_suggestions(
    draft: dict[str, Any],
    *,
    zip_code: str | None,
    block_id: str | None,
    user_jwt: str | None = None,
) -> list[str]:
    """Real nearby places of this community's kind, for the chat fork's subject step.

    The carousel fork has the Places picker; the chat fork has only chips, so without
    these a "which gym?" asked in chat comes back as typed prose that cannot be grounded.

    Only where a tapped name can LAND (§38a). A place is set by `google_place_id` alone,
    and run_community_capture_turn drops a typed or tapped answer to the subject step by
    design — so for every pinned type these chips were taps that re-asked the question.
    They are no longer sent; the client's map picker is the answer control on that step.
    A group (hobby / support) is the exception: its subject step asks where it meets, and
    a tapped name is kept as `meets_at`, so the nearby names are real answers there.
    """
    from app.community_question_sets import GROUP_TYPES, normalize_community_type

    ctype = str(draft.get("circle_type") or "")
    if not ctype or normalize_community_type(ctype) not in GROUP_TYPES:
        return []
    try:
        from app.circles_flow import _TYPE_SEARCH
        from app.places import nearby_place_suggestions

        _, keyword = _TYPE_SEARCH.get(ctype, (None, ctype.replace("_", " ")))
        from app.auth import jwt_user_id

        # See `tip_share._name_suggestions`: no user id, no centre, no places for anyone
        # whose session has no ZIP in it.
        return nearby_place_suggestions(
            query=str(draft.get("name") or keyword),
            zip_code=zip_code,
            block_id=block_id,
            user_id=jwt_user_id(user_jwt) if user_jwt else None,
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


def _awaiting_text_subject(session_ctx: dict[str, Any], draft: dict[str, Any]) -> bool:
    """True while the pending question is a free-text subject — "What's the community
    called?" for a creator community, which has no place to pin."""
    from app.community_question_sets import COMMUNITY_SUBJECT_FIELD

    if str(session_ctx.get("community_pending_ask") or "") != COMMUNITY_SUBJECT_FIELD:
        return False
    step = next(
        (st for st in step_set_of(draft) if st.get("field") == COMMUNITY_SUBJECT_FIELD), None
    )
    return (step or {}).get("kind") == "text"


def _is_bare_control(message: str, pattern: "re.Pattern[str]") -> bool:
    """The message IS the control phrase, rather than merely containing it.

    Both control regexes match ordinary words — stop, pass, skip, done, all good — so at a
    step whose answer is a NAME they eat real ones: "Stop the Stigma" destroyed the whole
    draft, "Pass the Mic" was silently dropped. Neither is a command; both are plausible
    community names. But exempting the step outright would trap someone who genuinely wants
    out while being asked the name, so the test is whether the control phrase is essentially
    the entire message.
    """
    text = re.sub(r"[\s.!?,]+", " ", str(message or "").strip().lower()).strip()
    if not text:
        return False
    m = pattern.search(text)
    return bool(m) and len(m.group(0)) >= len(text) - 1


def _resolve_parent(draft: dict[str, Any], user_id: Any) -> None:
    """Which community "inside SJSU" means — once per draft, before publish.

    Sets draft["parent_place"] = {place_id, place_name, located} or
    draft["parent_unresolved"] = True. The AI alias matcher may map a short form; the
    reply names the community it attached to, and the chip / detach undo a wrong one."""
    said = str(draft.get("parent") or "").strip()
    if not said or not user_id or draft.get("parent_place") or draft.get("parent_unresolved"):
        return
    from app.community_discovery import find_named_community

    try:
        hit = find_named_community(str(user_id), said)
    except Exception:  # noqa: BLE001 — a parent is optional; never block the publish
        logger.exception("community_parent_resolve_failed said=%r", said[:60])
        hit = None
    if not hit or not hit.get("place_id"):
        draft["parent_unresolved"] = True
        return
    located = False
    try:
        from app.auth import service_client

        row = (
            service_client()
            .table("places")
            .select("lat, lng, handle")
            .eq("id", str(hit["place_id"]))
            .limit(1)
            .execute()
        )
        got = (row.data or [{}])[0] if isinstance(row.data, list) else {}
        located = got.get("lat") is not None and got.get("lng") is not None
        parent_handle = str(got.get("handle") or "").strip() or None
    except Exception:  # noqa: BLE001
        logger.exception("community_parent_point_read_failed")
        parent_handle = None
    draft["parent_place"] = {
        "place_id": str(hit["place_id"]),
        "place_name": str(hit.get("place_name") or said).strip(),
        "located": located,
        "handle": parent_handle,
    }
    draft["chips"] = _build_chips(draft)


# ── Asking for a parent the creator did not name ────────────────────────────────────────
# Creators rarely say "inside SJSU", so a club made by an SJSU member went up standalone
# (that is how RCC did). Lana asks ONCE per draft — only when one of the creator's own
# communities could really hold it, which is the AI's call, never a keyword list.

_PARENT_ASK = "parent"
_PARENT_MAX = 3

_UMBRELLA_SYSTEM = """You decide whether a community a person is creating could sit INSIDE \
one of the bigger communities they already belong to, as one of its clubs, teams, chapters \
or groups — so that people looking at the bigger community see it there.

A candidate is a plausible home ONLY when one of these holds:
1. INSTITUTION: the candidate is an institution or organisation with its own members that \
commonly has groups forming under it — a school, university or campus, a company or \
workplace, a congregation, an organisation with local branches — and the new community is \
the kind of group that forms among THAT organisation's own people (its students, staff, \
members). A group defined by a neighbourhood or street, or by a different institution, is \
not inside it.
2. TOPIC WITH LOCAL BRANCHES: the candidate is a community about a topic or interest whose \
members are not tied to one spot (spread_out is true), and the new community is a local \
branch of THAT SAME topic — the same interest, gathering in one city, area or venue. A \
new community about a different subject is not inside it.
A single venue people simply go to (a cafe, a shop, a gym, a park) and a neighbourhood or \
area are never homes for another group, and neither is an ordinary local club.

When in doubt, leave it out: a wrong question costs the person a tap, a missing one costs \
nothing they cannot fix later.

Return ONE JSON object: {"umbrellas": [numbers]} — the numbers of the plausible \
candidates, most plausible first, at most 3. {"umbrellas": []} when none is plausible."""

_PARENT_ANSWER_SYSTEM = """Lana asked a person who is creating a community whether it \
belongs inside one of the bigger communities they are in (the numbered OPTIONS), or stands \
on its own. Read their reply and return ONE JSON object:
{"choice": "candidate" | "standalone" | "other" | "unclear" | "none", "n": number or null, \
"name": string or null}

- candidate: they pick one of the OPTIONS. n is its number. A plain agreement when there \
is exactly one option is that option.
- standalone: it is not part of any of them / it stands on its own / a plain refusal.
- other: it IS part of a bigger community, but one that is not among the OPTIONS. name is \
that community's name exactly as they wrote it.
- unclear: they agree it is part of one but there are several OPTIONS and they did not say \
which.
- none: the reply does not answer the question at all — they moved on, pressed a different \
button, changed something else about the community, or asked something.
The reply may be in any language."""


def _parent_ask_due(draft: dict[str, Any]) -> bool:
    """Whether the "is it part of something bigger?" question may still be asked.

    Never when they named a parent themselves (resolved or not), never twice in one draft
    (asked, declined, or the chip removed), and never for a creator community, which the
    SQL refuses to make a chapter anyway (creator_community_cannot_be_chapter)."""
    from app.circles_flow import CREATOR_PLACE_PREFIX
    from app.community_question_sets import normalize_community_type

    if draft.get("parent_asked") or draft.get("parent_declined"):
        return False
    if _has(draft, "parent") or draft.get("parent_place") or draft.get("parent_unresolved"):
        return False
    if normalize_community_type(draft.get("circle_type")) == "creator":
        return False
    return not str(draft.get("google_place_id") or "").startswith(CREATOR_PLACE_PREFIX)


def _parent_candidates(draft: dict[str, Any], user_id: Any) -> list[dict[str, Any]]:
    """The creator's own communities that attach_chapter would accept as this one's parent.

    Confirmed membership (what the SQL checks), not itself a chapter (depth is one level),
    and not the very place being published. A creator community IS eligible: attach_chapter
    only refuses one as the CHAPTER (creator_community_cannot_be_chapter) — a topic like
    Podcasters can hold local branches. Best effort: a failed read means no question,
    never a failed turn."""
    if not user_id:
        return []
    from app.circles_flow import list_my_circles

    try:
        rows = list_my_circles(str(user_id))
    except Exception:  # noqa: BLE001
        logger.exception("community_parent_candidates_failed")
        return []
    own_gpid = str(draft.get("google_place_id") or "").strip()
    out: list[dict[str, Any]] = []
    for r in rows or []:
        if str(r.get("status") or "") != "confirmed" or not r.get("place_id"):
            continue
        if not str(r.get("place_name") or "").strip():
            continue
        gpid = str(r.get("google_place_id") or "")
        if r.get("parent_place_id"):
            continue
        if own_gpid and gpid == own_gpid:
            continue
        out.append(
            {
                "place_id": str(r["place_id"]),
                "place_name": str(r["place_name"]).strip(),
                "circle_type": r.get("circle_type"),
                "relation": r.get("relation"),
                "detail": r.get("detail"),
                "located": r.get("lat") is not None and r.get("lng") is not None,
            }
        )
    if not out:
        return out
    # Each candidate's own description, so the judge reads what it IS, not just its name,
    # and its link.
    try:
        from app.auth import service_client

        res = (
            service_client()
            .table("places")
            .select("id, blurb, handle")
            .in_("id", [c["place_id"] for c in out])
            .execute()
        )
        rows_by_id = {
            str(p.get("id")): p for p in (res.data or []) if isinstance(p, dict)
        }
        for c in out:
            got = rows_by_id.get(c["place_id"]) or {}
            c["blurb"] = str(got.get("blurb") or "").strip() or None
            # A linked parent gives its chapters get.lana.help/{parent}/{chapter}, so the
            # link question is skipped for one picked here, as for one they named.
            c["handle"] = str(got.get("handle") or "").strip() or None
    except Exception:  # noqa: BLE001
        logger.exception("community_parent_blurb_read_failed")
    return out


def _judge_umbrellas(
    draft: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The candidates the AI reads as a plausible home for this community, best first.

    [] on any failure or an unconfigured model: no judgement, no question."""
    if not candidates:
        return []
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return []
        answers = {
            k: v for k, v in (draft.get("answers") or {}).items() if isinstance(v, str) and v
        }
        new = {
            "name": draft.get("name"),
            "type": draft.get("circle_type"),
            "description": draft.get("blurb"),
            "where_it_meets": draft.get("meets_at"),
            "address": draft.get("place_address"),
            "answers": answers or None,
        }
        listed = [
            {
                "n": i + 1,
                "name": c["place_name"],
                "type": c.get("circle_type"),
                "kind": c.get("relation"),
                "description": c.get("blurb"),
                "their_note": c.get("detail"),
                # A creator / placeless community: a topic whose members are anywhere.
                "spread_out": not c.get("located"),
            }
            for i, c in enumerate(candidates)
        ]
        data = llm_json(
            model=synthesizer_model(),
            system=_UMBRELLA_SYSTEM,
            user_payload=(
                "NEW COMMUNITY:\n"
                + json.dumps({k: v for k, v in new.items() if v}, ensure_ascii=False)
                + "\n\nCOMMUNITIES THEY BELONG TO:\n"
                + json.dumps(listed, ensure_ascii=False)
            ),
            max_tokens=120,
            temperature=0.0,
        )
        picked = data.get("umbrellas") if isinstance(data, dict) else None
        out: list[dict[str, Any]] = []
        for n in picked if isinstance(picked, list) else []:
            try:
                i = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(candidates) and candidates[i] not in out:
                out.append(candidates[i])
        return out[:_PARENT_MAX]
    except Exception:  # noqa: BLE001 — a parent is optional; never cost the turn
        logger.exception("community_umbrella_judge_failed")
        return []


def _interpret_parent_answer(message: str, offer: list[dict[str, Any]]) -> dict[str, Any]:
    """The AI's read of a typed answer to the parent question. {"choice": "none"} on failure."""
    try:
        from app.orchestrator.llm import llm_configured, llm_json, router_model

        if not llm_configured():
            return {"choice": "none"}
        options = "\n".join(f"{i + 1}. {o['place_name']}" for i, o in enumerate(offer))
        data = llm_json(
            model=router_model(),
            system=_PARENT_ANSWER_SYSTEM,
            user_payload=f"OPTIONS:\n{options}\n\nTHEIR REPLY:\n{message.strip()[:300]}",
            max_tokens=80,
            temperature=0.0,
        )
        return data if isinstance(data, dict) else {"choice": "none"}
    except Exception:  # noqa: BLE001
        logger.exception("community_parent_answer_read_failed")
        return {"choice": "none"}


def _norm_label(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _apply_parent_answer(
    draft: dict[str, Any], message: str, user_id: Any
) -> str:
    """Apply their answer to the parent question to the draft. Returns what it was:
    chosen | other | standalone | unclear | none.

    A tapped chip is a rendered control and is matched exactly; anything typed is the AI's
    read. A pick sets the same state `_resolve_parent` sets, so the chip, the attach and the
    inherited location all run unchanged; a different name goes through `_resolve_parent`."""
    offer = [o for o in (draft.get("parent_offer") or []) if isinstance(o, dict)]
    norm = _norm_label(message)

    def choose(o: dict[str, Any]) -> str:
        draft["parent"] = o["place_name"]
        draft["parent_place"] = {
            "place_id": o["place_id"],
            "place_name": o["place_name"],
            "located": bool(o.get("located")),
            "handle": o.get("handle"),
            "join_first": bool(o.get("join_first")),
        }
        draft.pop("parent_unresolved", None)
        draft["chips"] = _build_chips(draft)  # the card shows the "Part of" chip
        return "chosen"

    tapped = next((o for o in offer if _norm_label(o.get("label")) == norm), None)
    if tapped:
        return choose(tapped)
    if norm and norm == _norm_label(draft.get("parent_standalone_label")):
        draft["parent_declined"] = True
        return "standalone"

    read = _interpret_parent_answer(message, offer)
    choice = str(read.get("choice") or "none")
    if choice == "candidate":
        try:
            i = int(read.get("n")) - 1
        except (TypeError, ValueError):
            i = -1
        if 0 <= i < len(offer):
            return choose(offer[i])
        if len(offer) == 1:
            return choose(offer[0])
        return "unclear"
    if choice == "other" and str(read.get("name") or "").strip():
        draft["parent"] = str(read["name"]).strip()[:80]
        draft.pop("parent_place", None)
        draft.pop("parent_unresolved", None)
        _resolve_parent(draft, user_id)
        return "other"
    if choice == "standalone":
        draft["parent_declined"] = True
        return "standalone"
    if choice == "unclear":
        return "unclear"
    # Not an answer: the question was asked once, so it is not asked again.
    draft["parent_declined"] = True
    return "none"


def _ask_parent(
    session_ctx: dict[str, Any], draft: dict[str, Any], *, again: bool = False
) -> str:
    """Put the parent question on the ready card: one chip per candidate + standalone."""
    from app.i18n import localize_labels, session_lang

    offer = [o for o in (draft.get("parent_offer") or []) if isinstance(o, dict)]
    names = [o["place_name"] for o in offer]
    lang = session_lang(session_ctx)
    raw = [f"Part of {n}"[:40] for n in names] + ["On its own"]
    labels = localize_labels(raw, lang) if lang else raw
    for o, label in zip(offer, labels):
        o["label"] = label
    draft["parent_offer"] = offer
    draft["parent_standalone_label"] = labels[-1]
    # The first of the closing questions (_after_questions): asked with the card not yet
    # ready, the same state the city and link steps are asked in; the PWA renders
    # `suggestions` as quick replies under the message.
    draft["chips"] = _build_chips(draft)
    draft["pending_field"] = _PARENT_ASK
    draft["suggestions"] = labels
    session_ctx["community_ready"] = None
    session_ctx["community_pending_ask"] = _PARENT_ASK
    session_ctx["community_offered"] = labels
    session_ctx["community_pending_question"] = (
        "Whether this new community is part of one of these bigger communities "
        f"({', '.join(names)}), or stands on its own"
    )
    session_ctx["community_draft"] = draft
    session_ctx["community_create_active"] = True
    session_ctx["routing_phase"] = "listening"
    name = str(draft.get("name") or "their community")
    joined = " or ".join(f"**{n}**" for n in names)
    return compose_reply(
        goal=(
            "Ask in one short line whether their new community is part of one of the bigger "
            "communities listed in the facts, or stands on its own. Name them. "
            + (
                "You already asked once and they did not say which one, so ask them to pick "
                "one. "
                if again
                else ""
            )
            + "They answer with the buttons below or in their own words."
        ),
        facts=[
            f"The new community: {name}",
            "Bigger communities it could sit inside: " + ", ".join(names),
        ]
        + [
            f"They are not a member of {o['place_name']} yet; choosing it also makes them one"
            for o in offer
            if o.get("join_first")
        ]
        + [
            "Inside one, people looking at that community see it as one of its clubs; it "
            "can still be found and joined on its own",
        ],
        fallback=f"Is **{name}** part of {joined}, or does it stand on its own?",
    )


def _maybe_ask_parent(
    session_ctx: dict[str, Any], draft: dict[str, Any], user_id: Any
) -> str | None:
    """Ask the parent question if it is due and someone could hold this community; the
    reply, or None to carry on. Marks the draft asked either way, so the membership read
    and the judgement run once per draft."""
    if not _parent_ask_due(draft):
        return None
    draft["parent_asked"] = True
    candidates = _parent_candidates(draft, user_id)
    # The community picked at the top of the app is where they are standing: it is offered
    # first, without the umbrella judgement — choosing it is their own signal. Still a
    # question, never an assumption: browsing inside Podcasters does not make every new
    # community a branch of it.
    scoped = _scoped_parent(session_ctx, draft, candidates)
    others = [c for c in candidates if not scoped or c["place_id"] != scoped["place_id"]]
    plausible = ([scoped] if scoped else []) + _judge_umbrellas(draft, others)
    if not plausible:
        return None
    draft["parent_offer"] = [
        {
            "place_id": c["place_id"],
            "place_name": c["place_name"],
            "located": bool(c.get("located")),
            "handle": c.get("handle"),
            # Not a member yet: picking it joins them first (attach_chapter needs that).
            "join_first": bool(c.get("join_first")),
        }
        for c in plausible[:_PARENT_MAX]
    ]
    return _ask_parent(session_ctx, draft)


def _scoped_parent(
    session_ctx: dict[str, Any], draft: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The community selected at the top of the app, as a parent candidate, or None.

    Theirs already → that candidate row. Not theirs yet → read the place and mark it
    join_first. None when it could not hold a chapter: itself a chapter (one level only),
    paused, or the very place being created."""
    from app.community_scope import active_community

    comm = active_community(session_ctx)
    if not comm:
        return None
    pid = str(comm["place_id"])
    mine = next((c for c in candidates if c["place_id"] == pid), None)
    if mine:
        return mine
    try:
        from app.auth import service_client

        res = (
            service_client()
            .table("places")
            .select("id, name, lat, lng, handle, parent_place_ref, governance_state, google_place_id")
            .eq("id", pid)
            .limit(1)
            .execute()
        )
        row = (res.data or [{}])[0] if isinstance(res.data, list) and res.data else {}
    except Exception:  # noqa: BLE001 — no read, no offer; the judged ones still go
        logger.exception("community_scoped_parent_read_failed place=%s", pid)
        return None
    if not row or row.get("parent_place_ref") or row.get("governance_state") == "suspended":
        return None
    own_gpid = str(draft.get("google_place_id") or "").strip()
    if own_gpid and str(row.get("google_place_id") or "") == own_gpid:
        return None
    name = str(row.get("name") or comm.get("name") or "").strip()
    if not name:
        return None
    return {
        "place_id": pid,
        "place_name": name,
        "located": row.get("lat") is not None and row.get("lng") is not None,
        "handle": str(row.get("handle") or "").strip() or None,
        "join_first": True,
    }


def _attach_to_parent(draft: dict[str, Any], user_id: Any, place_id: str) -> list[str]:
    """Attach the just-published community to the parent they named; reply facts.

    The community is live either way — a refused attach says why, in words the reply can
    use, and never undoes the publish."""
    said = str(draft.get("parent") or "").strip()
    if not said:
        return []
    if draft.get("parent_unresolved") or not draft.get("parent_place"):
        return [
            f'They wanted it inside "{said}", but there is no community by that name on Lana '
            "yet, so it stands on its own for now — say so in a few words"
        ]
    parent = draft["parent_place"]
    pname = str(parent.get("place_name") or said)
    from app.community_chapter_ops import attach_chapter

    got = attach_chapter(str(user_id or ""), place_id, str(parent["place_id"]))
    joined: list[str] = []
    if got.get("reason") == "not_a_member_of_parent":
        # They put it inside X themselves (named it, or tapped "Part of X"), and the SQL
        # needs them to belong to X — so they join X, the same self-claim as its Join
        # button, and the attach runs once more. Said in the reply, never silent.
        from app.community_discovery import join_community

        try:
            join_community(str(user_id or ""), str(parent["place_id"]))
        except ValueError:
            logger.exception("community_parent_join_failed parent=%s", parent.get("place_id"))
        else:
            joined = [f"They were not in {pname}, so they are now a member of it too"]
            got = attach_chapter(str(user_id or ""), place_id, str(parent["place_id"]))
    draft["parent_attached"] = bool(got.get("ok"))
    if got.get("ok"):
        return joined + [
            f"It is now a club inside {pname} — people who look at {pname} will see it"
        ]
    reason = got.get("reason")
    if reason == "not_a_member_of_parent":
        return [
            f"It could NOT be put inside {pname} because they are not a member of {pname}. "
            f"It is live on its own; they can join {pname} and then ask you to add it"
        ]
    if reason in ("chapter_depth_exceeded",):
        return [f"{pname} is itself a club inside another community, so this one stands on its own"]
    return [f"It could not be put inside {pname} just now, so it stands on its own"]


def publish_community(
    *, draft: dict[str, Any], user_id: str
) -> tuple[dict[str, Any] | None, str]:
    """Create the community for real: canonical place + the creator's grounded
    affiliation + one `place_features` row per answered step.

    (result, error_detail) — the reason comes back so the caller can recover from the one
    failure that is fixable in-turn (place_required) instead of just apologising.
    """
    gpid = str(draft.get("google_place_id") or "").strip()
    ctype = str(draft.get("circle_type") or "").strip()
    if not ctype:
        return None, "type_required"
    if not gpid:
        # No place was picked — either a creator community (nothing to pick) or the
        # neighbour skipped an optional place step. Identity comes from the NAME instead:
        # the same key every time, so re-publishing finds the row rather than splitting the
        # roster across two places. Everything downstream is unchanged — add_circle grounds
        # it through the normal path (circles_flow.ground_affiliation), which reads the
        # prefix and skips the Google lookup.
        from app.circles_capture import _slugify
        from app.circles_flow import CREATOR_PLACE_PREFIX
        from app.community_question_sets import COMMUNITY_SUBJECT_FIELD

        # The SUBJECT ANSWER wins here, and draft["name"] is only the fallback — the
        # opposite of the grounded lane. draft["name"] is whatever the extractor lifted
        # verbatim from the opening message, so "I want a community for people who follow
        # my Jack Russell account" names the community "people who follow my Jack Russell
        # account". The subject step then asks what it is actually called, and that answer
        # is the one the creator chose.
        answers = draft.get("answers") or {}
        name = str(answers.get(COMMUNITY_SUBJECT_FIELD) or "").strip() or str(
            draft.get("name") or ""
        ).strip()
        slug = _slugify(name)
        if not slug:
            return None, "name_required"
        # Mutate the CALLER's draft, not a local copy. `draft = {**draft, ...}` rebound the
        # name here and nowhere else, so the row got the chosen name while the community
        # filter label and the celebration line both went on printing the extractor's
        # opening phrase — the row and the copy disagreeing is worse than both being wrong,
        # because only one of them is what the creator actually reads.
        draft["name"] = name
        gpid = CREATOR_PLACE_PREFIX + slug
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
        if str(draft.get("meets_at") or "").strip():
            try:
                upsert_place_feature(
                    place_id=place_id,
                    key="meets_at",
                    value=str(draft["meets_at"])[:200],
                    label="Where we meet",
                    confidence=0.9,
                    source="community_create",
                    contributed_by=user_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("community_meets_at_write_failed")
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
            # The creator's own words become the community's description — only when it
            # has none, so joining an existing community never overwrites what is there.
            # No blurb_key: a person wrote it, so the profile never regenerates it. This is
            # what lets a new community be found by topic from its first minute; before,
            # places.blurb stayed empty until someone opened the profile (2026-10-07).
            try:
                from app.auth import service_client

                service_client().table("places").update(
                    {"blurb": str(draft["blurb"]).strip()[:300]}
                ).eq("id", place_id).is_("blurb", "null").execute()
            except Exception:  # noqa: BLE001
                logger.exception("community_place_blurb_write_failed")
        from app.community_embeddings import embed_place_later

        embed_place_later(place_id)
    return {**result, "place_id": place_id}, ""


def _handle_offer(place_id: str, user_id: str | None) -> dict[str, str] | None:
    """The short link this user may claim for the community they just published, or None.

    Who may claim is the database's call (community_handle_offer_for, 20270108120000): the
    community's operator, or the creator of a name-only one. A guest has no confirmed email,
    so is never asked. Best effort — an offer that fails must not cost the community."""
    if not place_id or not user_id:
        return None
    try:
        from app.auth import service_client

        res = (
            service_client()
            .rpc("community_handle_offer_for", {"p_user_id": user_id, "p_place_id": place_id})
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("community_handle_offer_failed")
        return None
    data = res.data if isinstance(res.data, dict) else {}
    suggestion = str(data.get("suggestion") or "").strip()
    if not data.get("eligible") or not suggestion:
        return None
    return {"place_id": place_id, "suggestion": suggestion}


def _planned_place_key(draft: dict[str, Any]) -> str:
    """The google_place_id publish_community will use — the picked place, or for a
    community with no place the creator:<slug> its NAME maps to (same rule as publish)."""
    gpid = str(draft.get("google_place_id") or "").strip()
    if gpid:
        return gpid
    from app.circles_capture import _slugify
    from app.circles_flow import CREATOR_PLACE_PREFIX
    from app.community_question_sets import COMMUNITY_SUBJECT_FIELD

    answers = draft.get("answers") or {}
    name = str(answers.get(COMMUNITY_SUBJECT_FIELD) or "").strip() or str(
        draft.get("name") or ""
    ).strip()
    slug = _slugify(name)
    return CREATOR_PLACE_PREFIX + slug if slug else ""


def _link_check(user_id: str | None, handle: str, draft: dict[str, Any]) -> dict[str, Any]:
    """check_community_handle_for (20270130120000) — may they have this link for the
    community they are about to create. {"status": "error"} when the read fails."""
    if not user_id:
        return {"status": "sign_in_required"}
    try:
        from app.auth import service_client

        res = (
            service_client()
            .rpc(
                "check_community_handle_for",
                {
                    "p_user_id": user_id,
                    "p_handle": handle,
                    "p_name": str(draft.get("name") or ""),
                    "p_google_place_id": _planned_place_key(draft) or None,
                },
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("community_link_check_failed")
        return {"status": "error"}
    return res.data if isinstance(res.data, dict) else {"status": "error"}


def _claim_link(user_id: str, place_id: str, handle: str) -> str | None:
    """Claim the reserved link once the community exists; on a race, the first suggestion
    the database offers. Returns the handle it now has, or None."""
    from app.auth import service_client

    for attempt in range(2):
        try:
            res = (
                service_client()
                .rpc(
                    "claim_community_handle_for",
                    {"p_user_id": user_id, "p_place_id": place_id, "p_handle": handle},
                )
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("community_link_claim_failed place=%s", place_id)
            return None
        data = res.data if isinstance(res.data, dict) else {}
        status = data.get("status")
        if status == "claimed":
            return str(data.get("handle") or handle)
        suggestions = [s for s in (data.get("suggestions") or []) if isinstance(s, str)]
        if status == "unavailable" and suggestions and attempt == 0:
            # Taken between the check and the publish — take the next free one rather
            # than publish without the link they were told they would get.
            handle = suggestions[0]
            continue
        logger.info("community_link_not_claimed place=%s status=%s", place_id, status)
        return None
    return None


def _chapter_link(draft: dict[str, Any], place_id: str) -> str | None:
    """get.lana.help/{parent}/{chapter} for a community just attached as a chapter — the
    chapter part is assigned on attach (20270131120000). None when the parent has no link
    or the read fails; the caller then falls back to the community's own link."""
    parent_handle = str((draft.get("parent_place") or {}).get("handle") or "").strip()
    if not parent_handle or not place_id:
        return None
    try:
        from app.auth import service_client

        row = (
            service_client()
            .table("places")
            .select("chapter_handle")
            .eq("id", place_id)
            .limit(1)
            .execute()
        )
        got = (row.data or [{}])[0] if isinstance(row.data, list) else {}
    except Exception:  # noqa: BLE001
        logger.exception("community_chapter_link_read_failed place=%s", place_id)
        return None
    chapter = str(got.get("chapter_handle") or "").strip()
    return f"{parent_handle}/{chapter}" if chapter else None


def _hq_offer(session_ctx: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any] | None:
    """{"city", "lat", "lng"} for the user's own ZIP — the run-from city they most likely
    mean, offered as a chip. Geocoded once per draft and kept on it, so a tap on the chip
    is placed from these coordinates rather than geocoded a second time."""
    kept = draft.get("hq_offer")
    if isinstance(kept, dict) and kept.get("city"):
        return kept
    zip5 = str(session_ctx.get("zip_code") or session_ctx.get("zip") or "").strip()[:5]
    if not (len(zip5) == 5 and zip5.isdigit()):
        return None
    from app.community_hq import geocode_city

    got = geocode_city(f"{zip5}, USA")
    if not got:
        return None
    draft["hq_offer"] = got
    return got


def _after_questions(
    *, draft: dict[str, Any], session_ctx: dict[str, Any], user_id: str | None,
    chips: list[dict[str, Any]] | None = None,
) -> str:
    """Every question is answered. What is left before the ready card, in order: where a
    community with no place is run from, then its link (required, prefilled). Then the
    ready card — nothing is created until they press Share."""
    session_ctx["community_create_active"] = True
    session_ctx["routing_phase"] = "listening"
    name = str(draft.get("name") or "your community")
    # "Inside SJSU" is settled here, before the closing questions: a chapter with no spot
    # of its own meets at its parent's (attach_chapter copies the parent's point), and its
    # link is get.lana.help/{parent}/{chapter}, given on attach — so neither is asked.
    _resolve_parent(draft, user_id)
    # The one question the creator rarely answers unprompted — is it inside one of their
    # bigger communities? — goes first: the answer can make the city and link unnecessary.
    asked = _maybe_ask_parent(session_ctx, draft, user_id)
    if asked:
        return asked
    parent = draft.get("parent_place") or {}
    parent_located = bool(parent.get("located"))
    if parent.get("handle") and not draft.get("handle"):
        draft["_link_settled"] = True

    if (
        not str(draft.get("google_place_id") or "").strip()
        and not draft.get("hq_city")
        and not parent_located
    ):
        session_ctx["community_pending_ask"] = "hq"
        session_ctx["community_ready"] = None
        draft["pending_field"] = "hq"
        # Their own area as a one-tap answer: the question had no control at all, so a
        # neighbour in a known ZIP still had to type the city they are standing in.
        offer = _hq_offer(session_ctx, draft)
        draft["suggestions"] = [offer["city"]] if offer else []
        session_ctx["community_offered"] = list(draft["suggestions"])
        session_ctx["community_draft"] = draft
        facts = [f"The community: {name}"]
        if offer:
            facts.append(
                f"Their own area, offered as a tap under your message: {offer['city']}. "
                "A search box for any other city sits there too."
            )
        return compose_reply(
            goal=(
                "Before their community is ready, ask in one short line which city it is "
                "run from. Say it is just for its card and map pin — anyone, anywhere, can "
                "still find and join it."
            ),
            facts=facts,
            fallback=(
                "One more thing — which city is it run from? It's just for the card; anyone "
                "anywhere can still join."
            ),
        )

    if not draft.get("handle") and not draft.get("_link_settled"):
        res = _link_check(user_id, "", draft)
        status = res.get("status")
        if status == "already_has_handle":
            # Publishing joins a community that already has its link: show that one.
            draft["handle"] = str(res.get("handle") or "") or None
            draft["_link_settled"] = True
        elif status in ("not_eligible", "sign_in_required", "error"):
            # Not theirs to link (someone else's place), or we cannot check: no step that
            # would fail at publish. Claiming stays possible later from its edit screen.
            draft["_link_settled"] = True
        else:
            suggestions = [s for s in (res.get("suggestions") or []) if isinstance(s, str)]
            session_ctx["community_pending_ask"] = "handle"
            session_ctx["community_ready"] = None
            draft["pending_field"] = "handle"
            draft["handle_suggestion"] = suggestions[0] if suggestions else None
            draft["handle_suggestions"] = suggestions
            draft["suggestions"] = suggestions[:3]
            session_ctx["community_offered"] = suggestions[:3]
            session_ctx["community_draft"] = draft
            return compose_reply(
                goal=(
                    "Last step before their community is ready: they choose its link, "
                    "get.lana.help/<name>, so people can find and join it. One short line; "
                    "the link box under your message is already filled in with a suggestion "
                    "they can keep or change."
                ),
                facts=[f"The community: {name}"]
                + (
                    [f"The suggested link: get.lana.help/{suggestions[0]}"]
                    if suggestions
                    else []
                ),
                fallback="Last step — choose your community's link so people can find it.",
            )

    # ── Ready card + the share CTA (nothing is created until they confirm: a community is
    # shared state other people join). ──
    if chips is not None:
        draft["chips"] = chips
    draft["suggestions"] = []
    draft["pending_field"] = None
    draft["ready"] = True
    session_ctx["community_draft"] = draft
    session_ctx["community_ready"] = True
    session_ctx["community_pending_ask"] = None
    session_ctx["community_pending_question"] = None  # nothing outstanding on the card
    facts = [f"Community ready: {name}"]
    if draft.get("handle"):
        facts.append(f"Its link: get.lana.help/{draft['handle']}")
    return compose_reply(
        goal=(
            "The community draft is complete and shown as a card. Tell the user it's ready "
            "and prompt them to tap **Share with the community** (keep that button name "
            "verbatim, bolded) so neighbours can find and join it."
        ),
        facts=facts,
        fallback=(
            f"It's ready to share — **{name}**. One last look, then "
            "**Share with the community** and neighbours can find and join it."
        ),
    )


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

    # Never on the SEED turn (nothing asked yet, so nothing to pivot away from). That turn
    # was already read as a create — usually by `looks_like_community_create`, which exists
    # precisely because the classifier reads the bare "I want to create a community" as
    # sharing.host 4/4. Re-asking the same classifier here undid the arming on the spot and
    # the turn fell through to decide_turn: dev 2026-09-07, the "Create a community" CTA
    # answered "want to set one up for your kids, your gym…?" with policy chips and no
    # capture ever started.
    if not int(session_ctx.get("community_turns") or 0):
        return False

    return not lane_should_continue(
        message,
        session_ctx,
        slots,
        is_valid_answer=_is_community_answer,
        is_offered_option=_is_carousel_handoff,
    )


def _is_carousel_handoff(
    message: str, session_ctx: dict[str, Any], slots: "dict[str, Any] | None" = None
) -> bool:
    """The carousel's own "Looks good", sent right after /community-setup stamped the
    answers and `community_ready` — the only thing that renders the ready card (§38b).

    Nothing else here treats it as in-lane: match_type_label misses it, community_offered
    holds only the current step's options, and a goal=chat read short-circuits
    _is_community_answer — so the lane released and reset_community_state threw away a
    filled-in carousel plus the community_ready the endpoint had just written. The host
    flow's _is_host_confirm intercepts the same words ahead of any release check; this is
    that, for this lane. A rendered control: exact match, scoped to the state the client
    sends it in (a step set and community_ready), and ahead of abandon."""
    from app.lane_decision import is_setup_handoff

    draft = session_ctx.get("community_draft")
    return (
        isinstance(draft, dict)
        and bool(draft.get("step_set"))
        and bool(session_ctx.get("community_ready"))
        and is_setup_handoff(message)
    )


# What this capture OWNS. Anything the AI confidently reads as a different lane is a pivot
# and releases. Self-maintaining via is_confident_off_lane (no foreign-list to maintain).
_NATIVE_GOALS = frozenset({"create_community"})
_NATIVE_SIGNALS: frozenset[str] = frozenset()
# `sharing.community` is the registered intent id (layer1_intents.LINEAR_INTENTS) — a name
# that is not in that registry can never match a classified turn, so the lane read its
# OWN correct read as a foreign intent and released every turn (dev 2026-09-07).
_NATIVE_LINEARS = frozenset({"sharing.community"})


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
        GROUP_TYPES,
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
    # A tapped "Part of …" chip is a rendered control; a community whose name happens to
    # hold a cancel word ("Stop the Stigma") must not throw the draft away.
    tapped_parent_chip = session_ctx.get("community_pending_ask") == _PARENT_ASK and _norm_label(
        msg
    ) in {_norm_label(o) for o in (draft.get("suggestions") or [])}
    cancelled = False if tapped_parent_chip else (
        _is_bare_control(msg, _CANCEL_RE)
        if _awaiting_text_subject(session_ctx, draft)
        else bool(_CANCEL_RE.search(msg))
    )
    if cancelled or turns > _COMMUNITY_TURN_CAP:
        reset_community_state(session_ctx)
        session_ctx["routing_phase"] = "listening"
        return compose_reply(
            goal="The user dropped the community they were setting up. Let it go warmly, in one line, and leave the door open.",
            facts=["The community was not created", "Nothing was shared with anyone"],
            fallback="No problem — I've let that go. Tell me when you want to start one.",
        )

    # ── The answer to "is it part of something bigger?" ──
    # Read before anything else: a tapped "Part of SJSU" is not a name, a blurb or a
    # publish. A correction chip is a rendered control of its own and goes to its branch.
    if session_ctx.get("community_pending_ask") == _PARENT_ASK and not re.match(
        r"\s*fix:\w+\s*$", msg
    ):
        session_ctx["community_pending_ask"] = None
        session_ctx["community_offered"] = []
        outcome = _apply_parent_answer(draft, msg, user_id)
        if outcome == "unclear" and not draft.get("parent_reasked"):
            draft["parent_reasked"] = True
            return _ask_parent(session_ctx, draft, again=True)
        if outcome == "unclear":
            draft["parent_declined"] = True
        if outcome != "none":
            # On to whatever closing question is left (a chapter of a located parent
            # skips the city; one of a linked parent skips the link), then the ready card.
            return _after_questions(draft=draft, session_ctx=session_ctx, user_id=user_id)
        # "none": not an answer — the turn carries on as whatever it is (an edit, a
        # question), and the parent question is not asked again.

    # ── The answers to the two closing questions: where it is run from, and its link ──
    # Both are asked by _after_questions once every question is in, so an answer goes
    # straight on to whatever is left — they already did the rest.
    pending = session_ctx.get("community_pending_ask")
    # A tapped correction chip ("fix:name") is a rendered control, not an answer.
    if pending in ("hq", "handle") and not re.match(r"\s*fix:\w+\s*$", msg):
        if pending == "hq":
            from app.community_hq import geocode_city

            offer = draft.get("hq_offer")
            if (
                isinstance(offer, dict)
                and offer.get("city")
                and msg.strip().casefold() == str(offer["city"]).casefold()
            ):
                got = offer
            else:
                got = geocode_city(msg)
            if not got:
                draft["pending_field"] = "hq"
                session_ctx["community_draft"] = draft
                return compose_reply(
                    goal=(
                        "You couldn't place what they gave as the city their community is "
                        "run from. Ask again in one short line for a town or city (e.g. a "
                        "city and state), saying it shows on the community's card and map pin."
                    ),
                    facts=[f'They said: "{msg[:80]}"'],
                    fallback="I couldn't place that — which city is it run from?",
                )
            draft["hq_city"], draft["hq_lat"], draft["hq_lng"] = (
                got["city"], got["lat"], got["lng"],
            )
            # The area chip answered its question; it must not ride on to the next card.
            draft["suggestions"] = []
            session_ctx["community_offered"] = []
        else:
            res = _link_check(user_id, msg, draft)
            if res.get("status") != "available":
                suggestions = [s for s in (res.get("suggestions") or []) if isinstance(s, str)]
                if suggestions:
                    draft["handle_suggestions"] = suggestions
                    draft["suggestions"] = suggestions[:3]
                    session_ctx["community_offered"] = suggestions[:3]
                draft["handle_error"] = str(res.get("reason") or res.get("status") or "")
                draft["pending_field"] = "handle"
                session_ctx["community_draft"] = draft
                return compose_reply(
                    goal=(
                        "The link they chose for their community can't be used. Say so in "
                        "one short line and point them to the suggestions under your message "
                        "— they can tap one or type another."
                    ),
                    facts=[
                        f'They asked for: get.lana.help/{res.get("normalizedHandle") or msg[:40]}',
                        f'Why not: {res.get("reason") or res.get("status")}',
                    ],
                    fallback="That link isn't available — pick one below or try another.",
                )
            draft["handle"] = str(res.get("normalizedHandle") or "")
            draft["handle_error"] = None
        session_ctx["community_pending_ask"] = None
        return _after_questions(draft=draft, session_ctx=session_ctx, user_id=user_id)

    # ── Publish: the ready card's CTA ──
    if session_ctx.get("community_ready") and _PUBLISH_RE.search(msg):
        # Which community "inside X" means is settled before the ready card
        # (_after_questions); this is a no-op then, and a safety net for older drafts.
        _resolve_parent(draft, user_id)
        # Normally asked before the ready card (_after_questions); a draft that reached
        # share without passing through it (the carousel stamps community_ready itself) is
        # asked here, ahead of the city — a located parent makes that question unnecessary.
        asked = _maybe_ask_parent(session_ctx, draft, user_id)
        if asked:
            return asked
        # A ready card from before the closing steps moved ahead of it (no city for a
        # placeless community) goes back through them rather than publishing unplaced.
        if (
            not str(draft.get("google_place_id") or "").strip()
            and not draft.get("hq_city")
            and not (draft.get("parent_place") or {}).get("located")
        ):
            return _after_questions(draft=draft, session_ctx=session_ctx, user_id=user_id)
        result, err = publish_community(draft=draft, user_id=str(user_id or ""))
        if not result:
            if err == "place_required":
                # Recoverable in-turn: re-open the subject step instead of apologising.
                session_ctx["community_pending_ask"] = COMMUNITY_SUBJECT_FIELD
                draft["pending_field"] = COMMUNITY_SUBJECT_FIELD
                draft["suggestions"] = _place_suggestions(
                    draft, zip_code=zip_code, block_id=block_id, user_jwt=user_jwt
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
        # Creating a community is the strongest possible "I am here" — so the top-of-app
        # filter follows it. Without this the scope stayed unset and the recommendation
        # filed two minutes later knew nothing about the community just made (Tommaso,
        # prod 2026-09-14). app/community_scope.py owns the key.
        if result.get("place_id"):
            from app.community_scope import CTX_KEY

            session_ctx[CTX_KEY] = {
                "place_id": str(result["place_id"]),
                "name": str(draft.get("name") or "").strip(),
            }
        draft["community_id"] = result.get("place_id") or result.get("affiliation_id")
        if draft.get("hq_city") and result.get("place_id") and user_id:
            from app.community_hq import write_community_hq

            # Best effort, and SQL decides: joining an existing same-name community does
            # not make its HQ theirs to set.
            write_community_hq(
                str(user_id),
                str(result["place_id"]),
                {"city": draft["hq_city"], "lat": draft.get("hq_lat"), "lng": draft.get("hq_lng")},
            )
        # The link they chose on the last step is claimed now that the community exists.
        # If that could not happen (it was someone else's place after all, or the claim
        # failed), the old offer — a "claim this link" button — is the way back to it.
        place_id = str(result.get("place_id") or "")
        name = str(draft.get("name") or "your community")
        facts = [f"{name} is now a community neighbours can find and join"]
        # Inside a parent first: a chapter of a linked parent shares
        # get.lana.help/{parent}/{chapter}, assigned on attach — no link of its own to claim.
        facts += _attach_to_parent(draft, user_id, place_id)
        link = _chapter_link(draft, place_id) if draft.get("parent_attached") else None
        claimed = None
        if link:
            draft["handle"] = link
        elif draft.get("handle") and place_id and user_id and not draft.get("_link_settled"):
            claimed = _claim_link(str(user_id), place_id, str(draft["handle"]))
            draft["handle"] = claimed
        offer = None if (claimed or draft.get("handle")) else _handle_offer(place_id, user_id)
        draft["handle_offer"] = offer
        draft["ready"] = True
        session_ctx["community_draft"] = draft
        session_ctx["community_published_now"] = True
        session_ctx["community_ready"] = None
        session_ctx["community_create_active"] = None
        session_ctx["community_turns"] = 0
        session_ctx["routing_phase"] = "listening"
        if draft.get("handle"):
            facts.append(f"Its link, to share anywhere: get.lana.help/{draft['handle']}")
        elif offer:
            facts.append(
                f"They can claim a short link for it, get.lana.help/{offer['suggestion']}, "
                "with the button below (or pick a different one there)"
            )
        return compose_reply(
            goal=(
                "The community is live. Celebrate briefly and warmly, say neighbours can "
                "now find and join it, and that you'll point people to it when they ask."
            ),
            facts=facts,
            fallback=f"🎉 **{name}** is up — neighbours can find it and ask to join. I'll point people to it.",
        )

    # ── Correction: chip tap "fix:<field>" → clear + re-ask that field ──
    fix = re.match(r"\s*fix:(\w+)\s*$", msg)
    if fix:
        field = fix.group(1)
        if field in ("handle", "hq"):
            # The two closing steps: tapping the link or the city on the ready card reopens
            # just that step; whatever else is set stays (2026-10-06).
            if field == "handle":
                for k in ("handle", "handle_error", "_link_settled"):
                    draft.pop(k, None)
            else:
                for k in ("hq_city", "hq_lat", "hq_lng"):
                    draft.pop(k, None)
            draft["ready"] = False
            return _after_questions(draft=draft, session_ctx=session_ctx, user_id=user_id)
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
        elif field == "parent":
            # Not re-asked: "part of" is optional, so removing it is the whole correction —
            # and Lana's own "is it part of…?" never comes back for this draft.
            for k in ("parent", "parent_place", "parent_unresolved", "_link_settled"):
                draft.pop(k, None)
            draft["parent_declined"] = True
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
                options = _place_suggestions(
                    draft, zip_code=zip_code, block_id=block_id, user_jwt=user_jwt
                )
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
    passed = (
        _is_bare_control(msg, _PASS_RE)
        if _awaiting_text_subject(session_ctx, draft)
        else bool(_PASS_RE.search(msg))
    )
    if pending and msg and not passed and not tapped_type:
        if pending == COMMUNITY_SUBJECT_FIELD:
            # A place is only ever set by the picker (/community-setup), never by text:
            # a typed name cannot be grounded. Left pending so the step is re-asked.
            #
            # EXCEPT when the subject step is declared kind="text" — which today means a
            # creator community, whose subject question is literally "What's the community
            # called?" because there is no location to pin. The step set has always said so
            # (community_question_sets, and test_community_capture asserts it); this handler
            # simply did not honour it, which is why publish_community's subject-wins rule
            # never fired and a community ended up named "people who follow my Jack Russell
            # account" — the opening phrase, not the name its creator chose.
            #
            # Keyed off the step's OWN kind rather than the type name, so a second placeless
            # type cannot quietly reintroduce the bug.
            subject_step = next(
                (st for st in step_set_of(draft) if st.get("field") == COMMUNITY_SUBJECT_FIELD),
                None,
            )
            if (subject_step or {}).get("kind") == "text":
                draft["answers"] = {
                    **(draft.get("answers") or {}),
                    COMMUNITY_SUBJECT_FIELD: msg,
                }
                session_ctx["community_pending_ask"] = None
            elif normalize_community_type(draft.get("circle_type")) in GROUP_TYPES:
                # A club answering "where do you all meet up?" in chat ("the engineering
                # building on campus") was dropped: chat has no picker and the step is
                # never re-asked, so the draft went ready with no location (QA
                # 2026-10-05). It is not the community's pin — a pin renames the
                # community after the building — so it is kept as where the group
                # meets, and published as that.
                draft["meets_at"] = msg[:200]
                session_ctx["community_pending_ask"] = None
        elif pending == "circle_type":
            resolved = normalize_community_type(msg)
            if resolved:
                draft["circle_type"] = resolved
                session_ctx["community_pending_ask"] = None
        elif pending in {s["field"] for s in step_set_of(draft)}:
            draft["answers"] = {**(draft.get("answers") or {}), pending: msg}
            session_ctx["community_pending_ask"] = None

    # ── Extract fields (+ the set, once) ──
    # Not on the carousel's hand-off: its answers are already stamped, and "Looks good"
    # is a control, not content to read for a name or a blurb.
    if msg and not tapped_type and not _is_carousel_handoff(msg, session_ctx):
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
        session_ctx["community_pending_question"] = "What should I add as a community?"
        session_ctx["routing_phase"] = "listening"
        return compose_reply(
            goal=(
                "Ask what they want to add as a community, and mention they can say what "
                "brings people to it. Warm, one or two short lines."
            ),
            # This turn runs BEFORE the type is known, so it must not foreclose either
            # answer. It used to assert "a community is always a real place" — true until
            # 20261207120000, and now the one thing that would talk a creator out of the
            # community they came to make.
            facts=[
                "Nothing captured yet",
                "Usually a real place nearby, but it can also be a community built around "
                "a shared interest with no location at all",
            ],
            fallback="Love that — what should I add as a community? A spot near you, or something people gather around.",
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
        lead = readback(session_ctx, "community_readback", draft.get("draft_id"), _summary(draft))
        return f"{lead}What kind of place is it?"

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
                _place_suggestions(
                    draft, zip_code=zip_code, block_id=block_id, user_jwt=user_jwt
                )
                if step.get("kind") == "place"
                else []
            )
            session_ctx["community_draft"] = draft
            session_ctx["community_offered"] = draft["suggestions"]
            session_ctx["community_create_active"] = True
            session_ctx["community_pending_question"] = step["question"]
            session_ctx["routing_phase"] = "listening"
            # Position, not "answered + 1": a skipped optional step stays unanswered, and
            # counting only answers gave the next question the same number (two "3/6"s,
            # QA 2026-10-05). Everything already asked or answered is behind us.
            fields = {s["field"] for s in steps}
            behind = {s["field"] for s in steps if s.get("answer")} | (
                (asked & fields) - {step["field"]}
            )
            lead = readback(session_ctx, "community_readback", draft.get("draft_id"), _summary(draft))
            return (
                f"{lead}{step['question']} "
                f"({min(len(behind) + 1, len(steps))}/{len(steps)})"
            )

    # ── P4: every question is in → the closing questions, then the ready card ──
    return _after_questions(draft=draft, session_ctx=session_ctx, user_id=user_id, chips=chips)
