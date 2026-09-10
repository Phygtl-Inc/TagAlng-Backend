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
import logging
import re
import uuid
from typing import Any

from app.reply_compose import compose_reply, readback

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
    r"go ahead|done|send it|share with (?:the |my )?communit(?:y|ies))\b",
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
{"name","category","trait","locality","reco_type","place_based","answers","reply_role","weak",<<STEPS_KEY>>"ask"}

- name: the specific who/where being recommended, e.g. "Dr. Sarah", "Canvas Restaurant", "Lake Nona Park". null if not stated.
- category: what kind of recommendation, e.g. "pediatric dentist","restaurant","playground","plumber","pediatrician". null if unclear.
- trait: why it's good / the standout detail, e.g. "twin-friendly","amazing tacos","gentle with toddlers". null if not stated.
- locality: neighborhood/area if mentioned, e.g. "Lake Nona". null otherwise.
- reco_type: EXACTLY one of <<TYPES>>, or null if genuinely unclear. <<TYPE_RULES>>
- answers: object mapping any of the CURRENT TYPE FIELDS listed below to what the user ALREADY said,
  verbatim-ish and short. Omit a field rather than guess it. {{}} when nothing was said.
<<STEPS_SPEC>>- reply_role: what the user's new message IS, relative to the question they were just
  asked (listed under CURRENT TYPE FIELDS / the pending question):
    "answer"   - they answered it, even loosely or badly.
    "asks_why" - they are questioning the QUESTION rather than answering it: "why are you
                 asking this?", "why should i answer this", "what do you need that for?",
                 "is this necessary?", "who sees this?". A question back at you, not an
                 answer. Read the INTENT, not the wording — there is no list to match.
    "off_topic"- neither: a remark about something else entirely.
  You are the only one who can call this, because you are the only one holding the question
  they were asked. Get it wrong towards "answer" and their question gets stored as their
  answer, which is what used to happen.
- weak: the ONE answer the user JUST GAVE, in this message, that does not answer the
  question it was asked. Only that one — a set submitted from the card carousel is judged
  where it arrives, and judging it again here reopened a form the user had just fixed:
  {"field": <the key from CURRENT TYPE FIELDS>, "why": <max 8 words>}. Judge shape,
  not length — a two-word answer is fine, a wrong-shaped one is not: "hundred dollars" to
  "what's the price range?", "good" to "what's it known for?", "idk", "ask me" to "how do
  neighbours reach them?". null whenever the answer fits, even loosely, and null when the
  user was not answering a question at all.
- place_based: true if this is a PLACE or business you could find on a map (restaurant, park, clinic, salon);
  false if it's a person/word-of-mouth service with no fixed public listing (a nanny, a handyman by referral).
- ask: the single MOST useful follow-up to make this a strong tip, TAILORED to what's still unknown,
  with tappable answers that fit (e.g. cuisine for a restaurant, age-fit for a doctor). Shape:
  {"field": <short snake_case key>, "question": <one short question>, "options": [2-4 short answers]}.
  Return null for `ask` when name + category + trait is already enough. Do NOT ask for a phone number.

