"""Question sets for the recommendation capture (the C-4-RECO mock).

One recommendation is NOT one question set: a dentist, a recipe and a sound machine need
different things asked. Lana GENERATES the set per recommendation (see
`validate_steps` + the `steps` key of the tip_share extractor) — a recipe gets asked about
taste and difficulty, a night light about what it fixed, and the placeholder under each
question is an example for THAT subject. The set drives the carousel steps the FE renders
(C-4-EVENT-P2B) and the fields the ready card shows (C-4-RECO-P3).

_SETS below is what survives of the static version and does three jobs: the per-type FLOOR
(fields a reader of that type needs whatever the model felt like asking), the fallback set
when generation fails or the LLM is unconfigured, and the worked examples the prompt is
grounded on. Type is still fixed taxonomy — seven values, indexed, "all the recipes near
me" browses on it. Only the QUESTIONS are dynamic.

Typing rule (settled with product): types split by what the neighbor is meant to DO with it,
never by topic — go eat there = restaurant, cook it = recipe, buy it = product, go spend
time there = location. That keeps "great biryani at Zaiqa" / "here's my biryani recipe" /
"their frozen biryani packs" from collapsing into one bucket.

# ponytail: copy in code, one deploy per wording change. Move to a table only if the
# wording keeps churning after launch (an admin editor is ~2 days).
"""

from __future__ import annotations

import re
from typing import Any

# field: the snake_case key the answer lands under, stable across wording changes.
# required: the flow will not reach the ready card without it. Kept in step with the first
# two entries of _FLOOR below (the only notion of required a generated set has), so the
# fallback set and a generated set gate on the same two answers.
_SETS: dict[str, list[dict[str, Any]]] = {
    "professional": [
        {"field": "profession", "label": "Profession", "question": "What do they do?", "required": True},
        {"field": "helped_with", "label": "Helped with", "question": "What did they help you with?", "required": True},
        {"field": "where", "label": "Location", "question": "Where are they based?"},
        {"field": "contact", "label": "Contact", "question": "How do neighbours reach them?"},
        {"field": "liked", "label": "Liked", "question": "What did you like about them?"},
        {"field": "stood_out", "label": "Stood out", "question": "What stood out for you?"},
        {"field": "best_for", "label": "Best for", "question": "Who are they best for?",
         "options": ["Kids", "Adults", "Seniors", "Anyone"]},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a neighbour should know before going?"},
    ],
    "restaurant": [
        {"field": "dish", "label": "Order this", "question": "What should they order?", "required": True},
        {"field": "where", "label": "Location", "question": "Where can neighbours find it?", "required": True},
        {"field": "cuisine", "label": "Cuisine", "question": "What kind of food is it?"},
        {"field": "price", "label": "Price", "question": "Roughly what does a meal run?",
         "options": ["Cheap eats", "Mid-range", "A treat"]},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?",
         "options": ["Families", "Date night", "Groups", "Quick solo bite"]},
        {"field": "liked", "label": "Liked", "question": "What did you like about it?"},
        {"field": "stood_out", "label": "Stood out", "question": "What stood out for you?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before going — waits, parking, hours?"},
    ],
    "recipe": [
        {"field": "recipe", "label": "Recipe", "question": "What is the recipe?", "required": True},
        {"field": "ingredients", "label": "Ingredients", "question": "What goes into it?", "required": True},
        {"field": "why", "label": "Why this one", "question": "Why is this one worth sharing?"},
        {"field": "steps", "label": "How to make it", "question": "How do you make it?"},
        {"field": "time", "label": "Cooks in", "question": "How long does it take?"},
        {"field": "serves", "label": "Serves", "question": "How many does it feed?"},
        {"field": "best_for", "label": "Best for", "question": "When is it good for?",
         "options": ["Weeknight", "Sunday lunch", "A crowd", "Freezer batch"]},
        {"field": "good_to_know", "label": "Good to know", "question": "Any tip that makes it work?"},
    ],
    "product": [
        {"field": "used_for", "label": "Used for", "question": "What is it used for?", "required": True},
        {"field": "where_to_buy", "label": "Where to buy", "question": "Where can neighbours get it?", "required": True},
        {"field": "why", "label": "Why this one", "question": "Why this one over the others?"},
        {"field": "price", "label": "Price", "question": "Roughly what does it cost?"},
        {"field": "setup", "label": "Sets up in", "question": "How much effort is it to get going?"},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before buying?"},
    ],
    "location": [
        {"field": "known_for", "label": "Known for", "question": "What is the place known for?", "required": True},
        {"field": "where", "label": "Location", "question": "Where is it?", "required": True},
        {"field": "why", "label": "Why go", "question": "Why is it worth going?"},
        {"field": "hours", "label": "Hours", "question": "When is it open — and the best time to go?"},
        {"field": "cost", "label": "Cost", "question": "Does it cost anything?"},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?",
         "options": ["Families", "Kids", "Dogs", "Quiet time"]},
        {"field": "liked", "label": "Liked", "question": "What did you like about it?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before going — parking, crowds?"},
    ],
    "service": [
        {"field": "service", "label": "Service", "question": "What do they do?", "required": True},
        {"field": "helped_with", "label": "Helped with", "question": "What did they do for you?", "required": True},
        {"field": "contact", "label": "Contact", "question": "How do neighbours reach them?"},
        {"field": "price", "label": "Price", "question": "Roughly what did it cost?"},
        {"field": "reliability", "label": "Reliability", "question": "Did they show up when they said they would?"},
        {"field": "liked", "label": "Liked", "question": "What did you like about them?"},
        {"field": "stood_out", "label": "Stood out", "question": "What stood out for you?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a neighbour should know before hiring them?"},
    ],
    "diy": [
        {"field": "fixes", "label": "Fixes", "question": "What problem does it solve?", "required": True},
        {"field": "how", "label": "How to do it", "question": "How do you do it?", "required": True},
        {"field": "needs", "label": "You'll need", "question": "What do they need on hand?"},
        {"field": "time", "label": "Takes", "question": "How long does it take?"},
        {"field": "difficulty", "label": "Difficulty", "question": "How hard is it?",
         "options": ["Anyone can", "Some patience", "Handy only"]},
        {"field": "cost", "label": "Cost", "question": "Roughly what does it cost to do?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything that goes wrong the first time?"},
    ],
}

