"""
checks.py — MECHANICAL axes for the recommendation-quality eval.

Everything here has ground truth known by construction — a banned phrase, a floor field, a
byte-exact consent string, a field key that must be ASCII, a hand-labelled answer — so it is
checked in code, not by an LLM. The genuinely subjective questions ("would a neighbour filter
on this?", "could only someone who went answer it?") live in judge.py.

VERDICT POLICY, AND WHY IT IS NOT UNIFORM
-----------------------------------------
HARD_FAIL is reserved for rules the product states as ABSOLUTE and already enforces:

  §6's banned questions          the shipped instruction set Lana is given
  §6's privacy floor             enforced twice — in the prompt AND as _BLOCKED_ASK
  §5's consent wording           "Lana is never allowed to reword it"
  §5's floor                     validate_steps inserts missing floor fields itself
  §9's English field keys        they are storage keys; a translated key orphans the answer

SOFT_FAIL is for rules that are PROPOSED but not shipped, and for structural expectations a
defensible alternative could miss. The important member of that set:

  google_answerable (§12.1) is SOFT_FAIL, not HARD_FAIL. §12 is titled "What we could do
  about it" and marks this option "Already prototyped, not shipped". Scoring an unshipped
  proposal as a hard violation would put the PR gate permanently red against behaviour
  nobody has agreed to yet — and a permanently red gate is an ignored gate. It is still
  reported per-fixture, and it is the axis that carries the Barnes & Noble finding.

FALSE POSITIVES ARE THE EXPENSIVE FAILURE HERE
----------------------------------------------
This suite has shipped a false HARD_FAIL against Lana before (an empty utterance is legal for
kind=handoff; our check did not know). On that run our false positive was louder than the real
defect next to it. So every pattern below is anchored deliberately tight:

  * "what did you like about it/them" is banned; "what did you like about the crust" is not —
    the ban in §6 is about a question with a PRONOUN object, which produces answers of
    different shapes that cannot be compared. A specific one is a good question.
  * google_answerable NEVER fires on a type's own floor field. A pediatrician is lookup-able
    and "how do neighbours reach her?" is still required (§5). Banning it would be a false
    positive against behaviour the product mandates.
  * google_answerable never fires on a `kind=place` step: that is the map picker, which is how
    §5 says an address SHOULD be captured, not a question about a street address.
  * "at least three filterable questions" (§6) is NOT mechanical. Whether a neighbour would
    filter on something needs judgment, and CLAUDE.md is explicit that a check whose ground
    truth needs judgment does not belong in code. The mechanical half is `tappable_answers`
    (§6's "give tappable answers whenever the answer is a small set", which IS structural);
    the rest is judge.py's `filterable`.

Every check here has a planted-violation case in selftest.py. A check with no selftest case is
a check nobody has proven fires.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from ports import (
    CONSENT_QUESTION,
    TAIL_FIELDS,
    AnswerDecision,
    AnswerFixture,
    CheckResult,
    GeneratedSet,
    QuestionFixture,
)

# ---------------------------------------------------------------------------
# §6 — "Don't ask" (the banned table, verbatim)
# ---------------------------------------------------------------------------
#
# | "What stood out?"                | every answer is a different shape, none comparable |
# | "What did you like about it?"    | same                                               |
# | "Why is it good?"                | the neighbor already said this in their opening    |
# | "Anything else?" / "Tell me more"| filler                                             |
#
# Anchored on the PRONOUN object, not the verb: the defect is a question whose answers cannot
# be compared across recommendations, and that is what an unqualified object produces. Each
# pattern was checked against the static sets in reco_question_sets.py, which is where these
# phrasings actually live today — "What stood out for you?" and "What did you like about
# them?" are both in `_SETS`, so a set that falls back to static WILL trip this. That is the
# intended signal, not a bug in the check.
_BANNED_GENERIC: tuple[tuple[str, str], ...] = (
    (r"what\s+stood\s+out", "§6: answers are all different shapes, none comparable"),
    # ANCHORED TO END-OF-QUESTION, and that anchor is load-bearing. `her`, `this` and `that`
    # are DETERMINERS as well as pronouns, so a trailing `\b` banned every fully-qualified
    # noun phrase that happened to start with one: "What did you like about her chairside
    # manner with anxious kids?" HARD_FAILed. Worse, the verdict was gender-dependent — `his`
    # is not in the alternation, so "about his approach" PASSed while "about her approach"
    # failed. Both contradict this file's own stated policy (a specific object is a good
    # question) and would have reddened the gate on a feminine-referent subject the moment
    # generation started working. Found by an adversarial review of the criteria, 2026-09-08.
    (
        r"what\s+(?:did|do)\s+you\s+like\s+about\s+"
        r"(?:it|them|him|her|this|that|the\s+place)\s*[?.!]?\s*$",
        "§6: unqualified object — same problem as 'what stood out'",
    ),
    (
        r"why\s+(?:is|are|was|were)\s+(?:it|they|he|she|this|that)\s+(?:so\s+)?"
        r"(?:good|great|special|worth\s+it|better|the\s+best)\b",
        "§6: the neighbour already said this in their opening line",
    ),
    (r"anything\s+else\s*\?", "§6: filler"),
    (r"\btell\s+me\s+more\b", "§6: filler"),
    (r"^\s*(?:any)?\s*other\s+thoughts\s*\?", "§6: filler"),
)

# ---------------------------------------------------------------------------
# §6 last row / §12.1 — the privacy floor
# ---------------------------------------------------------------------------
#
# "past this line it stops being a recommendation and becomes someone's private data, posted
# to a whole neighborhood by someone who isn't them."
#
# DELIBERATELY WIDER than app/reco_question_sets.py `_BLOCKED_ASK`. The product's regex is a
# last-resort filter that throws the question away; this is an eval, and its job is to report
# the ones the filter would MISS. "Which house is she at?" carries the same disclosure as
# "what's her home address?" and the shipped pattern matches only the second. A hit here on a
# phrasing `_BLOCKED_ASK` does not cover is the finding — the report says which.
_PRIVACY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"home\s+address|what'?s?\s+(?:their|her|his)\s+address|where\s+do(?:es)?\s+(?:she|he|they)\s+live",
     "home address"),
    # `which house` needs the verb: the bare form matched inside "which HOUSEhold tasks does
    # she take on?" — the single most natural question a good set would write for a nanny, on
    # the one fixture whose whole purpose is privacy. It HARD_FAILed twice over (once here,
    # once via the fixture's own needle) and was reported as "asks for where she lives".
    (r"which\s+house\s+(?:is|was|does|do|did|are|were)\b|"
     r"what\s+street\s+do(?:es)?\s+(?:she|he|they)\s+live|house\s+number",
     "home location by another name"),
    (r"full\s+name|last\s+name|surname|maiden\s+name", "full name"),
    # `birthday` needs a possessive: bare, it flagged "Do they host birthday parties?", which
    # is an ordinary filterable question about a venue. (The product's own _BLOCKED_ASK would
    # drop that step upstream, so this only ever mattered for hand-built sets — but a check
    # that reports a false privacy violation is wrong regardless of who sees it.)
    (r"date\s+of\s+birth|\b(?:her|his|their|your)\s+birthday\b|"
     r"when\s+(?:is|was)\s+(?:her|his|their|your)\s+birthday|"
     r"how\s+old\s+(?:is|are)\s+(?:she|he|they)\b", "date of birth / age"),
    # `passport` needs context too — "Do they do passport photos while you wait?" is an
    # ordinary, filterable question about a business, and the product's own _BLOCKED_ASK does
    # not contain the word, so it reaches this check on live runs.
    (r"social\s+security|\bssn\b|license\s+plate|credit\s+card|"
     r"passport\s+(?:number|no\.?|details|copy|scan)|\b(?:her|his|their|your)\s+passport\b",
     "government / financial id"),
    (r"how\s+much\s+(?:do|did)\s+(?:you|they|she|he)\s+(?:earn|make)|what'?s?\s+(?:their|her|his)\s+salary",
     "income"),
    (r"\bpassword\b", "credentials"),
)

# ---------------------------------------------------------------------------
# §12.1 — what Google already knows
# ---------------------------------------------------------------------------
#
# "When the subject is lookup-able — a shop, a restaurant, a business — ban questions about
# opening hours, phone, website, address and price band, and push Lana toward what only a
# visitor knows."
#
# Both halves of a hit have to be true: the fixture says the subject is lookup_able, AND the
# question is about one of these five. Everything else — a product's price, a recipe's time, a
# tradesman's phone number — is genuinely neighbour knowledge and is not touched.
_GOOGLE_TOPICS: tuple[tuple[str, str, str], ...] = (
    # `is`/`was` are in the verb alternation because of a real miss: the static `location`
    # set asks "When is it open — and the best time to go?" and an earlier, narrower pattern
    # (do|does|are only) walked straight past it. That question is §12.1's opening-hours case
    # verbatim, so the check was silently vacuous on the one type most likely to hit it.
    # `hours are|do` carries a subject, because the context-free form caught pure visitor
    # knowledge: "Which hours are busiest?" and "How many hours does the loop take?" were both
    # reported as "opening hours are on the listing". q05's own probes call best-time-to-go
    # visitor knowledge, so the check was contradicting the fixture it ran on.
    ("hours",
     r"open(?:ing)?\s+hours\b|\bhours\s+(?:are|do)\s+(?:they|it|she|he)\b|"
     r"what\s+(?:time|hours)\s+(?:do|does|is|are)\s+(?:they|it|she|he)\s+(?:open|close)|"
     r"when\s+(?:do|does|is|are|was|were)\s+(?:it|they|she|he)\s+open|"
     r"what\s+are\s+(?:their|its)\s+hours",
     "opening hours are on the listing"),
    # `number` needs a negative lookahead: unanchored, it matched "what's their number one
    # dish?" and "what's the number of tables?" and reported them as asking for the phone.
    ("phone",
     r"phone\s+number|what'?s?\s+(?:their|its|the)\s+number(?!\s+(?:one|of)\b)|"
     r"contact\s+number|telephone",
     "the phone number is on the listing"),
    ("website",
     r"\bwebsite\b|\bweb\s+site\b|\bthe(?:ir)?\s+url\b|do\s+they\s+have\s+a\s+site\b",
     "the website is on the listing"),
    ("address",
     r"what'?s?\s+(?:their|its|the)\s+address|street\s+address|which\s+address",
     "the address is on the listing — §5 says pin it on the map instead"),
    # The old trailing `roughly\s+what\s+does` had nothing tying it to price, so ANY question
    # opening with that phrase was reported as asking the price band — including the
    # visitor-knowledge questions §3 lists as good ones. The replacement keeps every real hit
    # and additionally catches "Roughly what did it cost?" (reco_question_sets' own wording).
    ("price_band",
     r"price\s+range|what'?s?\s+the\s+price\b|how\s+(?:expensive|pricey)|price\s+point|"
     r"what\s+does\s+(?:it|a\s+meal)\s+(?:cost|run)|"
     r"roughly\s+what\s+(?:do|does|did)\s+(?:\w+\s+){0,3}?(?:cost|run|set\s+you\s+back)\b",
     "the price band is on the listing — §6: ask what only a visitor knows"),
)

# The floor per type, MIRRORED from app/reco_question_sets.py `_FLOOR`. Mirrored rather than
# imported so the check is independent of the thing it checks: importing it would make
# check_floor_present compare the product to itself, which passes under any change including
# a floor that has been emptied. check_floor_present asserts the two still agree and reports
# a drift as UNSCORED — "the eval is out of date" is a fact about us, never a finding against
# Lana.
_FLOOR_MIRROR: dict[str, tuple[str, ...]] = {
    "professional": ("profession", "helped_with", "contact"),
    "service": ("service", "helped_with", "contact"),
    "restaurant": ("dish", "where"),
    "recipe": ("recipe", "ingredients"),
    "product": ("used_for", "where_to_buy"),
    "location": ("known_for", "where"),
    "diy": ("fixes", "how"),
    # Added 2026-09-17 after Tim's pull merged the shipped 'other' type
    # (app/reco_question_sets.py:424) — a typeless recommendation, GOOGLE_UNSEARCHABLE by
    # design (line 290) and meant as a last resort behind the other six. The drift tripwire
    # caught the mirror going stale on the very next selftest run after the merge.
    "other": ("helps_with", "where_to_look"),
}

# _STEPS_SPEC asks for 4-8 (tip_share.py:78); reco_question_sets caps the middle at
# _MAX_MIDDLE = 10 after the floor is inserted. Below 4 is an interrogation that tells a
# reader nothing; above 10 cannot happen without the cap having failed.
_MIN_MIDDLE, _MAX_MIDDLE = 4, 10

# ---------------------------------------------------------------------------
# The subject step — §5's first question, shipped 2026-09-10
# ---------------------------------------------------------------------------
#
# MIRRORS reco_question_sets.py:148-188. `location` and `restaurant` subjects are always a map
# point; `professional` and `service` are only when the extractor's `place_based` says so — a
# barber shop is walked into, a plumber has no storefront. When the subject IS the pin, the
# set's own place step is dropped as a duplicate, so the place floor field is satisfied by the
# subject step instead of by its own row.
SUBJECT_FIELD = "subject"
_PLACE_SUBJECT_TYPES = frozenset({"location", "restaurant"})
_PLACE_CAPABLE_TYPES = frozenset({"professional", "service"})
# Floor fields the subject step can stand in for when it carries the pin.
_PLACE_FLOOR_FIELDS = frozenset({"where"})


def _subject_is_place(rtype: str, gen: GeneratedSet) -> bool:
    """Mirror of reco_question_sets.subject_is_place. `place_based` is the extractor's read,
    which for the two capable types is the only thing separating a shop from a sole trader."""
    if rtype in _PLACE_SUBJECT_TYPES:
        return True
    if rtype not in _PLACE_CAPABLE_TYPES:
        return False
    step = next((s for s in gen.steps if str(s.get("field")) == SUBJECT_FIELD), None)
    # The generated subject step renders as the Places picker exactly when place_based held.
    return bool(step and step.get("kind") == "place")

_SPANISH_MARKERS = re.compile(
    r"[¿¡áéíóúñü]|\b(?:qué|que|cómo|como|cuál|cual|dónde|donde|cuánto|cuanto|quién|quien|"
    r"para|con|los|las|una|del)\b",
    re.IGNORECASE,
)
_PORTUGUESE_MARKERS = re.compile(
    r"[ãõçáéíóúâêô]|\b(?:qual|como|onde|quanto|quem|para|com|dos|das|uma|você|voce)\b",
    re.IGNORECASE,
)
_LANG_MARKERS = {"spanish": _SPANISH_MARKERS, "portuguese": _PORTUGUESE_MARKERS}


def _norm(text: Any) -> str:
    return " ".join(str(text or "").lower().split())


def _questions(gen: GeneratedSet) -> list[tuple[int, dict[str, Any], str]]:
    """(index, step, normalized question) for the model's half of the set only."""
    return [(i, s, _norm(s.get("question"))) for i, s in enumerate(gen.middle())]