Use null for any string the text does not support, false for place_based when unsure. Never invent a value."""


_STEPS_SPEC = """- steps: the question set for THIS recommendation — 6-8 questions, in the order to ask them.
  Each: {"field": short snake_case key, "label": 1-2 word eyebrow, "question": ONE short
  question ending in "?", "placeholder": a short example answer for THIS subject,
  "options": [2-4 short taps] — required on every question but the basics, see (c),
  "multi": true only when SEVERAL of those taps can be true at once}.
  Set `multi` for a question like "what is it known for?" (journals AND pens AND cards) or
  "who is it best for?" (families AND dogs). Leave it off for anything that has exactly one
  answer: a price band, a spice level, how long the wait is, yes/no.
  The FIRST step is ALWAYS {"field": "subject"} — what/who is being recommended, phrased
  for THIS subject: "Which trampoline park?", "What's the kettle called?", "What's the
  doctor's name?". Never "who or where". Write it even when the name is already known; it
  is shown answered rather than asked.
  Then the type's own basics: <<FLOOR>>.
  Then at least THREE questions specific to THIS subject. Before writing them, decide
  silently: who is looking for this, and what would make that person PICK IT or SKIP IT?
  Ask those. A pediatrician: which ages, does she take walk-ins, which insurance, weekend
  hours. A taqueria: parking or street only, the line at lunch, high chairs, cash only.
  A stroller: which car seat it clicks into, weight, fits a sedan trunk. Answerable in a
  few words.
  TWO TESTS every one of them must pass, or rewrite it:
  (a) Could ONLY someone who has actually been there / used it answer this? If a stranger
      could answer it off the listing or a five-second search, it is banned — opening
      hours, phone number, website, address, the $$ price band. Those Lana can look up;
      the neighbour is the only source for what she cannot.
  (b) Would this question read exactly the same for ANY other subject of this type? Then
      it is too generic to keep. "What is the price range?" fits every shop on earth —
      "Do they price-match Amazon at the register?" fits this one. "Is it nice?" fits
      every trail — "Any shade, or full sun the whole way?" fits this one.
  (c) Does it come with `options` — 2-4 taps covering the realistic answers? This is not a
      preference. A facet with no answer set renders as a free-text box, which is where
      "hundred dollars" came from, and it is DROPPED before the user ever sees it. If you
      cannot write four plausible answers, you do not know this subject well enough to ask
      that question — pick a different facet. "What is the price range?" has no sane set
      for a bookstore; "Which ages?" has an obvious one: Babies · Toddlers · Big kids ·
      Teens. The only questions allowed without options are the type's own basics above (a
      phone number has no answer set) and anything answered on the map.
  Name the subject in the wording — "How long is the wait at Zaiqa on a Friday?", not
  "How long is the wait?".
  BANNED (they read well and filter nothing): "what stood out", "what did you like", "why
  is it good", "anything else", "tell me more", anything already in CURRENT TIP DRAFT (the
  user's own trait is captured — do not ask for it again), and any two questions that would
  take the same answer.
  ALSO BANNED when the subject is a place, business or person you could look up: opening
  hours, phone number, website, address, price range or $$-band. Lana can look those up;
  the neighbour is the only source for what she CAN'T — how long the wait really is, which
  ages it suits, whether the parking is a nightmare, what regulars order.
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
        role = str(data.get("reply_role") or "").strip().lower()
        if role in ("answer", "asks_why", "off_topic"):
            out["reply_role"] = role
        weak = data.get("weak")
        if isinstance(weak, dict) and str(weak.get("field") or "").strip():
            out["weak_answer"] = {
                "field": str(weak["field"]).strip(),
                "why": " ".join(str(weak.get("why") or "").split())[:60],
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


_JUDGE_SYSTEM = """You check whether answers on a recommendation card actually answer the \
questions they were given.

You get QUESTION → ANSWER pairs. Return ONE compact JSON object:
{"weak": [{"field": <field key>, "why": <max 8 words, see below>, "reply": <or null>}]}
with an entry for EVERY answer that does not answer its question, and "weak": [] when they
all fit. Do not invent problems to fill the list — most sets have none.

Judge SHAPE, not length or eloquence. "Fade" is a fine answer to "what did they do for
you?". These are not: "bla bla bla", "what?", "why?", "idk", "everything", "good", "ask me"
to a question about contact, a dollar amount where a range was asked for, or an answer that
belongs to a different question.

`why` names what is MISSING, and never judges the person or their answer: "not a price
range", "no place to buy it", "does not say which ages". NEVER "nonsense", "gibberish",
"meaningless", "garbage", "not a real answer" — this is a neighbour doing us a favour, and
the line is shown to them on their own card.

`reply`: when the answer is itself a QUESTION BACK AT YOU — "why?", "what?", "why should I
tell you?", "who sees this?" — they are not being difficult, they are asking what the
question is for, and naming the problem back at them ("answered with a question") answers
nothing. Set `reply` to ONE short warm line, in Lana's voice, that ANSWERS them: what a
neighbour reading this card would actually do with the answer, then the question again.
Never scold, never mention forms or validation. Otherwise null."""

_MAX_WEAK = 4


def judge_answers(
    step_set: list[dict[str, Any]],
    answers: dict[str, Any],
    *,
    fields: Any = None,
    skip: Any = (),
) -> list[dict[str, str]]:
    """Every answer in a batch that does not answer its question, worst first. For the
    carousel fork, where a whole set arrives at once and the per-turn extractor never sees
    them being typed.

    ALL of them, not the worst one: the chat fork nudges once because a conversation that
    lists corrections is a form with a face, but the carousel IS a form — and handing back
    one problem per submit made fixing three answers cost three round trips (dev QA
    2026-09-08).

    `skip` is the fields already queried once, so a second submit of the same words always
    goes through and nobody is trapped on a card.
    """
    only = set(fields) if fields is not None else None
    skipped = set(skip or ())
    pairs = [
        (s["field"], s["question"], str(answers.get(s["field"]) or "").strip())
        for s in step_set or []
        if str(answers.get(s["field"]) or "").strip()
        and s["field"] not in skipped
        and (only is None or s["field"] in only)
        # Never argue with our own chip: an offered option is valid by construction. A
        # multi-select answer is several of them, joined.
        and not _all_offered(answers.get(s["field"]), s.get("options"))
    ]
    if not pairs:
        return []
    try:
        from app.orchestrator.llm import llm_configured, llm_json, synthesizer_model

        if not llm_configured():
            return []
        data = llm_json(
            model=synthesizer_model(),
            system=_JUDGE_SYSTEM,
            user_payload="\n".join(f"{f} | {q} → {a}" for f, q, a in pairs),
            max_tokens=400,
            temperature=0.1,
        )
        asked = {f for f, _, _ in pairs}
        out: list[dict[str, str]] = []
        for item in (data or {}).get("weak") or []:
            field = str((item or {}).get("field") or "").strip()
            if not isinstance(item, dict) or field not in asked:
                continue
            if any(o["field"] == field for o in out):
                continue
            row = {
                "field": field,
                "why": " ".join(str(item.get("why") or "").split())[:60]
                or "that does not answer it",
            }
            # An answer to their question outranks a description of it: the card shows this
            # instead of "answered with a question, not info", which told them nothing.
            reply = " ".join(str(item.get("reply") or "").split())
            if reply:
                row["reply"] = reply[:200]
            out.append(row)
        return out[:_MAX_WEAK]
    except Exception:  # noqa: BLE001 - a judge that errors must never block a submit
        logging.getLogger(__name__).exception("tip_judge_failed")
        return []


def _all_offered(answer: Any, options: Any) -> bool:
    """Is this answer made up entirely of taps we offered? Multi-select joins them with a
    comma, so "Journals, Cards & gifts" is two of our own chips and not a typed answer."""
    opts = {" ".join(str(o).split()).casefold() for o in (options or [])}
    if not opts:
        return False
    parts = [p.strip().casefold() for p in str(answer or "").split(",") if p.strip()]
    return bool(parts) and all(p in opts for p in parts)


def step_set_of(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """The recommendation's question set: the one Lana wrote for it, or the type's static
    set until she has (and if generation fails, forever — the flow never blocks on it)."""
    generated = draft.get("step_set")
    if isinstance(generated, list) and generated:
        return generated
    from app.reco_question_sets import steps_for

    return steps_for(
        draft.get("reco_type"),
        place_based=bool(draft.get("place_based")),
        subject_hint=draft.get("category"),
    )


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
        if s.get("answer") and s["field"] not in (*TAIL_FIELDS, SUBJECT_FIELD, COMMUNITY_FIELD)
    ]
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    if _has(draft, "locality"):
        parts.append(str(draft["locality"]).strip())
    return " · ".join([p for p in parts if p]) or str(draft.get("name") or "recommendation")


def _name_suggestions(
    draft: dict[str, Any],
    *,
    zip_code: str | None,
    block_id: str | None,
    user_jwt: str | None = None,
) -> list[str]:
    """Real nearby places (Google Places) matching the category — only for place-based
    tips. [] otherwise, so the flow falls back to free-type.

    `user_jwt` is what makes this work for a real account. A map search needs a centre, and
    of the three ways to find one — a passed ZIP, a DEV block id, the user's home location —
    only the third holds for a signed-in user whose session has no ZIP in it. Without it the
    search was skipped, the subject step arrived with no places to tap, and the user was
    left typing the name of a shop Lana could have found (dev QA 2026-09-08)."""
    from app.auth import jwt_user_id
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
            query=str(draft["category"]),
            zip_code=zip_code,
            block_id=block_id,
            user_id=jwt_user_id(user_jwt) if user_jwt else None,
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
        # COMMUNITY out with the subject: where the tip was SENT is not something the card
        # says about the place, and it lives in circle_place_ref already.
        if s.get("answer") and s["field"] not in (SUBJECT_FIELD, COMMUNITY_FIELD)
    ]
    return out or None


