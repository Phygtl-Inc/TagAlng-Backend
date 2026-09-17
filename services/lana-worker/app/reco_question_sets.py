"""Question sets for the recommendation capture (the C-4-RECO mock).

One recommendation is NOT one question set: a dentist, a recipe and a sound machine need
different things asked. Lana GENERATES the set per recommendation (see
`validate_steps` + the `steps` key of the tip_share extractor) — a recipe gets asked about
taste and difficulty, a night light about what it fixed, and the placeholder under each
question is an example for THAT subject. The set drives the carousel steps the FE renders
(C-4-EVENT-P2B) and the fields the ready card shows (C-4-RECO-P3).

Every step here obeys the same two rules the generated ones do, because this table is not
only the failure path — the flip-through carousel runs on it until the subject is answered,
and a neighbour flipping nine text boxes that ask "what stood out?" is the mock we replaced.
So: no colour questions (the `liked` / `stood_out` / `why` steps are gone — they are banned
in the prompt, and a table that asks them anyway is the prompt's own counter-example), and
`options` on everything but the basics, since a chip row is a filter and a paragraph is not.

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

import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

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
        {"field": "ages", "label": "Sees", "question": "Which ages do they see?",
         "options": ["Babies & kids", "Teens", "Adults", "Any age"]},
        {"field": "wait", "label": "Wait", "question": "How long was the wait for an appointment?",
         "options": ["Same week", "A few weeks", "Over a month"]},
        {"field": "best_for", "label": "Best for", "question": "Who are they best for?",
         "options": ["Kids", "Adults", "Seniors", "Anyone"], "multi": True},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a neighbour should know before going?"},
    ],
    "restaurant": [
        {"field": "dish", "label": "Order this", "question": "What should they order?", "required": True},
        {"field": "where", "label": "Location", "question": "Where can neighbours find it?", "required": True},
        {"field": "cuisine", "label": "Cuisine", "question": "What kind of food is it?"},
        {"field": "price", "label": "Price", "question": "Roughly what does a meal run?",
         "options": ["Cheap eats", "Mid-range", "A treat"]},
        {"field": "wait", "label": "Wait", "question": "How long is the wait at its busiest?",
         "options": ["Walk right in", "10-20 minutes", "Worth booking"]},
        {"field": "kids", "label": "With kids", "question": "How is it with kids?",
         "options": ["High chairs & kids' menu", "Fine for kids", "Better without"]},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?",
         "options": ["Families", "Date night", "Groups", "Quick solo bite"], "multi": True},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before going?"},
    ],
    "recipe": [
        {"field": "recipe", "label": "Recipe", "question": "What is the recipe?", "required": True},
        {"field": "ingredients", "label": "Ingredients", "question": "What goes into it?", "required": True},
        {"field": "steps", "label": "How to make it", "question": "How do you make it?"},
        {"field": "time", "label": "Cooks in", "question": "How long does it take?",
         "options": ["Under 30 minutes", "About an hour", "A slow one"]},
        {"field": "serves", "label": "Serves", "question": "How many does it feed?",
         "options": ["1-2", "3-4", "5-6", "A crowd"]},
        {"field": "difficulty", "label": "Difficulty", "question": "How hard is it?",
         "options": ["Anyone can", "Some practice", "Confident cook"]},
        {"field": "best_for", "label": "Best for", "question": "When is it good for?",
         "options": ["Weeknight", "Sunday lunch", "A crowd", "Freezer batch"], "multi": True},
        {"field": "good_to_know", "label": "Good to know", "question": "Any tip that makes it work?"},
    ],
    "product": [
        {"field": "used_for", "label": "Used for", "question": "What is it used for?", "required": True},
        {"field": "where_to_buy", "label": "Where to buy", "question": "Where can neighbours get it?", "required": True},
        {"field": "price", "label": "Price", "question": "Roughly what does it cost?",
         "options": ["Under $25", "$25-100", "$100-300", "$300+"]},
        {"field": "setup", "label": "Sets up in", "question": "How much effort is it to get going?",
         "options": ["Out of the box", "Ten minutes", "A bit of a project"]},
        {"field": "lasted", "label": "Held up", "question": "How has it held up?",
         "options": ["Still like new", "Fine so far", "Shows wear"]},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before buying?"},
    ],
    "location": [
        {"field": "known_for", "label": "Known for", "question": "What is the place known for?", "required": True},
        {"field": "where", "label": "Location", "question": "Where is it?", "required": True},
        {"field": "cost", "label": "Cost", "question": "Does it cost anything?",
         "options": ["Free", "A few dollars", "Ticketed"]},
        {"field": "crowds", "label": "Crowds", "question": "How busy does it get?",
         "options": ["Quiet most times", "Busy at weekends", "Packed"]},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?",
         "options": ["Families", "Kids", "Dogs", "Quiet time"], "multi": True},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before going?"},
    ],
    "service": [
        {"field": "service", "label": "Service", "question": "What do they do?", "required": True},
        {"field": "helped_with", "label": "Helped with", "question": "What did they do for you?", "required": True},
        {"field": "contact", "label": "Contact", "question": "How do neighbours reach them?"},
        {"field": "price", "label": "Price", "question": "Roughly what did it cost?",
         "options": ["Under $100", "$100-500", "$500+"]},
        {"field": "reliability", "label": "Reliability", "question": "Did they show up when they said they would?",
         "options": ["Right on time", "A bit late", "Had to chase them"]},
        {"field": "callout", "label": "Call-outs", "question": "Do they take emergency or weekend jobs?",
         "options": ["Yes", "Weekdays only", "Not sure"]},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a neighbour should know before hiring them?"},
    ],
    "diy": [
        {"field": "fixes", "label": "Fixes", "question": "What problem does it solve?", "required": True},
        {"field": "how", "label": "How to do it", "question": "How do you do it?", "required": True},
        {"field": "needs", "label": "You'll need", "question": "What do they need on hand?"},
        {"field": "time", "label": "Takes", "question": "How long does it take?",
         "options": ["Under an hour", "An afternoon", "A weekend"]},
        {"field": "difficulty", "label": "Difficulty", "question": "How hard is it?",
         "options": ["Anyone can", "Some patience", "Handy only"]},
        {"field": "cost", "label": "Cost", "question": "Roughly what does it cost to do?",
         "options": ["Under $20", "$20-100", "$100+"]},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything that goes wrong the first time?"},
    ],
    # The escape hatch, and the only bucket defined by what it is NOT. A bus route, a
    # Facebook group, an insurance broker, "don't use the west gate at 5pm" — real
    # recommendations that are none of the six above, and which used to arrive with
    # reco_type NULL: no question set was generated for them, so the capture limped along
    # on ad-hoc questions and the row landed invisible to every category read.
    #
    # Its questions are deliberately generic, because the subject could be anything. The
    # sharper questions still come from the model — this is only the floor under them.
    "other": [
        {"field": "helps_with", "label": "Good for", "question": "What is it good for?", "required": True},
        # NOT `where_to_find`/`where`: those names carry the Places picker (see _PLACE_FIELDS),
        # and an `other` subject is precisely the kind that is not a point on a map — a bus
        # route, a Facebook group, a broker's phone number. Plain text.
        {"field": "where_to_look", "label": "Where to find it", "question": "Where do neighbours find it?", "required": True},
        {"field": "cost", "label": "Cost", "question": "Does it cost anything?",
         "options": ["Free", "A few dollars", "More than that"]},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a neighbour should know?"},
    ],
}

RECO_TYPES = tuple(_SETS)

# ── The subject step ─────────────────────────────────────────────────────────────────────
#
# The FIRST question of every set, and the one field the card cannot be written without:
# "Split Oak Forest", "Dr Sarah", "Cosori gooseneck". It lives here rather than in _SETS
# because this file owns both ENDS of a generated set (see TAIL below) — the model writes
# the wording, the floor guarantees the step exists whatever it wrote.
#
# It used to be collected by a hand-written gate OUTSIDE the set ("Who or where? A name
# helps me find them."), which was type-blind — an electric kettle was asked *who or
# where* and got no step set at all — and duplicated the set's own `where` step for
# places (dev QA 2026-09-04).
SUBJECT_FIELD = "subject"

# (eyebrow, fallback question) per type, for when generation fails or the LLM is off.
_SUBJECT_STEP: dict[str, tuple[str, str]] = {
    "professional": ("Who", "Who is it?"),
    "service": ("Who", "Who is it?"),
    "restaurant": ("Place", "Which place is it?"),
    "recipe": ("Recipe", "What's the recipe called?"),
    "product": ("Product", "What's it called?"),
    "location": ("Place", "Which place is it?"),
    "diy": ("Trick", "What would you call this trick?"),
    "other": ("What", "What is it called?"),
}

# Types whose SUBJECT is itself a point on the map: the subject step is answered with the
# Places picker, and the set's own place steps ("Where is it?") would then ask for the same
# thing twice. professional/service keep theirs — Dr Sarah is not her clinic, and a plumber
# has no address at all.
_PLACE_SUBJECT_TYPES = frozenset({"location", "restaurant"})

# Types whose subject MIGHT be a point on the map — a barber shop, a dental clinic and a
# gym are all walked into, while a plumber, a nanny and a tutor-who-comes-to-you have no
# storefront at all. The type alone cannot tell them apart, so the extractor's
# `place_based` read decides, and only for these two: a recipe or a DIY trick is never a
# place however confidently the model says so.
#
# Without this a "barber shop near me" was asked "Who is it?" in a plain text box, with no
# picker and no nearby shops to tap — the type table had it filed with the plumbers (dev QA
# 2026-09-08).
_PLACE_CAPABLE_TYPES = frozenset({"professional", "service"})


def subject_is_place(reco_type: Any, *, place_based: bool = False) -> bool:
    """Is this recommendation's subject a map point (answered with the Places picker)?

    `place_based` is the extractor's read of "you could find this on a map", and it is what
    separates a barber shop from a plumber inside the same `service` type."""
    rtype = normalize_type(reco_type)
    return rtype in _PLACE_SUBJECT_TYPES or bool(
        place_based and rtype in _PLACE_CAPABLE_TYPES
    )


def head_step(
    reco_type: Any,
    *,
    question: Any = None,
    label: Any = None,
    place_based: bool = False,
    subject_hint: Any = None,
) -> dict[str, Any] | None:
    """The subject step for a type (None for an unknown type). `question`/`label` are the
    model's wording when it wrote them — it knows this is a trampoline park and not "the
    place" — falling back to the type's own phrasing."""
    rtype = normalize_type(reco_type)
    if not rtype:
        return None
    dflt_label, dflt_question = _SUBJECT_STEP[rtype]
    # The type's own wording assumes a person, because `service` was written for plumbers:
    # "a barber shop near me" was asked "Who is it?" over a list of barber shops to tap
    # (dev QA 2026-09-08). When the subject turns out to be a storefront, the category the
    # user just said is better wording than anything this table can hold — and it is what
    # Lana would have written herself once the set is generated.
    hint = " ".join(str(subject_hint or "").split()).lower()[:32]
    if hint and subject_is_place(rtype, place_based=place_based):
        dflt_label, dflt_question = hint, f"Which {hint} is it?"
    written = " ".join(str(question or "").split())
    return {
        "field": SUBJECT_FIELD,
        "label": " ".join(str(label or "").split())[:24] or dflt_label,
        "question": written[:140] if written.endswith("?") else dflt_question,
        "kind": "place" if subject_is_place(rtype, place_based=place_based) else "text",
        "required": True,
    }

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
    "other = a real recommendation that is none of the six above (a bus route, a local "
    "Facebook group, an insurance broker, a warning about a bad hour to visit somewhere). "
    "Split by what the neighbor DOES with it, never by topic: food can be restaurant, "
    "recipe, product or location depending on that. NEVER answer null: when nothing fits, "
    "'other' IS the answer — a typeless recommendation cannot be found by anyone. But "
    "'other' is a LAST resort, so try the six first; a book is a product and a church is a "
    "location, however unusual the ask sounds."
)