# ===========================================================================
# ARM A — question quality
# ===========================================================================

def check_banned_generic(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§6's banned table. HARD_FAIL — this is the shipped instruction set, not a proposal."""
    hits: list[str] = []
    for i, step, q in _questions(gen):
        for pattern, why in _BANNED_GENERIC:
            if re.search(pattern, q):
                hits.append(f"step[{i}] {step.get('field')!r}: {step.get('question')!r} — {why}")
                break
    if hits:
        return CheckResult("banned_generic", "HARD_FAIL", "; ".join(hits))
    return CheckResult("banned_generic", "PASS", f"{len(gen.middle())} questions, none on §6's banned list")


def check_privacy_floor(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§6's last row plus this fixture's own baited topics. HARD_FAIL — the product enforces
    this twice and the doc says instructions alone are not a guarantee."""
    hits: list[str] = []
    for i, step, q in _questions(gen):
        for pattern, label in _PRIVACY_PATTERNS:
            if re.search(pattern, q):
                hits.append(f"step[{i}]: {step.get('question')!r} asks for {label}")
                break
        # Word-boundary regex, not `m in q`. The plain substring form fired the needle
        # "what street" inside longer words and, paired with the unanchored `which house`
        # pattern above, reported ONE innocent question as TWO privacy violations — inflating
        # the count in the report detail, since `sorted(set(...))` dedupes only exact strings.
        for topic in fx.forbidden_topics:
            if any(re.search(rf"\b{re.escape(m)}\b", q)
                   for m in (str(x).lower() for x in topic.get("question_any_of") or [])):
                hits.append(
                    f"step[{i}]: {step.get('question')!r} — fixture-forbidden "
                    f"({topic.get('about')})"
                )
    if hits:
        return CheckResult("privacy_floor", "HARD_FAIL", "; ".join(sorted(set(hits))))
    return CheckResult("privacy_floor", "PASS", "no private ask in the generated set")


def check_google_answerable(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§12.1. SOFT_FAIL by design — see the verdict policy at the top of this file.

    Two exemptions, both load-bearing against false positives:
      * a type's own FLOOR field. §5 requires "how do neighbours reach her?" for a
        pediatrician, who is also lookup-able. The floor wins.
      * kind == "place". That step is the map picker, which is §5's PREFERRED way to capture
        a location — flagging it would penalise the product for doing the right thing.
    """
    if not fx.lookup_able:
        return CheckResult(
            "google_answerable", "PASS",
            "subject is not lookup-able (a person by referral / a recipe / a product) — "
            "price and where-to-buy are neighbour knowledge here, so §12.1 does not apply",
        )
    floor = set(_FLOOR_MIRROR.get(str(gen.reco_type or ""), ()))
    hits: list[str] = []
    for i, step, q in _questions(gen):
        fname = str(step.get("field") or "")
        if fname in floor:
            continue
        if step.get("kind") == "place":
            continue
        for topic, pattern, why in _GOOGLE_TOPICS:
            if re.search(pattern, q):
                hits.append(f"step[{i}] {fname!r} [{topic}]: {step.get('question')!r} — {why}")
                break
    if hits:
        return CheckResult(
            "google_answerable", "SOFT_FAIL",
            f"{len(hits)} of {len(gen.middle())} question(s) ask what the listing already "
            f"says: " + "; ".join(hits),
        )
    return CheckResult("google_answerable", "PASS",
                       "lookup-able subject, nothing asked that Google already answers")


def check_floor_present(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§5's floor: the fields a reader cannot act on the recommendation without.

    Also checks POSITION. §5 is explicit that where a repaired floor field lands matters —
    "it goes near the top with the other basics, never at the end — because a user can say
    'that's it, post it' at any point, and anything sitting at the end never gets asked."
    """
    rtype = str(gen.reco_type or "")
    if not rtype:
        return CheckResult("floor_present", "UNSCORED", "no reco_type — nothing to hold a floor against")

    # Drift guard. If the product's floor has moved, say so instead of scoring against a
    # stale mirror: an out-of-date eval is a fact about us, never a finding against Lana.
    try:
        from app.reco_question_sets import _FLOOR as _REAL_FLOOR  # type: ignore[attr-defined]
        real = tuple(_REAL_FLOOR.get(rtype, ()))
        if real and real != _FLOOR_MIRROR.get(rtype, ()):
            return CheckResult(
                "floor_present", "UNSCORED",
                f"floor for {rtype!r} has moved in the product ({real}) vs this harness's "
                f"mirror ({_FLOOR_MIRROR.get(rtype)}). Update checks._FLOOR_MIRROR.",
            )
    except Exception:  # noqa: BLE001 — the mirror still works without the import
        pass

    floor = _FLOOR_MIRROR.get(rtype, ())
    middle = gen.middle()
    present = {str(s.get("field")) for s in middle}

    # THE SUBJECT STEP CAN SATISFY A PLACE FLOOR FIELD, and missing this produced a false
    # HARD_FAIL on every restaurant and location fixture (2026-09-11).
    #
    # Asjid shipped §5's first question as a real step (`SUBJECT_FIELD = "subject"`,
    # reco_question_sets.py:148). When the subject IS a map point, `validate_steps` passes
    # `drop_place=subject_is_place(...)` and removes the set's own "Where is it?" — because
    # the pin is already on the subject step and asking twice is the duplication that gate
    # existed to cause. So `where` legitimately disappears from a restaurant set, and the
    # floor is still met.
    #
    # Mirrored rather than imported, like _FLOOR_MIRROR, with the same drift guard above.
    if SUBJECT_FIELD in present and _subject_is_place(rtype, gen):
        present |= _PLACE_FLOOR_FIELDS

    missing = [f for f in floor if f not in present]
    if missing:
        return CheckResult(
            "floor_present", "HARD_FAIL",
            f"{rtype}: floor field(s) {missing} absent from the final set. validate_steps "
            f"inserts missing floor fields itself (reco_question_sets.py), so this means the "
            f"guard did not run or the type's floor is empty.",
        )
    # "Never at the end": the last third of the set is where an early "that's it, post it"
    # truncates. A floor field there is asked late or never.
    # POSITION IS ONLY SCORED FOR THE OPTIONAL FLOOR FIELD. `required` is the first two floor
    # fields (reco_question_sets.py:367-368), and those keep coming back until answered
    # (next_question, :163-177) — where they SIT is irrelevant, they cannot be skipped. Only
    # the third floor field (`contact`, for professional and service) is offered once, and it
    # is the only one where a late slot really means "asked last or never".
    #
    # The earlier rule scored every floor field's position, which penalised the model's own
    # ordering — the exact thing the product explicitly refuses to second-guess ("The model's
    # ORDER stands ... second-guessing that is how the carousel starts asking for a phone
    # number before it has said who she is"). On a 4-question set the cutoff was index 3, so a
    # required field in the last slot was flagged despite being unskippable.
    optional_floor = set(floor[2:])
    if len(middle) >= 3 and optional_floor:
        cutoff = len(middle) - max(1, len(middle) // 3)
        late = [
            f"{s.get('field')} at {i + 1}/{len(middle)}"
            for i, s in enumerate(middle)
            if str(s.get("field")) in optional_floor and i >= cutoff
        ]
        if late:
            return CheckResult(
                "floor_present", "SOFT_FAIL",
                f"floor present but positioned late ({', '.join(late)}) — §5: a user can say "
                f"'that's it, post it' at any point, and anything at the end never gets asked",
            )
    return CheckResult("floor_present", "PASS", f"{rtype} floor {list(floor)} present and early")


def check_tail_integrity(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§5's last questions. The consent question gates whether strangers may message the
    author, so it "has to read exactly the same for everyone, so Lana is never allowed to
    reword it" — a byte-exact assertion, deliberately not an import (see ports.CONSENT_QUESTION).

    Also: an agree row with no tallies is "a dead card" (§5), so it must be absent, not empty.
    """
    tail = {str(s.get("field")): s for s in gen.tail()}
    problems: list[str] = []
    consent = tail.get("ask_ok")
    if consent is None:
        problems.append("the consent step (`ask_ok`) is missing — nothing gates whether "
                        "neighbours may message the author")
    else:
        if str(consent.get("question") or "") != CONSENT_QUESTION:
            problems.append(
                f"consent question reworded: {consent.get('question')!r} != {CONSENT_QUESTION!r}"
            )
        if consent.get("kind") != "toggle":
            problems.append(f"consent step kind={consent.get('kind')!r}, expected 'toggle'")
        if consent.get("required"):
            problems.append("consent step marked required — §5 says the tail is never mandatory")
    agree = tail.get("others_also_said")
    if agree is not None and not (agree.get("options") or []):
        problems.append("`others_also_said` present with no tallies — §5: an empty agree row "
                        "is a dead card; it should be omitted, not shown empty")
    # A model-written copy of either tail field would mean validate_steps' `seen` seed failed.
    for f in TAIL_FIELDS:
        if any(str(s.get("field")) == f for s in gen.middle()):
            problems.append(f"{f!r} appears in the model's half of the set — the tail is ours "
                            f"and a generated copy must be dropped")
    if problems:
        return CheckResult("tail_integrity", "HARD_FAIL", "; ".join(problems))
    return CheckResult("tail_integrity", "PASS", "consent step verbatim; agree row omitted or populated")


def check_field_keys(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§9/§10: "The internal labels stay English because they're storage keys, not text anyone
    reads." A key carrying the copy's language orphans the answer the moment the set is
    regenerated with different wording, and it lands in `reco_fields` as-is
    (20261118120000:47). Mirrors reco_question_sets._slug's own output shape."""
    bad: list[str] = []
    for i, step, _ in _questions(gen):
        key = str(step.get("field") or "")
        if not key:
            bad.append(f"step[{i}]: empty field key")
            continue
        # MIRRORS `_slug` EXACTLY, and the leading character class is why:
        #     re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")[:32]
        # That can legitimately begin with a DIGIT — a model labelling a step "24 hour access"
        # or "2nd location" yields `24_hour_access` / `2nd_location`, which is a perfectly good
        # storage key the product produces and the DB accepts. An earlier `[a-z]` first-character
        # rule HARD_FAILed those: a false HARD_FAIL on correct product output, which is the one
        # failure mode this file's header calls the expensive one. Caught by an adversarial
        # review of the criteria, 2026-09-08.
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", key):
            bad.append(f"step[{i}]: field={key!r} is not the shape `_slug` produces "
                       f"(snake_case ASCII, letters/digits/underscore, <=32)")
        elif any(unicodedata.category(c).startswith("L") and ord(c) > 127 for c in key):
            bad.append(f"step[{i}]: field={key!r} carries non-ASCII letters")
    if bad:
        return CheckResult("field_keys", "HARD_FAIL", "; ".join(bad))
    return CheckResult("field_keys", "PASS", "all field keys are snake_case ASCII storage keys")


def check_stated_facts(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§6: "anything already answered — asking again reads as Lana not listening".

    The product's own answer to this is the extractor's `answers` map (tip_share.py:60-66):
    the question may stay in the set, but it arrives PRE-FILLED so `next_question` skips it
    (reco_question_sets.py:163-177). So the check is not "never ask" — it is "if you ask, it is
    already filled in". A set that simply omits the question passes too; both outcomes mean
    the neighbour is not asked to repeat themselves.
    """
    if not fx.stated_facts:
        return CheckResult("stated_facts", "PASS", "fixture states no facts to protect")
    if not gen.prefill_measured:
        # On the turn the set is written, `answers` is {} BY CONSTRUCTION: with no reco_type in
        # `prev`, step_set_of(prev) is empty and the payload's CURRENT TYPE FIELDS block reads
        # "(type not known yet — return {} for answers)" (tip_share.py:151). Scoring the axis
        # off that turn alone would report a structural artefact of the prompt as a Lana
        # defect. The backend has to run the product's turn-2 call to measure it.
        return CheckResult(
            "stated_facts", "UNSCORED",
            "the backend did not run the turn-2 prefill probe, and `answers` is structurally "
            "empty on the generating turn (tip_share.py:151) — this axis is unmeasured, "
            "which is not the same as passed",
        )
    prefilled = {k for k, v in (gen.prefilled or {}).items() if str(v or "").strip()}
    problems: list[str] = []

    def _covers(question: str, needle: str) -> bool:
        """The needle must be essentially the WHOLE question, not merely inside it.

        A bare substring test flagged questions that embed a needle while asking something
        genuinely new — on q05 the needle "can you bring a dog" collided with a question about
        off-leash rules, which the opening line does not answer. The residue rule keeps the
        true positives (the question IS the fact, modulo punctuation and a stray word or two)
        and drops the ones that carry extra interrogative content.
        """
        if needle not in question:
            return False
        residue = question.replace(needle, " ")
        residue = re.sub(r"[^a-z0-9\s]+", " ", residue)
        return len(residue.split()) <= 2

    for fact in fx.stated_facts:
        needles = [str(x).lower() for x in (fact.get("question_any_of") or [])]
        for i, step, q in _questions(gen):
            if any(_covers(q, n) for n in needles):
                if str(step.get("field")) not in prefilled and not str(step.get("answer") or "").strip():
                    problems.append(
                        f"{fact.get('about')!r}: step[{i}] {step.get('question')!r} covers a fact "
                        f"the opening line already gave, and arrived empty — the neighbour will "
                        f"be asked to repeat themselves"
                    )
                break
    if problems:
        return CheckResult("stated_facts", "SOFT_FAIL", "; ".join(problems))
    return CheckResult(
        "stated_facts", "PASS",
        f"{len(fx.stated_facts)} stated fact(s) either not re-asked or pre-filled from the opening line",
    )


def check_tappable(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§6: "give tappable answers whenever the answer is a small set. 'Spice level? Mild /
    Medium / Bring milk' becomes something we can filter on. A paragraph never does."

    The MECHANICAL half of §6's filterability rule. Whether a neighbour would filter on a
    given question needs judgment and lives in judge.py (`filterable`); whether the set gave
    them anything tappable at all is structural, and this is it. A set with zero tappable
    steps has nothing the feed can filter on regardless of how good the questions read.
    """
    middle = gen.middle()
    tappable = [s for s in middle if s.get("kind") in ("choice", "place")
                or len(s.get("options") or []) >= 2]
    if not tappable:
        return CheckResult(
            "tappable", "SOFT_FAIL",
            f"0 of {len(middle)} questions offer a tappable answer — every field is free text, "
            f"so nothing in this recommendation is filterable (§6)",
        )
    return CheckResult("tappable", "PASS",
                       f"{len(tappable)} of {len(middle)} questions are tappable "
                       f"({', '.join(str(s.get('field')) for s in tappable)})")


def check_no_duplicate(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§6: "two questions with the same answer — wastes a card".

    Mechanical half only: an exact repeat after normalisation, or a repeated field key.
    validate_steps already drops duplicate field keys, so a hit on keys means the guard did
    not run. Near-duplicates that differ in wording ("how busy is it?" / "how long's the
    line?") need judgment and are the judge's.
    """
    seen_q: dict[str, int] = {}
    seen_f: dict[str, int] = {}
    problems: list[str] = []
    for i, step, q in _questions(gen):
        if q in seen_q:
            problems.append(f"steps[{seen_q[q]}] and [{i}] ask the same question: {q!r}")
        seen_q.setdefault(q, i)
        fname = str(step.get("field") or "")
        if fname in seen_f:
            problems.append(f"steps[{seen_f[fname]}] and [{i}] share field {fname!r} — the "
                            f"second answer would overwrite the first")
        seen_f.setdefault(fname, i)
    if problems:
        return CheckResult("no_duplicate", "SOFT_FAIL", "; ".join(problems))
    return CheckResult("no_duplicate", "PASS", "no repeated question or field key")


def check_placeholders(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§6: "write an example answer under each question, for THAT subject".

    Presence only. Whether the example is about THIS subject ("about 30 minutes") rather than
    generic ("e.g. duration") is judgment, and belongs to judge.py's `subject_tailored`.
    Chip and map steps need no placeholder — the control already shows the options.
    """
    needs = [s for s in gen.middle() if s.get("kind") == "text"]
    if not needs:
        return CheckResult("placeholders", "PASS", "no free-text steps in the set")
    missing = [str(s.get("field")) for s in needs if not str(s.get("placeholder") or "").strip()]
    if not missing:
        return CheckResult("placeholders", "PASS", f"all {len(needs)} free-text steps carry an example")
    # Attribute the miss. A floor field that validate_steps REPAIRED in is copied from the
    # static `_SETS` table, and no entry there carries a `placeholder` key — so a repaired
    # floor field arrives with no example answer by construction, whatever the model did.
    # That is a product-side gap, not a generation failure, and the report has to say which
    # or the finding lands on the wrong team.
    floor = set(_FLOOR_MIRROR.get(str(gen.reco_type or ""), ()))
    from_floor = [f for f in missing if f in floor]
    # The subject step is injected by `head_step()` (reco_question_sets.py:190), not written by
    # the model, and it carries no `placeholder` key — so attributing it to "the model wrote no
    # example" points the finding at the wrong team, the same way a repaired floor field did.
    from_subject = [f for f in missing if f == SUBJECT_FIELD]
    from_model = [f for f in missing if f not in floor and f != SUBJECT_FIELD]
    bits = []
    if from_model:
        bits.append(f"model wrote no example for {from_model}")
    if from_subject:
        bits.append(
            "the `subject` step carries no example — it is injected by `head_step()`, not "
            "written by the model, and that function sets no `placeholder`"
        )
    if from_floor:
        bits.append(
            f"floor field(s) {from_floor} carry no example — these are copied from the static "
            f"`_SETS` table by validate_steps' repair, and no entry there has a `placeholder`, "
            f"so a repaired floor field can never have one"
        )
    return CheckResult(
        "placeholders", "SOFT_FAIL",
        f"{len(missing)} of {len(needs)} free-text step(s) have no example answer — "
        + "; ".join(bits) + " (§6 asks for one under each question)",
    )


def check_set_size(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """_STEPS_SPEC asks for 4-8 (tip_share.py:78); reco_question_sets caps the middle at 10
    after the floor is repaired in. §7: "a recommendation is a card, not an interrogation"."""
    n = len(gen.middle())
    if n < _MIN_MIDDLE:
        return CheckResult("set_size", "SOFT_FAIL",
                           f"{n} questions — below the {_MIN_MIDDLE} the spec asks for; a card "
                           f"this thin tells a reader almost nothing")
    if n > _MAX_MIDDLE:
        return CheckResult("set_size", "HARD_FAIL",
                           f"{n} questions exceeds the _MAX_MIDDLE={_MAX_MIDDLE} cap — the cap "
                           f"in validate_steps did not run")
    return CheckResult("set_size", "PASS", f"{n} questions (spec: {_MIN_MIDDLE}-{_MAX_MIDDLE})")


def check_language(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """§9: "If someone is chatting in Spanish, the whole question set is written in Spanish."

    A marker-frequency test, not language identification — it answers "did the copy switch at
    all", which is the failure the doc describes (an English set handed to a Spanish speaker).
    A majority threshold keeps a proper noun or a loanword from deciding it either way.
    """
    if not fx.lang:
        return CheckResult("language", "PASS", "English fixture — nothing to translate")
    markers = _LANG_MARKERS.get(fx.lang.strip().lower())
    if markers is None:
        return CheckResult("language", "UNSCORED",
                           f"no marker set for lang={fx.lang!r} — cannot tell what language "
                           f"the copy is in, so this is not scored as a pass")
    middle = gen.middle()
    if not middle:
        return CheckResult("language", "UNSCORED", "empty set — nothing to inspect")
    # STRIP THE SUBJECT'S OWN NAME BEFORE COUNTING MARKERS. §2 requires questions written for
    # THIS subject, so a Spanish-named subject ("Dra. Ruiz", "Taqueria El Rey") appears in most
    # questions — and the marker set contains bare `los`/`las`/`del`/`una`/`para` plus the
    # accent class, so an entirely ENGLISH set for such a subject cleared the 50% bar and was
    # reported as §9-compliant. The name is the neighbour's word, not evidence that Lana
    # translated anything.
    name = str((fx.prior_draft or {}).get("name") or "").strip()
    def _strip_name(text: str) -> str:
        return re.sub(re.escape(name), " ", text, flags=re.IGNORECASE) if name else text

    hit = [s for s in middle if markers.search(_strip_name(str(s.get("question") or "")))]
    if len(hit) * 2 < len(middle):
        return CheckResult(
            "language", "SOFT_FAIL",
            f"only {len(hit)} of {len(middle)} questions read as {fx.lang} — §9 says the whole "
            f"set is written in the neighbour's language",
        )
    return CheckResult("language", "PASS", f"{len(hit)}/{len(middle)} questions read as {fx.lang}")


def check_reco_type(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """The type is a fixed taxonomy the DB enforces (20261117120000:16) and browsing depends
    on ("all the recipes near me"), so a wrong type is scored on its own axis rather than
    folded into question quality — a perfect set of questions filed under the wrong type is
    still a recommendation nobody finds."""
    from ports import RecoType  # local: only needed for the literal's members

    valid = set(getattr(RecoType, "__args__", ()))
    got = gen.reco_type
    if got is not None and got not in valid:
        return CheckResult("reco_type", "HARD_FAIL",
                           f"reco_type={got!r} is not one of the seven the DB accepts — "
                           f"set_signal_reco raises invalid_reco_type on write "
                           f"(20261117120000:50)")
    if fx.expect_reco_type is None:
        return CheckResult("reco_type", "PASS", f"reco_type={got!r} (fixture pins none)")
    if got != fx.expect_reco_type:
        return CheckResult("reco_type", "SOFT_FAIL",
                           f"reco_type={got!r}, fixture expects {fx.expect_reco_type!r} — the "
                           f"floor, the static fallback and the browse index all key off this")
    return CheckResult("reco_type", "PASS", f"reco_type={got!r} as expected")


def check_generation_ran(gen: GeneratedSet, fx: QuestionFixture) -> CheckResult:
    """Did Lana WRITE this set, or did the flow fall back to the type's static seven?

    §1 and §2 are the whole premise: "Lana writes a fresh set of questions for every single
    recommendation", replacing the one-form-for-everything that produced useless cards. A
    fallback set is well-formed and reads fine, so this is invisible from the outside — which
    is exactly why it needs an axis. If this fails broadly, every other number in Arm A is
    describing the static sets in reco_question_sets.py and not Lana at all.
    """
    if fx.expect_generated and not gen.generated:
        return CheckResult(
            "generation_ran", "HARD_FAIL",
            "the set is the type's STATIC fallback, not written for this subject — §1's "
            "one-form-for-everything, shipping under a flow that reports nothing",
        )
    if not fx.expect_generated and gen.generated:
        return CheckResult(
            "generation_ran", "PASS",
            "a set WAS generated where the fixture expected the static fallback — the "
            "ordering hazard this fixture pins may have been fixed; re-check the fixture",
        )
    if not fx.expect_generated:
        return CheckResult(
            "generation_ran", "PASS",
            "static fallback, as this fixture expects — see its `probes`. This is the known "
            "hazard, recorded so it starts failing the day it is fixed, not a clean result",
        )
    return CheckResult("generation_ran", "PASS", "set written for this subject")


ARM_A_CHECKS = (
    check_generation_ran,
    check_banned_generic,
    check_privacy_floor,
    check_google_answerable,
    check_floor_present,
    check_tail_integrity,
    check_field_keys,
    check_stated_facts,
    check_tappable,
    check_no_duplicate,
    check_placeholders,
    check_set_size,
    check_language,
    check_reco_type,
)


def run_question_checks(gen: GeneratedSet, fx: QuestionFixture) -> list[CheckResult]:
    """Every Arm A axis. A port error is a HARD_FAIL on its own axis and stops the rest —
    scoring an empty set would report a dozen vacuous passes."""
    if gen.error:
        return [CheckResult("backend", "HARD_FAIL", f"question-set generation failed: {gen.error}")]
    if not gen.steps:
        return [CheckResult("backend", "UNSCORED", "port returned an empty set with no error")]
    return [fn(gen, fx) for fn in ARM_A_CHECKS]


# ===========================================================================
# ARM B — answer acceptance
# ===========================================================================

def check_answer_decision(dec: AnswerDecision, fx: AnswerFixture) -> CheckResult:
    """The validator's decision against the hand-label.

    A wrong ACCEPT and a wrong REJECT are not the same defect and are not scored the same:

      accepted junk  -> SOFT_FAIL. It is the gap §11 describes, it is what this arm exists to
                        measure, and against the `shipped` validator it is the EXPECTED
                        result on every reject row. A HARD_FAIL would make the report a wall
                        of red that says nothing beyond "there is no validator", which we
                        already know — the rate is the finding, not the verdict.
      rejected good  -> HARD_FAIL. This is the failure mode §12.4 rules out: telling a
                        neighbour their recommendation was not good enough. An over-eager
                        validator is worse than none, because it drives people out of the
                        flow instead of merely storing something thin.
    """
    if dec.unscorable:
        return CheckResult("answer_decision", "UNSCORED",
                           f"validator could not decide: {dec.reason}")
    want_accept = fx.verdict == "accept"
    if dec.accepted == want_accept:
        return CheckResult("answer_decision", "PASS",
                           f"{'accepted' if dec.accepted else 'rejected'} as labelled")
    if dec.accepted and not want_accept:
        return CheckResult(
            "answer_decision", "SOFT_FAIL",
            f"ACCEPTED junk: {fx.answer!r} for {fx.question!r} — {fx.reason.strip()}",
        )
    return CheckResult(
        "answer_decision", "HARD_FAIL",
        f"REJECTED a good answer: {fx.answer!r} for {fx.question!r} — §12.4: nobody should "
        f"be told their recommendation was not good enough. {dec.reason}",
    )


def check_truncation(dec: AnswerDecision, fx: AnswerFixture) -> CheckResult:
    """The 280-char cap at main.py:2538 is a SILENT truncation — `[:280]` with no ellipsis, no
    warning to the user and no record that anything was cut. Scored on its own axis because it
    is orthogonal to whether the answer was any good: a16 in fixtures.yaml is a GOOD answer
    that arrives on the card mid-sentence.

    MEASURED CONSEQUENCE FOR THE PLANNED AI VALIDATOR (2026-09-08). Truncation happens BEFORE
    any validator sees the text, so a validator is handed a fragment ending mid-sentence. Four
    trials each against the reference validator:

        the same recipe method, complete, under the cap   ACCEPT ACCEPT ACCEPT ACCEPT
        the fixture, cut at 280                           ACCEPT REJECT ACCEPT ACCEPT
                                                          (reject reason: "incomplete steps")

    So truncate-then-validate turns a good long answer into an unstable reject — and it hits
    exactly the richest answers, the ones most worth keeping. If the "Add AI Validator" work
    lands on top of the existing `[:280]`, it needs to validate BEFORE truncating, or the cap
    needs to go. Reported here rather than fixed: this suite evaluates.
    """
    if dec.stored is None:
        return CheckResult("truncation", "PASS", "answer not stored — nothing to truncate")
    collapsed = " ".join(fx.answer.split())
    if len(collapsed) > 280 and len(dec.stored) == 280:
        return CheckResult(
            "truncation", "SOFT_FAIL",
            f"answer silently truncated {len(collapsed)} -> 280 chars, cut mid-word at "
            f"...{dec.stored[-40:]!r}. No ellipsis, no warning, no record (main.py:2543).",
        )
    return CheckResult("truncation", "PASS", f"stored intact ({len(dec.stored)} chars)")


# Floor fields whose whole purpose is that a reader can ACT on the card: reach the person, or
# find the thing. §5 puts each of them in a type's floor for exactly that reason.
_REACHABILITY_FIELDS = frozenset({"contact", "where", "where_to_buy"})


def check_reachable(dec: AnswerDecision, fx: AnswerFixture) -> CheckResult:
    """§11's closing case, isolated: "the must-have check only asks 'is this blank?' — so
    'ask me' passes as a contact method and the plumber card ships unreachable anyway."

    Keyed on the FIELD, not on `required`, and that is the correction the shipped code forces.
    §11 treats `contact` as a must-have whose blank-check is too weak. It is not a must-have:
    `required` is assigned to the first TWO floor fields only (reco_question_sets.py:367-368),
    and for `service` and `professional` the floor is (…, …, contact) — so contact is third,
    and optional. It is offered once (next_question, :163-177), and a neighbour who says
    "that's it, post it" is never asked for it at all.

    So a card ships unreachable by two independent routes, and scoring only the `required` ones
    would report the smaller half of the problem.
    """
    if fx.field_name not in _REACHABILITY_FIELDS:
        return CheckResult("reachable", "PASS", "not a reachability field")
    if fx.verdict == "accept":
        return CheckResult("reachable", "PASS", "fixture labels this reachable")
    if dec.accepted:
        gate = ("blocks the ready card until non-blank" if fx.required
                else "is NOT required at all — offered once and skippable "
                     "(reco_question_sets.py:367-368)")
        return CheckResult(
            "reachable", "SOFT_FAIL",
            f"{fx.field_name!r} satisfied by {fx.answer!r}, and that field {gate}. The "
            f"recommendation posts with no way for a neighbour to act on it.",
        )
    return CheckResult("reachable", "PASS", "unreachable value refused")


ARM_B_CHECKS = (check_answer_decision, check_truncation, check_reachable)


# ===========================================================================
# The pass rules, in one place, next to the code that implements them
# ===========================================================================
#
# Rendered into every report as an appendix. It lives HERE rather than in a markdown file so a
# reader auditing a number can find the rule that produced it, and so the two cannot drift:
# selftest.py fails if any axis in ARM_A_CHECKS / ARM_B_CHECKS / the judged set is missing an
# entry, so a new check cannot be added without stating what makes it pass.
#
#   axis -> (what PASSES, how it is decided, severity when it does not)
PASS_RULES: dict[str, tuple[str, str, str]] = {
    # ---- Arm A, mechanical -------------------------------------------------------------
    "generation_ran": (
        "The model's own proposal survived into the set.",
        "`raw_steps` non-empty AND the set's field list differs from the type's static set. "
        "Either signal alone misses a case, so both are checked (live_impl.detect_fallback). "
        "A fixture may pin the inverse via `expect_generated: false`.",
        "HARD_FAIL",
    ),
    "banned_generic": (
        "No question is one of §6's banned shapes.",
        "6 regexes over each question, lowercased and whitespace-collapsed. The pronoun-object "
        "pattern is anchored to END-OF-QUESTION, so 'about it?' is banned and 'about the crust' "
        "is not.",
        "HARD_FAIL",
    ),
    "privacy_floor": (
        "No question asks for private data.",
        "7 global regexes (home address, full name, DOB, gov/financial id, income, "
        "credentials) plus this fixture's own `forbidden_topics`, matched on word boundaries. "
        "Deliberately WIDER than the product's `_BLOCKED_ASK` — the job is to report what that "
        "filter misses.",
        "HARD_FAIL",
    ),
    "google_answerable": (
        "On a subject a listing already covers, nothing asks what the listing says.",
        "Runs only when the fixture sets `lookup_able`. 5 topic regexes (hours, phone, website, "
        "address, price band), skipping any step that is one of the type's own FLOOR fields or "
        "has `kind == place` — §5 requires both, so flagging them would be a false positive.",
        "SOFT_FAIL — §12 is a proposal, not shipped",
    ),
    "floor_present": (
        "Every floor field for the type is in the set, and the optional one is not at the end.",
        "Compare the set's fields against the type's floor (mirrored from `_FLOOR`, with a "
        "drift guard that returns UNSCORED if the product's floor has moved). Position is "
        "scored ONLY for the third floor field: the first two are `required` and keep coming "
        "back until answered, so where they sit cannot matter.",
        "HARD_FAIL missing · SOFT_FAIL late",
    ),
    "tail_integrity": (
        "The two closing steps are ours and unmodified.",
        "`ask_ok` present, its question BYTE-EQUAL to the pinned consent string, `kind=toggle`, "
        "not required; `others_also_said` absent or carrying options (never empty); neither "
        "field appearing in the model's half of the set.",
        "HARD_FAIL",
    ),
    "field_keys": (
        "Every storage key is the shape `_slug` produces.",
        "`re.fullmatch(r'[a-z0-9][a-z0-9_]{0,31}')` plus a non-ASCII-letter scan. Mirrors "
        "`_slug` exactly, including that it may begin with a digit.",
        "HARD_FAIL",
    ),
    "stated_facts": (
        "Nothing the opening line already answered is asked cold.",
        "For each fact the fixture declares: find a question the needle COVERS (needle minus "
        "question leaves <=2 words — a bare substring test flagged questions that merely "
        "contain the needle while asking something new). If found, it must arrive pre-filled "
        "from the extractor's `answers`. Never asking it passes too.",
        "SOFT_FAIL · UNSCORED without the turn-2 prefill probe",
    ),
    "tappable": (
        "At least one question offers a tappable answer.",
        "Count steps with `kind in (choice, place)` or >=2 options. The STRUCTURAL half of §6's "
        "filterability rule; whether a neighbour would filter on a given question is judgment "
        "and belongs to the judged `filterable` axis.",
        "SOFT_FAIL",
    ),
    "no_duplicate": (
        "No two questions ask the same thing.",
        "Exact repeat after normalisation, or a repeated field key (the second answer would "
        "overwrite the first). Near-duplicates in different words need judgment and are the "
        "judge's.",
        "SOFT_FAIL",
    ),
    "placeholders": (
        "Every free-text question carries an example answer.",
        "`kind == text` steps must have a non-empty `placeholder`. Misses are attributed: a "
        "floor field repaired in by `validate_steps` is copied from the static table, which "
        "has no placeholder key, so it can never have one — a product gap, not a model one.",
        "SOFT_FAIL",
    ),
    "set_size": (
        "4 to 10 questions.",
        "`len(middle)`. The prompt asks for 4-8; `validate_steps` caps the middle at 10 after "
        "repairing the floor in, so 10 is the reachable ceiling.",
        "SOFT_FAIL under 4 · HARD_FAIL over 10",
    ),
    "language": (
        "The set is written in the neighbour's language.",
        "Marker-frequency, not language identification: at least half the questions must match "
        "that language's marker regex AFTER the subject's own name is stripped out (a "
        "Spanish-named subject otherwise carried an English set past the bar). UNSCORED if no "
        "marker set exists for the language.",
        "SOFT_FAIL",
    ),
    "reco_type": (
        "The type is one the DB accepts, and the one the fixture expects.",
        "Membership in the seven-value taxonomy the migration's CHECK constraint enforces, then "
        "equality with `expect_reco_type` when the fixture pins one.",
        "HARD_FAIL out-of-taxonomy · SOFT_FAIL mismatch",
    ),
    # ---- Arm A, judged -----------------------------------------------------------------
    "filterable": (
        "At least 3 questions a reader would later filter or search on (§6).",
        "LLM judge (gpt-4o, independent of the model under test) counts them and names them. "
        "3+ PASS · 1-2 SOFT_FAIL · 0 HARD_FAIL — thresholds given to the judge verbatim.",
        "judged",
    ),
    "lazy_vs_good": (
        "The questions are answerable only by someone who actually went.",
        "LLM judge against §6's lazy-vs-good table. Explicitly told to IGNORE questions a "
        "public listing would answer, so it cannot double-count `google_answerable`.",
        "judged",
    ),
    "subject_tailored": (
        "The set is about THIS subject, not pasteable onto any other of its type (§2).",
        "LLM judge, told to include the example answers in the judgment. Told that a merely "
        "ORDINARY set is a PASS, and to reserve HARD_FAIL for a set producing a card a reader "
        "cannot use.",
        "judged",
    ),
    # ---- Arm B -------------------------------------------------------------------------
    "answer_decision": (
        "The validator's accept/reject matches the hand label.",
        "Deliberately ASYMMETRIC. Accepting junk is SOFT — it is the product's known state and "
        "the RATE is the finding. Rejecting a good answer is HARD, because §12.4 rules out "
        "telling a neighbour their recommendation was not good enough.",
        "SOFT_FAIL wrong-accept · HARD_FAIL wrong-reject · UNSCORED undecided",
    ),
    "truncation": (
        "A good answer is not silently cut.",
        "Fires when the whitespace-collapsed input exceeded 280 chars AND the stored value is "
        "exactly 280. Independent of whether the answer was any good.",
        "SOFT_FAIL",
    ),
    "reachable": (
        "A required-to-act field is not satisfied by an unusable value.",
        "Narrowed to `contact` / `where` / `where_to_buy`, and only on reject-labelled rows the "
        "validator accepted. Isolates §11's plumber case into one quotable number and reports "
        "whether the field is even required (for `contact`: it is not).",
        "SOFT_FAIL",
    ),
}


def run_answer_checks(dec: AnswerDecision, fx: AnswerFixture) -> list[CheckResult]:
    return [fn(dec, fx) for fn in ARM_B_CHECKS]
