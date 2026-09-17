"""
selftest.py — proves the recommendation-quality checks are not vacuous.

    cd services/lana-worker/simulations/reco_eval
    python selftest.py

No API key, no server, no DB. Every case is offline and deterministic.

WHY THIS FILE EXISTS
--------------------
A green eval run is worthless if the checks never fire. Every mechanical axis in checks.py is
fed a PLANTED violation here and asserted to fail — and fed a clean input and asserted to
pass, because a check that fires on everything is just as useless as one that fires on
nothing, and considerably more expensive (it poisons a gate, people mute it, and the real
finding underneath goes with it).

It also carries the DRIFT TRIPWIRES. Three things in this harness are copies of product
values — the consent question, the per-type floor, and the 280-char storage transform. A copy
that has silently gone stale scores Lana against a rule that no longer exists, which is how
this suite has produced false findings before. Each one is asserted against the product here,
so the failure lands on us at selftest time rather than on backend in a report.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1]))  # services/lana-worker -> `import app.*`

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import checks  # noqa: E402
import run_eval  # noqa: E402
from ports import (  # noqa: E402
    CONSENT_QUESTION,
    AnswerFixture,
    GeneratedSet,
    QuestionFixture,
    worst,
)
from validators import ShippedValidator, store_as  # noqa: E402

_PASSED = 0
_FAILED: list[str] = []


def expect(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
    else:
        _FAILED.append(msg)
        print(f"  FAIL: {msg}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 66 - len(title)))


# ---------------------------------------------------------------------------
# Builders — a clean set, and a mutator to plant one violation into it
# ---------------------------------------------------------------------------

_TAIL = [{"field": "ask_ok", "label": "Neighbours", "question": CONSENT_QUESTION,
          "kind": "toggle", "options": ["Let them ask", "Keep it to the card"],
          "required": False}]


def clean_set(reco_type: str = "restaurant", **over) -> GeneratedSet:
    """A set that must pass every Arm A axis. Any check that fails this is a false positive."""
    steps = [
        {"field": "dish", "label": "Order this", "question": "What should they order?",
         "kind": "text", "placeholder": "the al pastor tacos", "required": True},
        {"field": "where", "label": "Location", "question": "Where can neighbours find it?",
         "kind": "place", "required": True},
        {"field": "line_at_lunch", "label": "Lunch line",
         "question": "How long is the line at lunch?", "kind": "choice",
         "options": ["No wait", "10 minutes", "Worth the wait"], "required": False},
        {"field": "parking", "label": "Parking",
         "question": "Is there parking, or is it street only?", "kind": "choice",
         "options": ["Own lot", "Street only"], "required": False},
        {"field": "kids", "label": "With kids",
         "question": "Do they have a kids' menu or high chairs?", "kind": "text",
         "placeholder": "high chairs, no kids' menu", "required": False},
    ]
    base = {"steps": steps + _TAIL, "reco_type": reco_type, "prefilled": {},
            "raw_steps": steps, "generated": True, "prefill_measured": True}
    base.update(over)
    return GeneratedSet(**base)


def clean_fx(**over) -> QuestionFixture:
    base = dict(id="selftest", opening_line="Taqueria El Rey off Narcoossee — best al pastor",
                prior_draft={}, expect_reco_type="restaurant", lookup_able=True,
                stated_facts=[], forbidden_topics=[], lang=None, expect_generated=True)
    base.update(over)
    return QuestionFixture(**base)  # type: ignore[arg-type]


def with_step(step: dict, reco_type: str = "restaurant") -> GeneratedSet:
    """The clean set with one extra step planted in the middle."""
    gen = clean_set(reco_type)
    middle = gen.middle()
    gen.steps = middle + [{"kind": "text", "required": False, **step}] + _TAIL
    return gen


# ===========================================================================
print("=" * 72)
print("reco_eval selftest")
print("=" * 72)

section("0. the clean set passes every axis (no false positives)")
_clean = checks.run_question_checks(clean_set(), clean_fx())
for r in _clean:
    expect(r.verdict == "PASS", f"clean set should pass `{r.name}`, got {r.verdict}: {r.detail}")
expect(worst(_clean) == "PASS", f"clean set overall verdict is {worst(_clean)}")

section("1. banned_generic (§6) fires on each banned shape")
for q in ("What stood out for you?",
          "What did you like about it?",
          "Why is it good?",
          "Anything else?",
          "Tell me more about it?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_banned_generic(gen, clean_fx())
    expect(r.verdict == "HARD_FAIL", f"banned_generic missed {q!r}")

section("1b. banned_generic does NOT fire on a specific version of the same verb")
for q in ("What did you like about the al pastor?",
          "What stands between a good taco and a great one here?",
          "Anything else on the menu worth a second trip?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_banned_generic(gen, clean_fx())
    expect(r.verdict == "PASS", f"banned_generic FALSE POSITIVE on {q!r}: {r.detail}")

section("2. privacy_floor (§6 last row) fires — including phrasings _BLOCKED_ASK misses")
for q, why in (
    ("What's her home address?", "covered by the product's own regex"),
    ("Where does she live?", "NOT covered by _BLOCKED_ASK"),
    ("Which house is she at?", "NOT covered by _BLOCKED_ASK"),
    ("What's her full name?", "covered"),
    ("What's her date of birth?", "covered"),
    ("How much do they make?", "covered"),
):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_privacy_floor(gen, clean_fx())
    expect(r.verdict == "HARD_FAIL", f"privacy_floor missed {q!r} ({why})")

section("1c. banned_generic does NOT fire on a determiner-led specific object")
# `her`, `this` and `that` are determiners as well as pronouns. With a trailing \b the ban hit
# every qualified noun phrase starting with one — and the verdict was GENDER-DEPENDENT, since
# `his` is absent from the alternation. Both are false HARD_FAILs on good questions.
for q in ("What did you like about her chairside manner with anxious kids?",
          "What did you like about his approach to nervous kids?",
          "What did you like about this recipe's texture?",
          "What did you like about that first visit?",
          "What did you like about the place settings?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_banned_generic(gen, clean_fx())
    expect(r.verdict == "PASS", f"banned_generic FALSE POSITIVE on {q!r}: {r.detail}")
# ...and the real banned wordings, which live verbatim in the product's static sets, still fire.
for q in ("What did you like about them?", "What did you like about it?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    expect(checks.check_banned_generic(gen, clean_fx()).verdict == "HARD_FAIL",
           f"banned_generic stopped catching the real static wording {q!r}")

section("2b. privacy_floor honours a fixture's own forbidden topics")
gen = with_step({"field": "extra", "label": "X", "question": "What street is she on?",
                 "placeholder": "x"})
fx = clean_fx(forbidden_topics=[{"about": "home street",
                                 "question_any_of": ["what street"]}])
expect(checks.check_privacy_floor(gen, fx).verdict == "HARD_FAIL",
       "privacy_floor ignored a fixture-declared forbidden topic")

section("2c. privacy_floor does NOT fire on innocent questions that contain the same words")
# privacy_floor was the ONE axis with no must-not-fire section, which is why these survived.
# "Which household tasks…" is the most natural question a good set would write for a nanny —
# the fixture whose entire purpose is privacy — and it HARD_FAILed as "asks for where she lives".
for q in ("Which household tasks does she take on?",
          "Which house plants do they keep alive?",
          "Do they do passport photos while you wait?",
          "Do they host birthday parties?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_privacy_floor(gen, clean_fx())
    expect(r.verdict == "PASS", f"privacy_floor FALSE POSITIVE on {q!r}: {r.detail}")
# The genuine asks still fire, including the phrasings the product's _BLOCKED_ASK misses.
for q in ("Which house is she at?", "Where does she live?", "What's her passport number?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    expect(checks.check_privacy_floor(gen, clean_fx()).verdict == "HARD_FAIL",
           f"privacy_floor stopped catching a real private ask: {q!r}")

section("3. google_answerable (§12.1) fires on all five topics for a lookup-able subject")
for q, topic in (
    ("What are their opening hours?", "hours"),
    # The exact wording in reco_question_sets' static `location` set. An earlier pattern
    # missed it, which made google_answerable vacuous on the one type most likely to trip it.
    ("When is it open — and the best time to go?", "hours (static `location` wording)"),
    ("When are they open?", "hours"),
    ("What time do they close?", "hours"),
    ("What's their phone number?", "phone"),
    ("Do they have a website?", "website"),
    ("What's their address?", "address"),
    ("What's the price range?", "price_band"),
):
    gen = with_step({"field": f"x_{topic}", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_google_answerable(gen, clean_fx(lookup_able=True))
    expect(r.verdict == "SOFT_FAIL", f"google_answerable missed {topic}: {q!r}")

section("3b. google_answerable does NOT fire where it must not")
# not lookup-able -> the whole axis is inapplicable (a product's price is neighbour knowledge)
gen = with_step({"field": "price", "label": "Price", "question": "What's the price range?",
                 "placeholder": "$60"}, reco_type="product")
expect(checks.check_google_answerable(gen, clean_fx(lookup_able=False,
                                                    expect_reco_type="product")).verdict == "PASS",
       "google_answerable fired on a NOT lookup-able subject")
# a type's own FLOOR field is exempt: a pediatrician is lookup-able and `contact` is required
gen = clean_set("professional")
gen.steps = [
    {"field": "profession", "label": "P", "question": "What does she practise?",
     "kind": "text", "placeholder": "paediatrics", "required": True},
    {"field": "helped_with", "label": "H", "question": "What did she help you with?",
     "kind": "text", "placeholder": "a first filling", "required": True},
    {"field": "contact", "label": "C", "question": "What's their phone number?",
     "kind": "text", "placeholder": "555-0134", "required": False},
    {"field": "ages", "label": "A", "question": "Which ages does she see?", "kind": "choice",
     "options": ["Babies", "Toddlers", "Teens"], "required": False},
] + _TAIL
r = checks.check_google_answerable(gen, clean_fx(expect_reco_type="professional",
                                                 lookup_able=True))
expect(r.verdict == "PASS",
       f"google_answerable FALSE POSITIVE on the type's own floor field `contact`: {r.detail}")
# a map-picker step is how §5 says an address SHOULD be captured
gen = with_step({"field": "venue", "label": "V", "question": "What's the address?",
                 "kind": "place"})
expect(checks.check_google_answerable(gen, clean_fx()).verdict == "PASS",
       "google_answerable FALSE POSITIVE on a kind=place step")
# Visitor knowledge that the unanchored patterns caught. Each is a GOOD question — how long a
# loop takes, which hours are busiest, the signature dish, the room count — reported as
# "on the listing".
for q in ("Which hours are busiest?",
          "How many hours does the loop take?",
          "Roughly what does a first visit look like?",
          "What's their number one dish?",
          "What's the number of tables inside?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    r = checks.check_google_answerable(gen, clean_fx(lookup_able=True))
    expect(r.verdict == "PASS", f"google_answerable FALSE POSITIVE on {q!r}: {r.detail}")
# The real §12.1 asks still fire, including reco_question_sets' own cost wording.
for q in ("Roughly what does a meal cost?", "What's their number?", "What are their hours?"):
    gen = with_step({"field": "extra", "label": "X", "question": q, "placeholder": "x"})
    expect(checks.check_google_answerable(gen, clean_fx(lookup_able=True)).verdict == "SOFT_FAIL",
           f"google_answerable stopped catching a real listing question: {q!r}")

section("4. floor_present (§5) fires on a missing floor field and on a late one")
gen = clean_set()
gen.steps = [s for s in gen.middle() if s["field"] != "dish"] + _TAIL
expect(checks.check_floor_present(gen, clean_fx()).verdict == "HARD_FAIL",
       "floor_present missed an absent floor field")
# The late-position rule applies ONLY to the OPTIONAL floor field — the third one. The first
# two are `required` and keep coming back until answered (next_question), so their position is
# irrelevant and flagging it penalised the model's ordering, which the product refuses to
# second-guess. `service` floor is (service, helped_with, contact); `contact` is the optional one.
_SVC = [
    {"field": "service", "label": "S", "question": "What do they do?", "kind": "text",
     "placeholder": "AC repair", "required": True},
    {"field": "helped_with", "label": "H", "question": "What did they do for you?",
     "kind": "text", "placeholder": "replaced the blower", "required": True},
    {"field": "on_time", "label": "O", "question": "Did they show up when they said?",
     "kind": "choice", "options": ["Yes", "Ran late"], "required": False},
    {"field": "quote_held", "label": "Q", "question": "Was the final bill the quote?",
     "kind": "choice", "options": ["Same", "More"], "required": False},
    {"field": "contact", "label": "C", "question": "How do neighbours reach them?",
     "kind": "text", "placeholder": "his cell", "required": False},
]
gen = GeneratedSet(steps=_SVC + _TAIL, reco_type="service", raw_steps=_SVC,
                   generated=True, prefill_measured=True)
r = checks.check_floor_present(gen, clean_fx(expect_reco_type="service"))
expect(r.verdict == "SOFT_FAIL",
       f"floor_present missed `contact` (the OPTIONAL floor field) in the last slot — the one "
       f"case where a late position really means 'offered once or never': {r.verdict}")
# ...and a REQUIRED floor field last is fine, because it cannot be skipped.
gen = GeneratedSet(steps=[_SVC[2], _SVC[4], _SVC[3], _SVC[0], _SVC[1]] + _TAIL,
                   reco_type="service", raw_steps=_SVC, generated=True, prefill_measured=True)
r = checks.check_floor_present(gen, clean_fx(expect_reco_type="service"))
expect(r.verdict == "PASS",
       f"floor_present FALSE POSITIVE: it flagged a REQUIRED floor field's position, but "
       f"required steps keep coming back until answered: {r.detail}")

section("5. tail_integrity (§5) — the consent question may never be reworded")
gen = clean_set()
gen.steps = gen.middle() + [{**_TAIL[0], "question": "Can your neighbours reach out to you?"}]
expect(checks.check_tail_integrity(gen, clean_fx()).verdict == "HARD_FAIL",
       "tail_integrity missed a reworded consent question")
gen = clean_set()
gen.steps = gen.middle()  # no tail at all
expect(checks.check_tail_integrity(gen, clean_fx()).verdict == "HARD_FAIL",
       "tail_integrity missed a MISSING consent step")
gen = clean_set()
gen.steps = gen.middle() + _TAIL + [{"field": "others_also_said", "label": "O",
                                     "question": "Others also said", "kind": "agree",
                                     "options": [], "required": False}]
expect(checks.check_tail_integrity(gen, clean_fx()).verdict == "HARD_FAIL",
       "tail_integrity missed an EMPTY agree row (§5: a dead card)")

section("6. field_keys (§9) — storage keys stay snake_case ASCII")
for key in ("¿QuéEspecialidad?", "Profesión", "What They Do", "profesión_médica"):
    gen = with_step({"field": key, "label": "X", "question": "¿Qué hace?", "placeholder": "x"})
    expect(checks.check_field_keys(gen, clean_fx()).verdict == "HARD_FAIL",
           f"field_keys accepted a non-ASCII/non-snake key {key!r}")

section("6b. field_keys does NOT fire on keys `_slug` legitimately produces")
# `_slug` is re.sub(r"[^a-z0-9]+","_",...).strip("_")[:32] — it can begin with a DIGIT. A model
# labelling a step "24 hour access" or "2nd location" produces exactly these, and they are
# valid storage keys. An earlier `[a-z]` first-character rule HARD_FAILed them: a false
# HARD_FAIL on correct product output. Generated through the REAL _slug, not hand-typed, so
# this cannot drift away from what the product actually emits.
try:
    from app.reco_question_sets import _slug  # type: ignore[attr-defined]
    for label in ("24 hour access", "2nd location", "5-star?", "ADA accessible", "kid-friendly"):
        key = _slug(label)
        gen = with_step({"field": key, "label": "X", "question": "Is it there?",
                         "placeholder": "x"})
        r = checks.check_field_keys(gen, clean_fx())
        expect(r.verdict == "PASS",
               f"field_keys FALSE POSITIVE on {key!r} (from label {label!r}), which is exactly "
               f"what the product's own _slug emits: {r.detail}")
except ImportError:
    print("  [skip] _slug not importable")

section("7. stated_facts (§6) — a re-asked fact must arrive pre-filled")
fx = clean_fx(stated_facts=[{"about": "what to order",
                             "question_any_of": ["what should they order"]}])
expect(checks.check_stated_facts(clean_set(), fx).verdict == "SOFT_FAIL",
       "stated_facts missed a covered-but-empty question")
gen = clean_set(prefilled={"dish": "al pastor tacos"})
expect(checks.check_stated_facts(gen, fx).verdict == "PASS",
       "stated_facts fired even though the extractor pre-filled the answer")
gen = clean_set()
gen.steps = [s for s in gen.middle() if s["field"] != "dish"] + _TAIL
expect(checks.check_stated_facts(gen, fx).verdict == "PASS",
       "stated_facts fired on a set that simply never asked the question")

section("8. tappable (§6) — a set with nothing tappable")
gen = clean_set()
gen.steps = [{**s, "kind": "text", "options": []} for s in gen.middle()] + _TAIL
expect(checks.check_tappable(gen, clean_fx()).verdict == "SOFT_FAIL",
       "tappable missed an all-free-text set")

section("9. no_duplicate (§6) — a repeated question and a repeated field key")
gen = with_step({"field": "dish2", "label": "X", "question": "What should they order?",
                 "placeholder": "x"})
expect(checks.check_no_duplicate(gen, clean_fx()).verdict == "SOFT_FAIL",
       "no_duplicate missed a verbatim repeat")
gen = with_step({"field": "dish", "label": "X", "question": "Anything to add on the order?",
                 "placeholder": "x"})
expect(checks.check_no_duplicate(gen, clean_fx()).verdict == "SOFT_FAIL",
       "no_duplicate missed a repeated field key")

section("10. placeholders (§6) and set_size (§7)")
gen = clean_set()
gen.steps = [{**s, "placeholder": ""} for s in gen.middle()] + _TAIL
expect(checks.check_placeholders(gen, clean_fx()).verdict == "SOFT_FAIL",
       "placeholders missed a set with no example answers")
gen = clean_set()
gen.steps = gen.middle()[:2] + _TAIL
expect(checks.check_set_size(gen, clean_fx()).verdict == "SOFT_FAIL",
       "set_size missed a 2-question set")
gen = clean_set()
gen.steps = [dict(s, field=f"f{i}") for i, s in enumerate(gen.middle() * 4)] + _TAIL
expect(checks.check_set_size(gen, clean_fx()).verdict == "HARD_FAIL",
       "set_size missed a 20-question interrogation")

section("11. language (§9) and reco_type")
gen = clean_set()  # English questions
expect(checks.check_language(gen, clean_fx(lang="Spanish")).verdict == "SOFT_FAIL",
       "language missed an English set handed to a Spanish speaker")
gen = clean_set()
gen.steps = [{**s, "question": "¿Qué deberían pedir?"} for s in gen.middle()] + _TAIL
expect(checks.check_language(gen, clean_fx(lang="Spanish")).verdict == "PASS",
       "language FALSE POSITIVE on a genuinely Spanish set")
# A Spanish-NAMED subject in ENGLISH questions must not read as a translated set. §2 puts the
# subject's name in most questions, and the marker set contains bare `los`/`las`/`del`/`una`
# plus the accent class — so the name alone used to clear the 50% bar and report an entirely
# English set as §9-compliant.
_EN = [{**s, "question": q} for s, q in zip(clean_set().middle(), [
    "What does Dra. Ruiz del Los Santos treat?",
    "What did Dra. Ruiz del Los Santos help you with?",
    "Which ages does Dra. Ruiz del Los Santos see?",
    "Does Dra. Ruiz del Los Santos take walk-ins?",
    "How do neighbours reach Dra. Ruiz del Los Santos?",
])]
gen = GeneratedSet(steps=_EN + _TAIL, reco_type="restaurant", raw_steps=_EN,
                   generated=True, prefill_measured=True)
fx = clean_fx(lang="Spanish", prior_draft={"name": "Dra. Ruiz del Los Santos"})
r = checks.check_language(gen, fx)
expect(r.verdict == "SOFT_FAIL",
       f"language passed an ENGLISH set as Spanish on the strength of the subject's own "
       f"name — the name is the neighbour's word, not evidence Lana translated: {r.detail}")

section("11b. stated_facts needles must cover the question, not merely appear inside it")
# A question that embeds a needle while asking something genuinely NEW was SOFT_FAILed as
# "the neighbour will be asked to repeat themselves".
_fx = clean_fx(stated_facts=[{"about": "dogs allowed",
                              "question_any_of": ["are dogs allowed"]}])
gen = with_step({"field": "offleash", "label": "O",
                 "question": "Are dogs allowed off-leash on the back loop?",
                 "placeholder": "yes past the gate"})
expect(checks.check_stated_facts(gen, _fx).verdict == "PASS",
       "stated_facts FALSE POSITIVE: the question embeds the needle but asks something the "
       "opening line never answered")
gen = with_step({"field": "dogs", "label": "D", "question": "Are dogs allowed?",
                 "placeholder": "yes"})
expect(checks.check_stated_facts(gen, _fx).verdict == "SOFT_FAIL",
       "stated_facts stopped catching a question that IS the already-stated fact")
expect(checks.check_reco_type(clean_set(reco_type="taqueria"), clean_fx()).verdict == "HARD_FAIL",
       "reco_type accepted a type the DB's check constraint would reject")
expect(checks.check_reco_type(clean_set(reco_type="location"), clean_fx()).verdict == "SOFT_FAIL",
       "reco_type missed a type that disagrees with the fixture")

section("12. generation_ran — a static fallback where a written set was expected")
gen = clean_set(generated=False)
expect(checks.check_generation_ran(gen, clean_fx()).verdict == "HARD_FAIL",
       "generation_ran missed a silent fallback to the static set")
expect(checks.check_generation_ran(gen, clean_fx(expect_generated=False)).verdict == "PASS",
       "generation_ran failed the fixture that deliberately pins the fallback")

section("13. a backend error is a HARD_FAIL, never an empty pass")
r = checks.run_question_checks(GeneratedSet(error="boom"), clean_fx())
expect(len(r) == 1 and r[0].verdict == "HARD_FAIL", "a backend error did not HARD_FAIL")
r = checks.run_question_checks(GeneratedSet(), clean_fx())
expect(len(r) == 1 and r[0].verdict == "UNSCORED", "an empty set did not come back UNSCORED")

# ===========================================================================
section("14. Arm B — the shipped validator does what the product does, and no more")
V = ShippedValidator()


def afx(**over) -> AnswerFixture:
    base = dict(id="t", reco_type="restaurant", field_name="price",
                question="What's the price range?", kind="text", required=False,
                answer="hundred dollars", verdict="reject", reason="x")
    base.update(over)
    return AnswerFixture(**base)  # type: ignore[arg-type]


expect(V.validate(afx(answer="hundred dollars")).accepted,
       "the shipped validator rejected junk — it has no semantic check, so it must accept it")
expect(V.validate(afx(answer="ask me", field_name="contact", required=True)).accepted,
       "§11: 'ask me' passes the non-blank must-have check today; the mirror must reproduce that")
expect(not V.validate(afx(answer="")).accepted, "the shipped validator accepted an empty answer")
expect(not V.validate(afx(answer="   ")).accepted,
       "whitespace should collapse to empty and be dropped (main.py:2543)")
expect(not V.validate(afx(answer="", required=True)).accepted,
       "a blank REQUIRED answer must be refused (missing_required)")

section("14b. checks scores a wrong-accept and a wrong-reject differently")
r = checks.check_answer_decision(V.validate(afx(answer="idk")), afx(answer="idk", verdict="reject"))
expect(r.verdict == "SOFT_FAIL", f"accepted junk should be SOFT_FAIL, got {r.verdict}")
from ports import AnswerDecision  # noqa: E402

r = checks.check_answer_decision(AnswerDecision(accepted=False, stored="yes lol"),
                                 afx(answer="yes lol", verdict="accept"))
expect(r.verdict == "HARD_FAIL",
       f"rejecting a GOOD answer must be HARD_FAIL (§12.4), got {r.verdict}")
r = checks.check_answer_decision(AnswerDecision(accepted=False, stored=None, unscorable=True),
                                 afx())
expect(r.verdict == "UNSCORED", "an undecided validator must be UNSCORED, never a pass")

section("14c. truncation is scored separately from quality")
long_answer = "x" * 400
r = checks.check_truncation(V.validate(afx(answer=long_answer, verdict="accept")),
                            afx(answer=long_answer, verdict="accept"))
expect(r.verdict == "SOFT_FAIL", "truncation missed a 400-char answer cut to 280")
expect(checks.check_truncation(V.validate(afx(answer="fine")),
                               afx(answer="fine")).verdict == "PASS",
       "truncation FALSE POSITIVE on a short answer")

section("14d. reachable isolates §11's plumber case — and the bigger half of it")
r = checks.check_reachable(V.validate(afx(answer="ask me", field_name="contact",
                                                   required=False)),
                                    afx(answer="ask me", field_name="contact", required=False,
                                        verdict="reject"))
expect(r.verdict == "SOFT_FAIL",
       "reachable missed an unreachable contact — and `contact` is NOT required "
       "(reco_question_sets.py:367-368), which is the half §11 understates")
expect(checks.check_reachable(
    V.validate(afx(answer="555-0134", field_name="contact", required=False, verdict="accept")),
    afx(answer="555-0134", field_name="contact", required=False, verdict="accept"),
).verdict == "PASS", "reachable FALSE POSITIVE on a real phone number")

# ===========================================================================
section("15. DRIFT TRIPWIRES — the values this harness copies from the product")

try:
    from app.reco_question_sets import _FLOOR as REAL_FLOOR  # type: ignore[attr-defined]
    from app.reco_question_sets import TAIL_FIELDS as REAL_TAIL
    from app.reco_question_sets import tail_steps

    real_consent = next((s["question"] for s in tail_steps(()) if s["field"] == "ask_ok"), None)
    expect(real_consent == CONSENT_QUESTION,
           f"CONSENT DRIFT: the product now asks {real_consent!r}, ports.CONSENT_QUESTION says "
           f"{CONSENT_QUESTION!r}. §5 calls this wording a guarantee — confirm the change was "
           f"deliberate, then update ports.py.")
    expect(dict(REAL_FLOOR) == checks._FLOOR_MIRROR,
           f"FLOOR DRIFT: product={dict(REAL_FLOOR)} vs checks._FLOOR_MIRROR="
           f"{checks._FLOOR_MIRROR}. check_floor_present is scoring against a stale mirror.")
    expect(tuple(REAL_TAIL) == ("ask_ok", "others_also_said"),
           f"TAIL DRIFT: the product's TAIL_FIELDS is now {REAL_TAIL}")

    # An empty agree row must be OMITTED, not shown empty (§5) — the product's own behaviour.
    expect(all(s["field"] != "others_also_said" for s in tail_steps(())),
           "the product now emits `others_also_said` with no tallies — §5 calls that a dead card")
    expect(any(s["field"] == "others_also_said" for s in tail_steps([{"attr": "easy parking", "n": 2}])),
           "the product no longer emits the agree row even when tallies exist")

    # The 280-char transform, asserted against the literal in main.py rather than trusted.
    main_src = (_HERE.parents[1] / "app" / "main.py").read_text(encoding="utf-8", errors="replace")
    expect('" ".join(str(value or "").split())[:280]' in main_src,
           "STORAGE DRIFT: main.py no longer does `\" \".join(str(value or \"\").split())[:280]` "
           "— validators.store_as is mirroring a rule that has changed")
except ImportError as exc:
    print(f"  [skip] product not importable ({exc}) — drift tripwires not run")

section("16. store_as reproduces the flow's storage semantics")
expect(store_as("  a   b  ") == "a b", "store_as did not collapse whitespace")
expect(store_as("   ") is None, "store_as did not drop an all-whitespace answer")
expect(store_as("x" * 400) == "x" * 280, "store_as did not truncate at 280")
expect(store_as(None) is None, "store_as did not handle None")

# ===========================================================================
section("17. fixtures.yaml loads, validates, and its validator is not vacuous")
qs, ans, raw = run_eval.load_fixtures()
expect(run_eval.validate_fixtures(raw) == [], f"fixtures.yaml does not validate: "
                                              f"{run_eval.validate_fixtures(raw)}")
expect(len(qs) >= 8, f"only {len(qs)} question fixtures — thin coverage of seven types")
expect(len(ans) >= 15, f"only {len(ans)} answer fixtures")
expect({a.verdict for a in ans} == {"accept", "reject"},
       "the answer fixtures must carry BOTH labels, or 'accept everything' scores perfectly")
expect(len({q.expect_reco_type for q in qs if q.expect_reco_type}) >= 6,
       "the question fixtures cover fewer than 6 of the 7 reco types")

for bad, why in (
    ({"questions": [{"id": "x", "opening_line": "", "expect_reco_type": "recipe"}]},
     "empty opening_line"),
    ({"questions": [{"id": "x", "opening_line": "a", "expect_reco_type": "taqueria"}]},
     "a reco_type the DB rejects"),
    ({"questions": [{"id": "x", "opening_line": "a", "lookup_able": True}]},
     "lookup_able with no type (google_answerable could not find the floor exemption)"),
    ({"questions": [{"id": "x", "opening_line": "a", "expect_reco_type": "recipe",
                     "prior_draft": {"step_set": []}}]},
     "prior_draft carrying step_set (generation would never run)"),
    ({"questions": [{"id": "x", "opening_line": "a", "expect_reco_type": "recipe",
                     "stated_facts": [{"about": "y", "question_any_of": []}]}]},
     "an inert matcher with no needles"),
    ({"answers": [{"id": "y", "reco_type": "recipe", "field": "f", "question": "q?",
                   "kind": "text", "answer": "a", "verdict": "maybe", "reason": "r"}]},
     "a verdict that is neither accept nor reject"),
    ({"answers": [{"id": "y", "reco_type": "recipe", "field": "f", "question": "q?",
                   "kind": "text", "answer": "a", "verdict": "reject", "reason": ""}]},
     "a hand-label with no auditable reason"),
    ({"answers": [{"id": "y", "reco_type": "recipe", "field": "f", "question": "q?",
                   "kind": "text", "answer": "a", "verdict": "reject", "reason": "r"}]},
     "only one label in the whole file"),
):
    expect(run_eval.validate_fixtures(bad) != [], f"validate_fixtures accepted {why}")

expect(run_eval.validate_fixtures(
    {"questions": [{"id": "a", "opening_line": "x", "expect_reco_type": "recipe"}],
     "answers": [{"id": "b", "reco_type": "recipe", "field": "f", "question": "q?",
                  "kind": "text", "answer": "a", "verdict": "accept", "reason": "r"},
                 {"id": "c", "reco_type": "recipe", "field": "f", "question": "q?",
                  "kind": "text", "answer": "b", "verdict": "reject", "reason": "r"}]}) == [],
    "validate_fixtures rejected a valid minimal file — it cries wolf")

# ===========================================================================
section("16b. every axis states its pass rule, and no rule outlives its check")
# The report's appendix is generated from checks.PASS_RULES. If an axis can be added without an
# entry, the appendix silently stops describing the thing it claims to describe — and a reader
# auditing a number finds nothing. If an entry outlives its check, the appendix documents a rule
# nothing enforces. Both directions are asserted.
_JUDGED = {"filterable", "lazy_vs_good", "subject_tailored"}
_implemented = ({fn.__name__.replace("check_", "") for fn in checks.ARM_A_CHECKS}
                | {fn.__name__.replace("check_", "") for fn in checks.ARM_B_CHECKS}
                | _JUDGED)
_documented = set(checks.PASS_RULES)
for axis in sorted(_implemented - _documented):
    expect(False, f"axis `{axis}` has no entry in checks.PASS_RULES — the report's appendix "
                  f"would not describe it")
for axis in sorted(_documented - _implemented):
    expect(False, f"checks.PASS_RULES documents `{axis}`, which no longer exists — the "
                  f"appendix would describe a rule nothing enforces")
expect(_implemented == _documented,
       f"PASS_RULES and the implemented axes disagree: "
       f"missing={sorted(_implemented - _documented)} stale={sorted(_documented - _implemented)}")
for axis, entry in checks.PASS_RULES.items():
    expect(len(entry) == 3 and all(str(x).strip() for x in entry),
           f"PASS_RULES[{axis!r}] is incomplete — it needs (what passes, how, severity)")
print(f"  {len(_documented)} axes, all with a stated pass rule")

section("17a. the fallback-rate confidence interval is honest")
# The headline Arm A number. Two runs of 30 returned 13/30 and 11/30 for the same quantity, so
# the report must publish an interval, not a point — otherwise the next run's movement reads as
# an improvement when it is sampling.
_lo, _hi = run_eval.wilson_ci(13, 30)
expect(_lo < 13 / 30 < _hi, "wilson_ci does not bracket the observed proportion")
expect(_hi - _lo > 0.25, f"CI at n=30 is implausibly tight ({_lo:.2f}-{_hi:.2f}) — the whole "
                         f"point is that n=30 cannot pin this number")
expect(run_eval.wilson_ci(11, 30)[0] < run_eval.wilson_ci(13, 30)[1],
       "the 11/30 and 13/30 intervals do not overlap — if this ever holds, the two runs really "
       "did differ and the noise explanation is wrong")
_lo2, _hi2 = run_eval.wilson_ci(27, 67)          # the three runs pooled
expect(_hi2 - _lo2 < _hi - _lo, "pooling more trials did not narrow the interval")
expect(run_eval.wilson_ci(0, 0) == (0.0, 0.0), "wilson_ci divides by zero on an empty sample")
for k, n in ((0, 30), (30, 30)):
    lo, hi = run_eval.wilson_ci(k, n)
    expect(0.0 <= lo <= hi <= 1.0, f"wilson_ci({k},{n}) escaped [0,1]: {lo}-{hi}")

section("17c. the reference validator is not told the answers to its own test")
# An earlier prompt quoted eight of the 21 hand-labelled answers verbatim, so its score was a
# lookup. Any example in a validator's prompt that is also a fixture answer contaminates the
# measurement; this asserts the two stay disjoint.
from validators import _DEAD_ANSWERS as _DA  # noqa: E402
from validators import _REFERENCE_SYSTEM as _RS  # noqa: E402

_leak = [a.id for a in ans
         if a.answer.strip() and a.answer.strip().lower() in _RS.lower()]
expect(not _leak,
       f"CONTAMINATION: the reference validator's system prompt names these fixture answers "
       f"verbatim, so its score on them is a lookup, not a judgment: {_leak}")
# The deterministic floor is legitimate — a real validator would have one — but it decides
# rows without consulting the model, so its reach has to stay visible and small.
_floor = [a.id for a in ans
          if (store_as(a.answer) or "").strip().lower().rstrip(".!") in _DA]
expect(len(_floor) <= 3,
       f"{len(_floor)} fixtures are decided by the _DEAD_ANSWERS wordlist rather than by the "
       f"model ({_floor}). Past a couple, the reference validator is scoring a lexicon that "
       f"was written while looking at this file.")
print(f"  prompt-fixture overlap: {len(_leak)}   decided by wordlist floor: {len(_floor)} {_floor}")

section("17b. no naive heuristic can solve the answer labels")
# If a two-line heuristic scored perfectly, Arm B would be measuring string length, not meaning,
# and the reference validator's score would say nothing. This asserts the fixture set is not
# trivially separable — INCLUDING against a wordlist built by reading the fixture file itself,
# which is the strongest cheat available and still cannot get there.
_bl = run_eval.baseline_table(ans)
expect(len(_bl) >= 5, "baseline_table returned almost nothing — the honesty check is inert")
for b in _bl:
    solved = b["junk_accepted"] == 0 and b["good_rejected"] == 0
    expect(not solved,
           f"NAIVE BASELINE SOLVES THE FIXTURE SET: {b['name']!r} accepted "
           f"{b['junk_accepted']}/{b['junk']} junk and rejected {b['good_rejected']}/{b['good']} "
           f"good. Arm B is then measuring a string property, not meaning — add harder rows.")
_best = max(b["accuracy"] for b in _bl)
expect(_best < 0.95, f"best naive baseline reaches {_best:.0%} accuracy — too close to solved")
print(f"  best naive baseline: {_best:.0%} accuracy; none reaches 0 junk + 0 false-reject")

# ===========================================================================
section("18. non-vacuity against a FROZEN known-bad set (not against live product code)")
# WHY THIS IS FROZEN, 2026-09-11. This section used to assert that reco_question_sets' STATIC
# sets trip banned_generic and google_answerable — which they did, because they contained
# "What stood out for you?" and, for `location`, an opening-hours question.
#
# Asjid then fixed them (e265309): colour questions removed, hours removed, chips added. The
# assertion started failing, correctly — and that exposed a design error of mine. A
# non-vacuity proof that depends on the PRODUCT STAYING BROKEN evaporates the moment someone
# fixes it, exactly when you still need the check to be proven live.
#
# So the known-bad set is now a frozen artefact this harness owns. It is a verbatim snapshot
# of the pre-fix table, kept solely to prove the checks still fire.
_FROZEN_BAD = [
    {"field": "profession", "label": "Profession", "question": "What do they do?",
     "kind": "text", "required": True},
    {"field": "helped_with", "label": "Helped with", "question": "What did they help you with?",
     "kind": "text", "required": True},
    {"field": "hours", "label": "Hours", "question": "When is it open — and the best time to go?",
     "kind": "text", "required": False},
    {"field": "liked", "label": "Liked", "question": "What did you like about them?",
     "kind": "text", "required": False},
    {"field": "stood_out", "label": "Stood out", "question": "What stood out for you?",
     "kind": "text", "required": False},
]
_frozen = GeneratedSet(steps=_FROZEN_BAD + _TAIL, reco_type="location", raw_steps=_FROZEN_BAD,
                       generated=True, prefill_measured=True)
_ffx = clean_fx(expect_reco_type="location", lookup_able=True)
expect(checks.check_banned_generic(_frozen, _ffx).verdict == "HARD_FAIL",
       "banned_generic no longer fires on the frozen known-bad set — the check has gone vacuous")
expect(checks.check_google_answerable(_frozen, _ffx).verdict == "SOFT_FAIL",
       "google_answerable no longer fires on the frozen known-bad set — check gone vacuous")
print("  frozen known-bad set trips banned_generic and google_answerable")

section("18b. the live static sets — reported, never asserted")
from stub_impl import StaticQuestionSets  # noqa: E402

try:
    static = StaticQuestionSets()
    hits = {"banned_generic": 0, "google_answerable": 0}
    for fx in qs:
        gen = static.generate(fx)
        if gen.error:
            continue
        for c in checks.run_question_checks(gen, fx):
            if c.name in hits and c.verdict != "PASS":
                hits[c.name] += 1
    # REPORTED, NOT ASSERTED. These are live product tables; the day they stop tripping a
    # check is good news, not a harness failure. Non-vacuity is proven above against the
    # frozen set instead.
    print(f"  live static sets: banned_generic fires on {hits['banned_generic']} fixtures, "
          f"google_answerable on {hits['google_answerable']}"
          + ("  <- both clean: the fallback path now obeys §6" if not any(hits.values()) else ""))
except Exception as exc:  # noqa: BLE001
    print(f"  [skip] static control not runnable ({exc})")

# ===========================================================================
print("\n" + "=" * 72)
print(f"{_PASSED} assertions passed, {len(_FAILED)} failed")
if _FAILED:
    print("\nFAILURES:")
    for f in _FAILED:
        print(f"  - {f}")
print("=" * 72)
raise SystemExit(1 if _FAILED else 0)