RECO_TYPES = tuple(_SETS)

# What the extractor is told each type MEANS. Kept next to the sets so a new type is one
# edit, not two files.
TYPE_RULES = (
    "professional = a named person you'd book (doctor, dentist, teacher, tutor, lawyer). "
    "service = a trade/help you'd hire (plumber, cleaner, handyman, nanny). "
    "restaurant = somewhere the neighbor would GO EAT. "
    "recipe = something the neighbor would COOK THEMSELVES. "
    "product = a thing the neighbor would BUY. "
    "location = somewhere the neighbor would VISIT or spend time (park, market, trail). "
    "diy = a how-to / trick the neighbor would DO themselves. "
    "Split by what the neighbor DOES with it, never by topic: food can be restaurant, "
    "recipe, product or location depending on that."
)


def normalize_type(raw: Any) -> str | None:
    """A known reco type, or None. Tolerates the plural/spacey shapes an LLM returns."""
    key = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key.endswith("s") and key[:-1] in _SETS:
        key = key[:-1]
    return key if key in _SETS else None


def steps_for(reco_type: Any) -> list[dict[str, Any]]:
    """The ordered carousel steps for a type ([] for an unknown type). Copies, so a caller
    stamping `answer` onto a step can't mutate the shared table."""
    return [dict(s) for s in _SETS.get(normalize_type(reco_type) or "", [])]


def _resolve(spec: Any) -> list[dict[str, Any]]:
    """Accepts either a generated step list (the normal path — what Lana wrote for THIS
    recommendation) or a reco_type (the static fallback set). One walk serves both, so
    nothing downstream has to know which one it got."""
    if isinstance(spec, list):
        return [dict(s) for s in spec]
    return steps_for(spec)