def _description(draft: dict[str, Any]) -> str | None:
    """Why it is worth recommending, in the author's OWN words — the italic line on the
    card. The trait plus any corrections they added, and nothing generated: a description
    Lana wrote would be Lana vouching for a place she has never been to."""
    parts = [str(draft.get("trait") or "").strip()]
    parts += [str(d).strip() for d in (draft.get("details") or []) if str(d).strip()]
    return " · ".join([p for p in parts if p]) or None


# The community step. Deterministic, appended after the generated set the way the hosting
# flow puts "For one of your communities?" on its setup card — a destination is not one of
# the facets Lana writes about the place, and it must never be invented.
COMMUNITY_FIELD = "community"


def my_communities(user_jwt: str) -> list[dict[str, str]]:
    """[{place_id, name}] for the communities the caller belongs to. [] on any failure —
    the step is then simply absent, which is also the right answer for a user with none."""
    from app.auth import jwt_user_id

    user_id = jwt_user_id(user_jwt)
    if not user_id:
        return []
    try:
        from app.circles_flow import list_my_circles

        rows = list_my_circles(user_id)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("tip_my_communities_failed")
        return []
    out: list[dict[str, str]] = []
    for c in rows or []:
        pid = str(c.get("place_id") or c.get("place_ref") or "").strip()
        name = str(c.get("place_name") or c.get("name") or "").strip()
        if pid and name and not any(o["place_id"] == pid for o in out):
            out.append({"place_id": pid, "name": name})
    return out[:8]