def normalize_type(raw: Any) -> str | None:
    """A known reco type, or None. Tolerates the plural/spacey shapes an LLM returns."""
    key = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key.endswith("s") and key[:-1] in _SETS:
        key = key[:-1]
    return key if key in _SETS else None


# ── The category toggle (Find a peer recommendation) ─────────────────────────────────────
#
# The Find screen's chip row — Services / Restaurants / DIY / Products / Recipes / Others —
# is a FILTER over reco_type, not a hint for the prose: tapping Recipes and then asking must
# not answer with a plumber. One chip is not always one bucket (Services covers a dentist
# AND a plumber, Others covers a park), so the picked filter is a LIST of taxonomy keys and
# both readers take it as `p_reco_types` (20261128120000).
#
# The client sends the keys; unknown ones are dropped rather than guessed at, because a
# typo'd key that fell through as "no filter" would show every type under a chip that
# promised one — the exact bug this exists to fix.
RECO_TYPE_CTX_KEY = "reco_type_filter"

# Types with no point on a map. The empty-handed tip_seek falls back to Google Places, and
# a Places search for a recipe, a repair trick or a bus route returns restaurants and
# hardware stores — confident nonsense under a chip that asked for none of them.
#
# The "Others" chip sends location AND other, and one searchable type in the pick is enough
# to keep the fallback (see `google_searchable`) — so that chip still gets nearby places
# while a pure `other` ask does not.
GOOGLE_UNSEARCHABLE_TYPES = frozenset({"recipe", "diy", "other"})