def carousel(spec: Any, answers: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The steps with each answer folded in, for the FE to render either fork of
    C-4-EVENT-P1B-FORK: the swipe carousel or the one-at-a-time side-Lana chat."""
    known = answers or {}
    out = _resolve(spec)
    for step in out:
        val = known.get(step["field"])
        if isinstance(val, str) and val.strip():
            step["answer"] = val.strip()
    return out


def next_question(
    spec: Any, answers: dict[str, Any] | None, asked: Any = ()
) -> dict[str, Any] | None:
    """The next step still worth asking, or None when nothing is outstanding.

    Required steps jump the queue and keep coming back until answered — the ready card
    can't be assembled without them. Optional steps are offered ONCE each, so `asked` has
    to be honoured here: without it the walk returns the same first unanswered optional
    every turn, the caller declines to re-ask it, and every step behind it is never reached.
    """
    seen = set(asked or ())
    unanswered = [s for s in carousel(spec, answers) if not s.get("answer")]
    required = next((s for s in unanswered if s.get("required")), None)
    if required:
        return required
    return next((s for s in unanswered if s["field"] not in seen), None)


def missing_required(spec: Any, answers: dict[str, Any] | None) -> list[str]:
    """Required fields with no answer — the flow can't post while this is non-empty."""
    return [s["field"] for s in carousel(spec, answers) if s.get("required") and not s.get("answer")]


# ── Generated sets ────────────────────────────────────────────────────────────────────
#
# The model writes the middle of the set; this file owns the two ends. What it owns and why:
#
#   FLOOR — the fields a reader of that type CANNOT act on the recommendation without. Left
#   to itself the model writes lovely colour ("what stood out?") and forgets the phone
#   number, so a plumber recommendation arrives unreachable. Floor fields are also the
#   required ones, so the ready card can't be assembled without them.
#
#   TAIL — the consent toggle and the agree row. Never model-generated: the consent question
#   gates whether neighbours may message the author, so it has to be worded identically for
#   everyone, and the agree row is real tallies of what other neighbours logged.
_FLOOR: dict[str, tuple[str, ...]] = {
    "professional": ("profession", "helped_with", "contact"),
    "service": ("service", "helped_with", "contact"),
    "restaurant": ("dish", "where"),
    "recipe": ("recipe", "ingredients"),
    "product": ("used_for", "where_to_buy"),
    "location": ("known_for", "where"),
    "diy": ("fixes", "how"),
}

# The floor, phrased for the prompt. Generated from _FLOOR so adding a type can't leave the
# prompt describing the old one.
FLOOR_RULES = "; ".join(
    f"{t} → " + " · ".join(f.replace("_", " ") for f in fields)
    for t, fields in _FLOOR.items()
)

TAIL_FIELDS = ("ask_ok", "others_also_said")

# Steps that are a POINT ON A MAP, answered with the Places picker instead of a text box: a
# park's "where is it?" typed by hand is a string nobody can navigate to, and the FE already
# has the picker the host flow's where-card uses.
#
# Deliberately NOT `where_to_buy` — "Amazon · ~$60" is the right answer to that one, and a
# map search would send the user hunting for a warehouse. Field names only, with no
# dependency on place_based: a dentist's clinic and a park are both findable, and `service`
# (a plumber with no fixed address) has no `where` step in its floor to begin with.
_PLACE_FIELDS = frozenset({"where", "location", "address", "where_to_find", "venue"})

# 8 generated + up to 3 floor + 2 tail lands on the mock's 8-10 steps.
_MAX_GENERATED = 8
_MAX_MIDDLE = 10

# Questions the model must never ask. Not an intent matcher — a disclosure floor: past this
# line the answer stops being a recommendation and starts being somebody's private data,
# posted to a whole block by someone who isn't them.
_BLOCKED_ASK = re.compile(
    r"home address|full name|social security|\bssn\b|date of birth|birthday|"
    r"password|license plate|credit card|how much (?:do|did) (?:you|they) (?:earn|make)",
    re.IGNORECASE,
)


def _slug(raw: Any) -> str:
    """A stable snake_case field key from whatever the model called it. The key is what the
    answer is stored under, so it must not carry the wording — re-generating a set with
    nicer copy has to leave the answers findable."""
    return re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")[:32]


def _kind_for(field: str, options: Any = ()) -> str:
    """Which control answers this step: a map search, a chip row, or a text box."""
    if field in _PLACE_FIELDS:
        return "place"
    return "choice" if len(options or []) >= 2 else "text"


def tail_steps(tallies: Any = ()) -> list[dict[str, Any]]:
    """The two closing steps of every set (mock steps 9/10 and 7/8).

    `others_also_said` is appended ONLY when neighbours have actually logged something on
    this subject — an empty agree row is a dead card, and the step count is dynamic anyway.
    """
    out: list[dict[str, Any]] = [
        {
            "field": "ask_ok",
            "label": "Neighbours",
            "question": "Can neighbours ask you more?",
            "kind": "toggle",
            "options": ["Let them ask", "Keep it to the card"],
        }
    ]
    # "easy parking ×2" — count in the label, so the agree row needs no second field on the
    # wire. ponytail: promote to {attr, n} objects if the FE wants to style the count.
    opts = [
        f"{str(t.get('attr') or '').strip()} ×{max(1, int(t.get('n') or 1))}"
        for t in (tallies or [])
        if isinstance(t, dict) and str(t.get("attr") or "").strip()
    ][:6]
    if opts:
        out.append(
            {
                "field": "others_also_said",
                "label": "Others also said",
                "question": "Others also said — tap the ones you'd agree with.",
                "kind": "agree",
                "options": opts,
            }
        )
    for step in out:
        step["required"] = False
    return out


def validate_steps(
    raw: Any, reco_type: Any, *, tallies: Any = ()
) -> list[dict[str, Any]]:
    """A model-written question set, made safe to ask: [] for an unknown type.

    Everything here is a guard against a specific way a generated set goes wrong — junk
    fields, a duplicate question, a statement instead of a question, a private ask, a
    35-step interrogation, a set that forgot the phone number, a model that helpfully
    re-invented the consent toggle in its own words.
    """
    rtype = normalize_type(reco_type)
    if not rtype:
        return []
    fallback = {s["field"]: s for s in steps_for(rtype)}
    middle: list[dict[str, Any]] = []
    seen: set[str] = set(TAIL_FIELDS)  # the tail is ours; a generated copy is dropped
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        field = _slug(item.get("field"))
        question = " ".join(str(item.get("question") or "").split())
        if not field or field in seen or not question.endswith("?"):
            continue
        if _BLOCKED_ASK.search(question):
            continue
        opts = [
            " ".join(str(o).split())
            for o in (item.get("options") or [])
            if isinstance(o, str) and str(o).strip()
        ][:4]
        step: dict[str, Any] = {
            "field": field,
            "label": " ".join(str(item.get("label") or field.replace("_", " ")).split())[:24],
            "question": question[:140],
            "kind": _kind_for(field, opts),
        }
        if opts:
            step["options"] = opts
        placeholder = " ".join(str(item.get("placeholder") or "").split())
        if placeholder:
            step["placeholder"] = placeholder[:80]
        middle.append(step)
        seen.add(field)
        if len(middle) >= _MAX_GENERATED:
            break

    if not middle:
        # Generation failed, returned junk, or the LLM is unconfigured — the static set for
        # the type still asks the right things, just not tailored to this subject.
        middle = [
            {**s, "kind": _kind_for(s["field"], s.get("options"))}
            for s in fallback.values()
        ]

    # The model's ORDER stands — it put "What does she do?" first and location before the
    # phone number for a reason, and second-guessing that is how the carousel starts asking
    # for a phone number before it has said who she is. Only the floor fields it FORGOT are
    # inserted, and they go next to the other basics rather than at the end: they are asked
    # early or, on an early "that's it", never.
    floor = _FLOOR.get(rtype, ())
    have = {s["field"] for s in middle}
    for field in floor:
        if field in have or field not in fallback:
            continue
        step = dict(fallback[field])
        step["kind"] = _kind_for(field, step.get("options"))
        last_basic = max(
            (i for i, s in enumerate(middle) if s["field"] in floor), default=-1
        )
        middle.insert(last_basic + 1, step)
        have.add(field)

    # Required = the type's first two floor fields. Not "the first two steps": a model that
    # leads with colour would make colour required and leave `contact` optional, which is
    # the exact failure the floor exists for.
    floor_present = [f for f in floor if f in have][:2]
    required = set(floor_present) or {s["field"] for s in middle[:2]}
    for step in middle:
        step["required"] = step["field"] in required

    return middle[:_MAX_MIDDLE] + tail_steps(tallies)