def _community_step(communities: list[dict[str, str]]) -> dict[str, Any]:
    """Optional and last-but-one: sharing with the whole area is the default, and picking a
    community NARROWS who sees it — never a required gate on posting."""
    return {
        "field": COMMUNITY_FIELD,
        "label": "Community",
        "question": "Is this for one of your communities?",
        "kind": "community",
        "options": [c["name"] for c in communities] + ["Everyone nearby"],
        # Ids beside the labels, same order, "" for the opt-out. Two communities can share
        # a name — a user belongs to two gyms both called "Life Time" — and a name-matched
        # answer would always resolve to the first, making the second unpickable. The
        # carousel posts the id; only the chat fork is left matching on names.
        "option_ids": [c["place_id"] for c in communities] + [""],
        "required": False,
    }


def resolve_community(
    draft: dict[str, Any], *, user_jwt: str, session_ctx: dict[str, Any]
) -> None:
    """Settle `circle_place_id` from whatever the user gave us, in priority order: the id
    the carousel posted, the name they answered the step with, else the community selected
    at the top of the app. "Everyone nearby" is a real answer — it clears the pick."""
    from app.community_scope import active_community, active_community_id

    # Order matters and it is not "first value wins": the header pre-fill lands turns
    # before the step is asked, so short-circuiting on a set value made the pre-fill
    # unoverridable — a user who answered "Everyone nearby" still shared into the
    # community that happened to be selected (caught by scripts/try_reco_carousel.py).
    # The user's own answer is authoritative; the pre-fill only fills a silence.
    answer = str((draft.get("answers") or {}).get(COMMUNITY_FIELD) or "").strip()
    # A fresh answer naming something OTHER than the pick on the draft is a correction and
    # outranks it — otherwise a carousel submit would freeze the choice against every later
    # "actually, make it Fitness CF".
    if answer and answer.casefold() != str(draft.get("circle_name") or "").casefold():
        draft["circle_picked"] = False
    # An id posted straight to /tip-setup is exact where a name is not, so it wins: it is
    # the only way to tell two same-named communities apart.
    if draft.get("circle_picked"):
        return
    if answer:
        if answer.lower() in ("everyone nearby", "everyone", "no", "none", "skip"):
            draft["circle_place_id"] = None
            draft["circle_name"] = None
            return
        for c in (session_ctx.get("tip_communities") or my_communities(user_jwt)):
            if str(c.get("name") or "").strip().lower() == answer.lower():
                draft["circle_place_id"] = c["place_id"]
                draft["circle_name"] = c["name"]
                return
    # Nothing said → whatever the app header is scoped to, same pre-fill the hosting
    # setup card uses.
    if active_community_id(session_ctx):
        draft["circle_place_id"] = active_community_id(session_ctx)
        draft["circle_name"] = (active_community(session_ctx) or {}).get("name")