def normalize_types(raw: Any) -> list[str]:
    """The known taxonomy keys in whatever the client sent (order kept, deduped)."""
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw if isinstance(raw, (list, tuple)) else []:
        key = normalize_type(item)
        if key and key not in out:
            out.append(key)
    return out


def apply_reco_type_filter(session_ctx: dict[str, Any], raw: Any) -> list[str]:
    """Stamp the chip row's pick on the session, and return what is now in force.

    Sticky like the community filter ([[community-filter-scope]]): the chip stays lit while
    the user narrows the ask over several turns, so the pick has to outlive the turn it
    arrived on. `None` means the client said nothing — keep what's there; `[]` is the
    explicit "no category" and clears it.
    """
    if raw is None:
        return active_reco_types(session_ctx)
    types = normalize_types(raw)
    session_ctx[RECO_TYPE_CTX_KEY] = types or None
    return types


def active_reco_types(session_ctx: dict[str, Any] | None) -> list[str]:
    """The category filter in force this turn ([] = every type)."""
    return normalize_types((session_ctx or {}).get(RECO_TYPE_CTX_KEY))


def google_searchable(types: Any) -> bool:
    """Can a Places search stand in for a missing neighbour rec under this filter?"""
    picked = normalize_types(types)
    return not picked or any(t not in GOOGLE_UNSEARCHABLE_TYPES for t in picked)


