"""Question sets for the CREATE-A-COMMUNITY capture (the C-CREATE-COMMUNITY mock).

The recommendation capture's twin, and deliberately built on its machinery: same head →
middle → tail shape, same `build_step_set` guards, same `carousel` / `next_question` /
`missing_required` walk. A community is not a recommendation, but "Lana writes the
questions for THIS subject and the FE renders them as cards or as chat" is the same
problem — see `reco_question_sets` for why the guards exist.

What differs, and why:

  SUBJECT is always a place. A community IS a canonical `places` row (2026-07-28 product
  decision: no community without a location), so the first step is always `kind: "place"`
  and it is answered with the Places picker, never typed. That one answer carries the
  `google_place_id` the publish needs — see `community_capture.publish_community`.

  TYPE is `circle_affiliations.circle_type`, the taxonomy `circles_capture` already
  indexes on. It is NOT re-invented here: a community created by this flow and one parked
  from conversation have to be the same kind of row, or "communities like this one" stops
  meaning anything.

  NO CONSENT TAIL. A recommendation's tail asks whether neighbours may message the
  author, because a rec is a claim about someone else. A community is a place anyone can
  already see and ask to join, so the tail is one warm closing question instead.

# ponytail: copy in code, one deploy per wording change — same call as reco_question_sets.
# A shared admin editor for both tables is the upgrade if wording churns after launch.
"""

from __future__ import annotations

from typing import Any

from app.circles_capture import CIRCLE_TYPES
from app.reco_question_sets import (
    SUBJECT_FIELD,
    _slug,
    build_step_set,
)

# The subject step's field. Same key as the recommendation capture on purpose: the FE's
# carousel, live card and place-picker all branch on `kind == "place"` and on the step
# being first, so nothing about the subject needs a second code path over there.
COMMUNITY_SUBJECT_FIELD = SUBJECT_FIELD

# The closing step of every community set: warm, optional, and ours (never generated), so
# the same question closes every community whatever the model wrote above it.
COMMUNITY_TAIL_FIELDS = ("who_for",)

# field: the snake_case key the answer lands under — and, at publish, the `place_features`
# key, so renaming one silently strands the stored answers. Stable across wording changes.
#
# required: the flow will not reach the ready card without it. Kept in step with the first
# two entries of _FLOOR below.
_SETS: dict[str, list[dict[str, Any]]] = {
    "fitness": [
        {"field": "draws", "label": "Known for", "question": "What kind of training happens here?", "required": True},
        {"field": "when", "label": "When", "question": "When do people usually show up?", "required": True},
        {"field": "level", "label": "Level", "question": "Who is it a good fit for?",
         "options": ["Beginners", "All levels", "Serious training"]},
        {"field": "cost", "label": "Cost", "question": "Roughly what does it cost to join in?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before turning up?"},
    ],
    "faith": [
        {"field": "draws", "label": "Known for", "question": "What gathers people here?", "required": True},
        {"field": "when", "label": "When", "question": "When are the main gatherings?", "required": True},
        {"field": "language", "label": "Language", "question": "Which languages are services in?"},
        {"field": "families", "label": "Families", "question": "Is there anything for kids?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a first-timer should know?"},
    ],
    "school": [
        {"field": "draws", "label": "Known for", "question": "What brings parents together here?", "required": True},
        {"field": "when", "label": "When", "question": "When do parents tend to gather?", "required": True},
        {"field": "ages", "label": "Ages", "question": "Which ages does it cover?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a new parent should know?"},
    ],
    "kids_activity": [
        {"field": "draws", "label": "Known for", "question": "What do kids do here?", "required": True},
        {"field": "when", "label": "When", "question": "When is it on?", "required": True},
        {"field": "ages", "label": "Ages", "question": "Which ages is it best for?"},
        {"field": "cost", "label": "Cost", "question": "What does it cost?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before going?"},
    ],
    "neighborhood": [
        {"field": "draws", "label": "Known for", "question": "What do neighbours come here for?", "required": True},
        {"field": "when", "label": "When", "question": "When does it get busy?", "required": True},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?",
         "options": ["Families", "Kids", "Dogs", "Quiet time"]},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know — parking, hours?"},
    ],
    "hobby": [
        {"field": "draws", "label": "Known for", "question": "What do people do together here?", "required": True},
        {"field": "when", "label": "When", "question": "When does the group meet?", "required": True},
        {"field": "level", "label": "Level", "question": "Do you need any experience?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to bring or know first?"},
    ],
    "support": [
        {"field": "draws", "label": "Known for", "question": "What kind of support happens here?", "required": True},
        {"field": "when", "label": "When", "question": "When does the group meet?", "required": True},
        {"field": "who_leads", "label": "Who leads", "question": "Who runs it?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything someone nervous should know?"},
    ],
    "heritage": [
        {"field": "draws", "label": "Known for", "question": "What is celebrated or shared here?", "required": True},
        {"field": "when", "label": "When", "question": "When do people gather?", "required": True},
        {"field": "language", "label": "Language", "question": "Which languages will they hear?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything a newcomer should know?"},
    ],
    "friends": [
        {"field": "draws", "label": "Known for", "question": "What makes this the spot?", "required": True},
        {"field": "when", "label": "When", "question": "When do people end up here?", "required": True},
        {"field": "best_for", "label": "Best for", "question": "Who is it best for?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know before going?"},
    ],
    "other": [
        {"field": "draws", "label": "Known for", "question": "What brings people here?", "required": True},
        {"field": "when", "label": "When", "question": "When do people gather?", "required": True},
        {"field": "best_for", "label": "Best for", "question": "Who is it for?"},
        {"field": "good_to_know", "label": "Good to know", "question": "Anything to know first?"},
    ],
}