def _save_tip(
    *, draft: dict[str, Any], user_jwt: str, block_id: str | None, zip_code: str | None
) -> tuple[dict[str, Any] | None, str]:
    """(saved_row, error_detail). The reason comes back so the caller can recover from the
    one failure that is fixable in-turn (block_required) instead of just apologising."""
    try:
        from app.local_signals import save_local_signal, tag_local_signal
        from app.tip_tags import tags_for_tip

        saved = save_local_signal(
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
            # Ask-shaped tags, generated once here. The ask side has always been
            # normalized by the classifier ("trim my beard" -> "barber"); this is the
            # same normalization for the tip, so both sides finally speak one vocabulary.
            # Best-effort: [] leaves the tip exactly as findable as it was before.
            affinity_tags=tags_for_tip(
                name=str(draft.get("name") or "").strip() or None,
                category=str(draft.get("category") or "").strip() or None,
                description=_description(draft),
                details=[
                    f"{f.get('label')}: {f.get('answer')}"
                    for f in (_reco_fields(draft) or [])
                    if isinstance(f, dict) and f.get("label") and f.get("answer")
                ],
            ),
        )
        # Tagged AFTER the insert rather than through save_local_signal, which is 150 lines
        # of dedupe/match/notify: threading one column through it is how a behaviour goes
        # missing in a copy-paste. Best-effort — an untagged tip is still a posted tip.
        place_id = str(draft.get("circle_place_id") or "").strip()
        if place_id and (saved or {}).get("id"):
            tag_local_signal(user_jwt, signal_id=str(saved["id"]), place_id=place_id)
        return saved, ""
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