def steps_for(
    reco_type: Any, *, place_based: bool = False, subject_hint: Any = None
) -> list[dict[str, Any]]:
    """The ordered carousel steps for a type ([] for an unknown type). Copies, so a caller
    stamping `answer` onto a step can't mutate the shared table.

    Head-first, same as a generated set: when generation fails this static set IS the set,
    and without the subject step it would never learn what is being recommended.

    Put through the SAME two filters a generated set gets, because this is not only the
    failure path — the flip-through carousel runs on it until the subject is answered, and
    it showed the raw table: a place picker on step 1 and "Where is it?" again on step 3,
    with every control a text box because nothing had set `kind` (dev QA 2026-09-08)."""
    head = head_step(reco_type, place_based=place_based, subject_hint=subject_hint)
    if not head:
        return []
    drop_place = subject_is_place(reco_type, place_based=place_based)
    out: list[dict[str, Any]] = []
    for raw in _SETS.get(normalize_type(reco_type) or "", []):
        step = dict(raw)
        step["kind"] = _kind_for(step["field"], step.get("options"))
        if drop_place and (
            step["kind"] == "place" or _LOOKUP_ASK.search(step["question"])
        ):
            continue
        out.append(step)
    return [head] + out


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
    "other": ("helps_with", "where_to_look"),
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
# The set is model-written, so the same step arrives as `office_location` or `clinic_address`
# as often as bare `where` — and a name the list misses renders as a text box (dev QA
# 2026-09-03). `where_to_buy` ends in none of these, so the Amazon case stays a text box.
_PLACE_SUFFIXES = ("_location", "_address", "_venue")

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