# Every circle type must have a set: an unknown type would otherwise fall to [] and the
# capture would have no questions to ask at all.
assert set(_SETS) == set(CIRCLE_TYPES), "every circle type needs a community question set"

# The two facts a neighbour deciding whether to show up cannot do without: what happens
# here, and when. Left to itself the model writes lovely colour ("what's the vibe?") and
# never says when the thing is on.
_FLOOR: dict[str, tuple[str, ...]] = {t: ("draws", "when") for t in _SETS}

# The floor, phrased for the prompt. Generated from _FLOOR so adding a type can't leave
# the prompt describing the old one.
COMMUNITY_FLOOR_RULES = "; ".join(
    f"{t} → " + " · ".join(f.replace("_", " ") for f in fields)
    for t, fields in _FLOOR.items()
)

# What the extractor is told each type MEANS. Kept next to the sets so a new type is one
# edit, not two files. Same taxonomy circles_capture parks conversational affiliations
# into, so a created community and a mentioned one are the same kind of row.
COMMUNITY_TYPE_RULES = (
    "fitness = a gym / studio / court people train at. "
    "faith = a church, mosque, synagogue, temple. "
    "school = a school or its parent community. "
    "kids_activity = somewhere kids do an activity (classes, soft play, swim school). "
    "neighborhood = a shared local spot (park, market, community centre, library). "
    "hobby = a club or group around an interest (chess, running, book club). "
    "support = a support or recovery group. "
    "heritage = a cultural or language community. "
    "friends = a hangout spot a circle of friends is built around (cafe, bakery, bar). "
    "other = a real community that fits none of the above. "
    "Pick by what people DO there, never by the building's category."
)

# (eyebrow, fallback question) per type, for when generation fails or the LLM is off. The
# subject is a PLACE in every one of them — no community without a location.
_SUBJECT_STEP: dict[str, tuple[str, str]] = {
    "fitness": ("Which gym", "Which gym or studio is it?"),
    "faith": ("Which place", "Which place of worship is it?"),
    "school": ("Which school", "Which school is it?"),
    "kids_activity": ("Which spot", "Where do the kids go?"),
    "neighborhood": ("Which spot", "Which spot is it?"),
    "hobby": ("Where", "Where does the group meet?"),
    "support": ("Where", "Where does the group meet?"),
    "heritage": ("Which place", "Which place is it?"),
    "friends": ("Which spot", "Which spot is it?"),
    "other": ("Which place", "Which place is it?"),
}


def normalize_community_type(raw: Any) -> str | None:
    """A known circle type, or None. Tolerates the plural/spacey shapes an LLM returns."""
    key = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key.endswith("s") and key[:-1] in _SETS:
        key = key[:-1]
    return key if key in _SETS else None


def community_head_step(
    circle_type: Any, *, question: Any = None, label: Any = None
) -> dict[str, Any] | None:
    """The place step every community set opens with (None for an unknown type).

    Always `kind: "place"` — the answer has to carry a `google_place_id`, because a typed
    place name cannot be grounded and a community that is not grounded is invisible
    everywhere (see `circles_flow.add_circle`).
    """
    ctype = normalize_community_type(circle_type)
    if not ctype:
        return None
    dflt_label, dflt_question = _SUBJECT_STEP[ctype]
    written = " ".join(str(question or "").split())
    return {
        "field": COMMUNITY_SUBJECT_FIELD,
        "label": " ".join(str(label or "").split())[:24] or dflt_label,
        "question": written[:140] if written.endswith("?") else dflt_question,
        "kind": "place",
        "required": True,
    }


def community_tail_steps() -> list[dict[str, Any]]:
    """The closing step of every community set — ours, worded the same for everyone.

    A recommendation's tail is a consent toggle (may neighbours message you about this
    person?). A community needs no such gate — it is a public place anyone can already
    find and ask to join — so the tail spends its one step on the thing the roster
    actually reads: who this is for.
    """
    return [
        {
            "field": "who_for",
            "label": "Who it's for",
            "question": "Who should feel welcome here?",
            "kind": "text",
            "required": False,
        }
    ]


def community_steps_for(circle_type: Any) -> list[dict[str, Any]]:
    """The static set for a type ([] for an unknown one), head first.

    Head-first like the generated set: when generation fails this IS the set, and without
    the place step the community could never be grounded.
    """
    head = community_head_step(circle_type)
    if not head:
        return []
    ctype = normalize_community_type(circle_type) or ""
    return [head] + [dict(s) for s in _SETS.get(ctype, [])]


def validate_community_steps(raw: Any, circle_type: Any) -> list[dict[str, Any]]:
    """A model-written community set, made safe to ask: [] for an unknown type.

    The guards live in `reco_question_sets.build_step_set` — this only supplies the
    community tables. `drop_place` is on because the subject already pinned the map: a
    second "where is it?" would ask for the thing the picker just returned.
    """
    ctype = normalize_community_type(circle_type)
    if not ctype:
        return []
    written = next(
        (
            item
            for item in (raw or [])
            if isinstance(item, dict)
            and _slug(item.get("field")) == COMMUNITY_SUBJECT_FIELD
        ),
        None,
    )
    return build_step_set(
        raw,
        fallback={s["field"]: s for s in community_steps_for(ctype)},
        floor=_FLOOR.get(ctype, ()),
        head=community_head_step(
            ctype,
            question=(written or {}).get("question"),
            label=(written or {}).get("label"),
        ),
        tail=community_tail_steps(),
        drop_place=True,
    )