# The one chip Lana adds when explaining herself. Matched exactly, like the community
# step's "Everyone nearby" opt-out — an offered label, not an intent guess.
_SKIP_CHIP = "Skip that one"


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
        # "why are u asking this? why should i tell?" is a question ABOUT the step Lana
        # just asked, and releasing on it reset the whole capture: the half-built card
        # vanished, the user was offered "help with something else", and the recommendation
        # they had already named was gone (dev QA 2026-09-08). A capture with a question
        # outstanding owns the answer to "why are you asking" — every other meta turn still
        # releases, which is what the goal=chat read is for.
        return bool(_pending_step(session_ctx))
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
    slots: dict[str, Any] | None = None,
) -> str:
    """Drive one tip-share capture turn. Mutates session_ctx (tip_draft,
    tip_share_active, tip_listed_now, routing_phase). Returns Lana's reply.

    `slots` is the classifier's read of THIS turn, needed for one thing: telling a question
    about the question ("why are you asking this?") apart from an answer to it. Without it
    the meta turn was stamped in as the answer."""
    from app.discovery_route import resolve_block_id
    from app.reco_question_sets import SUBJECT_FIELD, TAIL_FIELDS

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

    # The step Lana asked, captured BEFORE the stamp below clears it — the meta branch
    # further down needs to know what was on screen.
    from app.lane_decision import is_meta_or_chat

    pending_step = _pending_step(session_ctx)

    # ── "Skip that one" → the step is offered, declined, and never asked again ──
    if pending_step and msg.casefold() == _SKIP_CHIP.casefold():
        asked = set(session_ctx.get("tip_asked_fields") or [])
        asked.add(pending_step["field"])
        session_ctx["tip_asked_fields"] = list(asked)
        session_ctx["tip_pending_ask"] = None
        session_ctx["tip_pending_question"] = None
        msg = ""

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
    weak: dict[str, Any] | None = None
    role = ""
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
        weak = found.pop("weak_answer", None)
        role = str(found.pop("reply_role", "") or "")
        for k, v in found.items():
            draft[k] = v
        if merged_answers:
            draft["answers"] = merged_answers

    # ── "why are you asking this?" — answer it, keep the card ──
    #
    # Lana's own question is on screen, so she owes an answer to why it is worth asking.
    # The step stays open with its chips still under it: the user can tap one the moment
    # they are satisfied, which a released lane made impossible.
    if (
        pending_step
        and msg
        # The extractor's read comes first: it is the only one of the two holding the
        # question that was on screen. The classifier's `goal=chat` is kept as a second
        # opinion because it fires on the turn a capture is still warming up, but it is NOT
        # reliable mid-capture — it read "why should i answer this" as an answer and the
        # question went onto the card as the user's opinion (dev QA 2026-09-08).
        and (role == "asks_why" or (not role and is_meta_or_chat(slots)))
        and not _PASS_RE.search(msg)
    ):
        skippable = not pending_step.get("required")
        # Their question was stamped in as the answer a few lines up. Take it back out.
        draft["answers"] = {
            k: v
            for k, v in (draft.get("answers") or {}).items()
            if k != pending_step["field"]
        }
        session_ctx["tip_pending_ask"] = pending_step["field"]
        session_ctx["tip_pending_question"] = pending_step["question"]
        # The card reads this to know which row is live; without it the row Lana is
        # explaining is not the row the card highlights.
        draft["pending_field"] = pending_step["field"]
        draft["chips"] = _build_chips(draft)
        draft["suggestions"] = list(pending_step.get("options") or []) + (
            [_SKIP_CHIP] if skippable else []
        )
        session_ctx["tip_draft"] = draft
        session_ctx["tip_share_active"] = True
        session_ctx["routing_phase"] = "listening"
        return compose_reply(
            goal=(
                "The user is asking why you want this, or whether they have to say. Answer "
                "in one or two short lines: say plainly what a neighbour reading the card "
                "would do with this answer, and that they never have to give it. "
                + (
                    f"Then offer to skip it — keep **{_SKIP_CHIP}** verbatim and bolded — "
                    "or to just answer it."
                    if skippable
                    else "This one the card cannot be finished without, so say that gently "
                    "and ask it once more."
                )
            ),
            facts=[
                f"The question on screen: {pending_step['question']}",
                f"The recommendation: {_summary(draft)}",
                "Nothing they say here is shown to anyone until they tap Pass the tip along",
                ("This question is optional" if skippable else "This question is required"),
            ],
            fallback=(
                f"It just helps a neighbour know if it's for them — but skip it if you like. "
                f"**{_SKIP_CHIP}**, or tell me: {pending_step['question']}"
                if skippable
                else f"I can't finish the card without it, but nothing's shared until you "
                f"say so — {pending_step['question']}"
            ),
        )

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

    # ── One nudge on an answer that does not answer its question ──
    #
    # The other half of asking well: "hundred dollars" to "what is the price range?" was
    # stored and rendered as a fact (dev QA 2026-09-07). Free to detect — the extractor
    # already sees every step's question and the answer that just landed, so it only had
    # to be asked for an opinion.
    #
    # ONE nudge per field, then the answer stands whatever it says: this is a neighbour
    # doing us a favour, not a form validator. `tip_reasked_fields` is what makes it one —
    # without it a stubborn "good" and a stubborn Lana loop forever.
    # ponytail: chat fork only. A carousel bulk submit is the fast path by choice; add the
    # same check to /tip-setup if QA shows junk arriving that way too.
    weak_field = str((weak or {}).get("field") or "").strip()
    reasked = set(session_ctx.get("tip_reasked_fields") or [])
    if (
        weak_field
        and draft.get("step_set")
        and weak_field not in reasked
        and weak_field not in (SUBJECT_FIELD, COMMUNITY_FIELD, *TAIL_FIELDS)
        and not _PASS_RE.search(msg)
    ):
        step = next(
            (s for s in step_set_of(draft) if s["field"] == weak_field), None
        )
        answer_now = str((draft.get("answers") or {}).get(weak_field) or "").strip()
        # Never argue with our own chip. If the answer IS one of the options Lana offered,
        # it is valid by construction — your log had a nudge on a `choice` step (dev QA
        # 2026-09-08), which is Lana telling a user the answer she gave them is wrong.
        if step and answer_now and not _all_offered(answer_now, (step or {}).get("options")):
            draft["answers"] = {
                k: v for k, v in (draft.get("answers") or {}).items() if k != weak_field
            }
            reasked.add(weak_field)
            session_ctx["tip_reasked_fields"] = list(reasked)
            # Off `asked` as well, or the walk would skip the step we are re-opening.
            session_ctx["tip_asked_fields"] = [
                f for f in (session_ctx.get("tip_asked_fields") or []) if f != weak_field
            ]
            session_ctx["tip_pending_ask"] = weak_field
            draft["pending_field"] = weak_field
            draft["chips"] = _build_chips(draft)
            # The chips stay up, plus a way out: two off-target answers in a row means the
            # question is not landing, and a third ask is worse than no answer.
            draft["suggestions"] = list(step.get("options") or []) + (
                [] if step.get("required") else [_SKIP_CHIP]
            )
            session_ctx["tip_draft"] = draft
            session_ctx["tip_share_active"] = True
            session_ctx["tip_pending_question"] = step["question"]
            session_ctx["routing_phase"] = "listening"
            # Watch the RATE, not the line: a nudge means a question got an answer it could
            # not use, so a rising rate is the ask side regressing, not users getting
            # sloppier.
            logging.getLogger(__name__).info(
                "tip_answer_nudged field=%s kind=%s why=%r",
                weak_field, step.get("kind"), (weak or {}).get("why"),
            )
            return compose_reply(
                goal=(
                    "REACT to what the user just said first — one short clause, in your own "
                    "voice, that shows you actually read it (agree, laugh, sympathise, "
                    "whatever fits). Then ask your question again, because their answer "
                    "does not fit it. Warm and light, never scolding, two short lines at "
                    "most. Re-asking without acknowledging what they said reads like a form "
                    "rather than a neighbour. Do not mention validation, fields or forms, "
                    "and do not ask anything else."
                ),
                facts=[
                    f"The question still open: {step['question']}",
                    f"What they just said: {msg}",
                    f"Why it does not answer it: {(weak or {}).get('why') or 'it does not answer the question'}",
                ]
                + (
                    []
                    if step.get("required")
                    else [f"They can skip this one with **{_SKIP_CHIP}**"]
                ),
                fallback=str(step["question"]),
            )

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
        lead = readback(session_ctx, "tip_readback", draft.get("draft_id"), _summary(draft))
        return f"{lead}What kind of recommendation is it?"

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
    # ── The question set is written ONCE, here, and NOT UNTIL THE SUBJECT IS KNOWN.
    #
    # The name gate above passes on a category alone ("a stationery shop near me"), so the
    # set used to be written about a stationery shop in general — and it showed: every
    # question said "this stationery shop", and with nothing but a category to go on the
    # model reached for facets that fit any shop on earth ("what's parking like?"). Waiting
    # for the subject is what makes them about The Paper Store (dev QA 2026-09-08).
    #
    # Nothing is needed to bridge the gap: with no step_set, `step_set_of` falls back to the
    # type's static set, which is head-first — so the turn before this asks the subject
    # step, which is exactly the answer this build is waiting for.
    #
    # `steps_raw` is whatever the extractor proposed; validate_steps is what makes it
    # askable, and falls back to the type's static set when there is nothing usable. ──
    if reco_type and _has(draft, "name") and not draft.get("step_set"):
        built = validate_steps(
            draft.pop("steps_raw", None),
            reco_type,
            tallies=_reco_tallies(
                user_jwt=user_jwt, block_id=block_id, name=draft.get("name")
            ),
            place_based=bool(draft.get("place_based")),
            subject_hint=draft.get("category"),
        )
        # Where it goes is a step like any other, so both forks get it for free — the
        # carousel renders one more card, the chat fork asks one more question. Absent for
        # a user with no communities: there is nothing to choose between.
        communities = my_communities(user_jwt)
        if communities:
            session_ctx["tip_communities"] = communities
            built = list(built) + [_community_step(communities)]
        draft["step_set"] = built
    step_set = step_set_of(draft) if reco_type else []
    # Written for THIS subject, or still the type's generic table? The cards fork is a
    # whole set at once, so opening it on the static table means eight generic text boxes
    # while the chat fork would have asked about this barber shop by name — the client
    # holds the fork back until this is true (dev QA 2026-09-08).
    draft["tailored"] = bool(draft.get("step_set"))
    resolve_community(draft, user_jwt=user_jwt, session_ctx=session_ctx)
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
            # One list, three cases, in order:
            #   community — nothing. It has its own select in chat, and eight communities
            #     as eight full-width chips is a wall, not a question (dev QA 2026-09-08).
            #   options    — the taps Lana wrote for this question.
            #   place      — real nearby places, since a generated set writes no options
            #     for a map step and chat has no picker of its own to fall back on.
            # Plus a way out on anything optional. Deliberately not `a + b or c`: once a
            # skip chip is in the list the left side is never falsy, which silently ate the
            # nearby places for every optional map step.
            picks = list(step.get("options") or [])
            if not picks and step.get("kind") == "place":
                picks = _name_suggestions(
                    draft, zip_code=zip_code, block_id=block_id, user_jwt=user_jwt
                )
            if step.get("kind") == "community":
                picks = []
            elif not step.get("required"):
                picks = [*picks, _SKIP_CHIP]
            draft["suggestions"] = picks
            session_ctx["tip_draft"] = draft
            session_ctx["tip_share_active"] = True
            session_ctx["tip_pending_question"] = step["question"]
            session_ctx["routing_phase"] = "listening"
            answered = sum(1 for s in steps if s.get("answer"))
            lead = readback(session_ctx, "tip_readback", draft.get("draft_id"), _summary(draft))
            return (
                f"{lead}{step['question']} "
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
        lead = readback(session_ctx, "tip_readback", draft.get("draft_id"), _summary(draft))
        return f"{lead}{ask['question']}"

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
    # Naming the destination is not decoration: a tagged tip is invisible to the area, so
    # "shared with CF Fitness" is the difference between the neighbourhood seeing it and
    # not. Untagged keeps the old wording.
    circle = str(draft.get("circle_name") or "").strip()
    return compose_reply(
        goal=(
            "The tip draft is complete and shown as a card. Tell the user you'll pass it on when "
            + (
                f"someone at {circle} asks — and that it goes to {circle} only, not to the "
                "wider neighborhood. "
                if circle
                else "a neighbor asks, "
            )
            + "and prompt them to tap **Pass the tip along** (keep that button "
            "name verbatim, bolded) to post it, or send it to a neighbor they know."
        ),
        facts=[f"Tip ready: {summary}"]
        + ([f"Shared with the community: {circle} (and only there)"] if circle else []),
        fallback=(
            f"Got it — **{summary}**. I'll pass it on when a neighbor asks. "
            + (
                f"**Pass the tip along** to post it for {circle} — it stays inside {circle}."
                if circle
                else "**Pass the tip along** to post it for your neighbors, or send it to "
                "a neighbor you know."
            )
        ),
    )