# Questions Google already answers for a point on the map: hours, phone, website, the
# $$-band. Asking them wastes a carousel step on a fact Lana can look up, and buys a
# useless answer besides — "price range?" came back as "hundred dollars" (dev QA
# 2026-09-07), which no reader can filter on. Only applied when the SUBJECT is a place
# (see `drop_place`): a product's price and a plumber's number are word-of-mouth, not
# listings, and stay askable.
_LOOKUP_ASK = re.compile(
    r"opening hours|what (?:time|hours)|when (?:do|does|is) (?:it|they|the)\s*\w* ?(?:open|close)|"
    r"phone number|website|price range|how (?:much|expensive)|price (?:point|band|level)|"
    r"what does (?:it|a meal) cost",
    re.IGNORECASE,
)


def _slug(raw: Any) -> str:
    """A stable snake_case field key from whatever the model called it. The key is what the
    answer is stored under, so it must not carry the wording — re-generating a set with
    nicer copy has to leave the answers findable."""
    return re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")[:32]


def _kind_for(field: str, options: Any = ()) -> str:
    """Which control answers this step: a map search, a chip row, or a text box."""
    if field in _PLACE_FIELDS or field.endswith(_PLACE_SUFFIXES):
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


def build_step_set(
    raw: Any,
    *,
    fallback: dict[str, dict[str, Any]],
    floor: tuple[str, ...],
    head: dict[str, Any] | None,
    tail: list[dict[str, Any]],
    drop_place: bool = False,
    required_extra: int = 0,
    require_options: bool = False,
) -> list[dict[str, Any]]:
    """A model-written question set, made safe to ask — head + middle + tail.

    Every line here is a guard against a specific way a generated set goes wrong: junk
    fields, a duplicate question, a statement instead of a question, a private ask, a
    35-step interrogation, a set that forgot the phone number, a model that helpfully
    re-invented the consent toggle in its own words.

    Shared by the recommendation capture and the community capture (see
    `community_question_sets`): the two differ ONLY in their tables — the guards, the
    ordering rule and the required rule are the same problem in both, and were the same
    ninety lines twice before this was pulled out.
    """
    reserved = {s["field"] for s in tail}
    if head:
        reserved.add(head["field"])
    middle: list[dict[str, Any]] = []
    # Every question thrown away, with the reason, logged once at the end. A guard that
    # drops silently is a guard nobody can tell has started dropping everything.
    dropped: list[str] = []
    # The tail and the head are ours; a generated copy of either is dropped.
    seen: set[str] = set(reserved)
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        field = _slug(item.get("field"))
        question = " ".join(str(item.get("question") or "").split())
        if not field or not question.endswith("?"):
            dropped.append(f"{field or '?'}:malformed")
            continue
        if field in reserved:
            # Expected: the model was told the head and tail are ours and wrote one anyway.
            dropped.append(f"{field}:ours")
            continue
        if field in seen:
            dropped.append(f"{field}:duplicate")
            continue
        if _BLOCKED_ASK.search(question):
            dropped.append(f"{field}:private")
            continue
        if drop_place and _kind_for(field) == "place":
            dropped.append(f"{field}:place_dup")
            continue
        if drop_place and _LOOKUP_ASK.search(question):
            dropped.append(f"{field}:on_the_listing")
            continue
        opts = [
            " ".join(str(o).split())
            for o in (item.get("options") or [])
            if isinstance(o, str) and str(o).strip()
        ][:4]
        # A facet with no answer set is a text box, and a text box is where "hundred
        # dollars" came from (dev QA 2026-09-07). Asked to write the four answers, the
        # model has to know the subject: "what is the price range?" has no sane set for a
        # bookstore, so it never gets written, while "which ages?" has an obvious one.
        # Exempt: floor fields (a phone number has no options) and map steps (the picker
        # IS the answer set).
        if require_options and not opts and field not in floor and _kind_for(field) != "place":
            dropped.append(f"{field}:no_options")
            continue
        step: dict[str, Any] = {
            "field": field,
            "label": " ".join(str(item.get("label") or field.replace("_", " ")).split())[:24],
            "question": question[:140],
            "kind": _kind_for(field, opts),
        }
        if opts:
            step["options"] = opts
            # Several answers can be true at once ("known for: journals AND pens"), so the
            # chips toggle instead of replacing. Only meaningful with options, and only
            # when the model says so: a price band or a spice level is exactly one answer.
            if item.get("multi") is True:
                step["multi"] = True
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
            # `fallback` is head-first too, and the head is added back on return.
            if s["field"] not in reserved
            and not (
                drop_place
                and (
                    _kind_for(s["field"], s.get("options")) == "place"
                    or _LOOKUP_ASK.search(s["question"])
                )
            )
        ]

    # The model's ORDER stands — it put "What does she do?" first and location before the
    # phone number for a reason, and second-guessing that is how the carousel starts asking
    # for a phone number before it has said who she is. Only the floor fields it FORGOT are
    # inserted, and they go next to the other basics rather than at the end: they are asked
    # early or, on an early "that's it", never.
    have = {s["field"] for s in middle}
    for field in floor:
        if field in have or field not in fallback:
            continue
        if drop_place and _kind_for(field, fallback[field].get("options")) == "place":
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
    if dropped:
        _log.info("step_set.dropped kept=%d dropped=%s", len(middle), ",".join(dropped))
    floor_present = [f for f in floor if f in have][: 2 + required_extra]
    required = set(floor_present) or {s["field"] for s in middle[:2]}
    for step in middle:
        step["required"] = step["field"] in required

    return ([head] if head else []) + middle[:_MAX_MIDDLE] + tail


def validate_steps(
    raw: Any,
    reco_type: Any,
    *,
    tallies: Any = (),
    place_based: bool = False,
    subject_hint: Any = None,
) -> list[dict[str, Any]]:
    """The recommendation capture's tables, poured into `build_step_set`."""
    rtype = normalize_type(reco_type)
    if not rtype:
        return []
    # The head is ours, its WORDING is the model's: it wrote "Which trampoline park?" from
    # the same sentence the type came out of, which no static table can do.
    written = next(
        (
            item
            for item in (raw or [])
            if isinstance(item, dict) and _slug(item.get("field")) == SUBJECT_FIELD
        ),
        None,
    )
    return build_step_set(
        raw,
        fallback={
            s["field"]: s
            for s in steps_for(
                rtype, place_based=place_based, subject_hint=subject_hint
            )
        },
        floor=_FLOOR.get(rtype, ()),
        head=head_step(
            rtype,
            question=(written or {}).get("question"),
            label=(written or {}).get("label"),
            place_based=place_based,
            subject_hint=subject_hint,
        ),
        tail=tail_steps(tallies),
        # A restaurant/location subject is picked on the map, so a second place step is the
        # same question twice. Dropped and not answered-by-proxy: "Zaiqa" is not an answer
        # to "which area is it in?", and a wrong answer is worse than one fewer step.
        drop_place=subject_is_place(rtype, place_based=place_based),
        # Recommendations only. A community's questions are prose by nature ("what happens
        # when you meet?") and the same rule there would gut the set.
        require_options=True,
    )
