"""
ports.py — the swap seam + data contract for the recommendation-quality eval harness.

WHAT THIS HARNESS TESTS
-----------------------
The recommendation capture, in the two places it can fail independently:

  ARM A — the QUESTIONS Lana writes for a subject.
      app/tip_share.py:110          _extract_tip_fields(...) -> (fields, ask)
                                    returns `steps_raw` — the model's proposed question set
      app/reco_question_sets.py:292 validate_steps(raw, reco_type, tallies=()) -> list[step]
                                    the guards, the floor, the two closing steps

  ARM B — the ANSWERS the flow ACCEPTS.
      app/main.py:2509              POST /lana/sessions/{id}/tip-setup   (the carousel fork)
      app/tip_share.py:604          the chat fork, one question per turn
      app/reco_question_sets.py:181 missing_required(spec, answers)

Arm B exists because nothing in the product looks at what an answer MEANS. Verified against
the shipped code on 2026-09-08, and it is a short list — every check applied to a
recommendation answer today, end to end:

  1. the field must belong to the session's own generated step set   main.py:2534, 2539
  2. `" ".join(str(value or "").split())[:280]`                       main.py:2538
     — whitespace-collapsed, silently TRUNCATED at 280, dropped if it collapses to empty
  3. the required fields must be non-blank before the ready card     reco_question_sets.py:181
  4. the DB accepts any JSON array                                   20261118120000:89-97
     `check (jsonb_typeof(reco_fields) = 'array')` — no per-element constraint at all

That is the whole list. `TipSetupRequest.answers` is `dict[str, str]` with no validators
(models.py:791-800), and a repo-wide search for a relevance/semantic/type check on a
recommendation answer returns nothing. So "hundred dollars" answers "what's the price range?"
and "ask me" satisfies a required contact field, and both reach the card as facts.

Sources: Asjid Malik, "How Lana decides what to ask about a recommendation" (2026-09-07),
§§5, 6, 9, 11, 12 — §11 is the gap Arm B measures; and the R&D standup of the same day,
which turned it into an action item ("add an evaluation step to the report system; ensure
the system includes validation for data quality").

ROLE BOUNDARY
-------------
This suite EVALUATES. It never patches `app/` or `supabase/` — see simulations/CLAUDE.md.
Consequences that shape this file:

  * ShippedValidator MIRRORS rules 1 and 2 above, because they live inline in a FastAPI
    endpoint body and cannot be imported. Every mirrored line carries its citation, and
    selftest.py asserts the mirror against the real endpoint whenever a live worker is
    reachable — the same drift risk policy_eval/lingo_guardrail.py carries, handled the same
    way. Rule 3 is NOT mirrored: `missing_required` is imported and called for real.
  * ReferenceValidator is a REFERENCE, not a proposed patch. It exists to prove the fixtures
    are not vacuous (a file where nothing can score well measures nothing) and to hand the
    backend a concrete target for the AI-validator action item. Nobody should ship it as-is.

GUESSED / FLAGGED CONVENTION (identical to circles_zip and policy_eval)
----------------------------------------------------------------------
`# GUESSED` = the real system genuinely has not decided this yet.
`# FLAGGED` = adapter lossiness: the thing is real, but THIS backend cannot observe it.
`grep -rnE "GUESSED|FLAGGED" reco_eval/` lists them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

SCHEMA_VERSION = "reco-eval/1"

# The eight reco types (seven kinds + 'other'). REAL and CONSTRAINED IN TWO PLACES that must
# agree:
#   app/reco_question_sets.py `_SETS` keys (RECO_TYPES)
#   supabase/migrations/20261130120000_reco_other_and_census.sql:55-59
#     "the closed 8-value taxonomy (7 kinds + other); anything else raises invalid_reco_type"
#     (supersedes the original 7-value constraint in 20261117120000_reco_types.sql — 'other'
#     was added so "the app now writes 'other' rather than nothing, whatever the model
#     returned", per that migration's own comment)
# The migration is the authority — a type this list carries and the DB rejects is a write
# that 500s in production. checks.check_reco_type derives its valid set from THIS Literal, so
# it only catches the harness's own generator drifting from this list, not this list drifting
# from the DB — that direction has to be checked by hand against the migration (caught once,
# 2026-09-17: this list and checks._FLOOR_MIRROR both missed 'other' for one selftest run after
# it shipped). Update this list by hand whenever the migration's taxonomy changes.
RecoType = Literal[
    "professional", "restaurant", "recipe", "product", "location", "service", "diy", "other",
]

# RecoStep.kind — REAL: app/models.py:663. What control the FE renders for the step.
#   text   -> free-type, `placeholder` is the hint
#   choice -> `options` as tappable chips, still free-typeable
#   place  -> the Places map picker (reco_question_sets.py `_PLACE_FIELDS`)
#   toggle -> the consent step, ours, never model-written
#   agree  -> "others also said", options are "<attr> x<n>"
StepKind = Literal["text", "choice", "place", "toggle", "agree"]

# The two closing steps this harness must never score as model output — they are ours
# (reco_question_sets.py `TAIL_FIELDS`). The consent question in particular "has to read
# exactly the same for everyone, so Lana is never allowed to reword it" (§5).
TAIL_FIELDS = ("ask_ok", "others_also_said")

# The consent question, byte-for-byte as reco_question_sets.tail_steps() emits it. Asserted
# rather than imported ON PURPOSE: importing it would make the check tautological (it would
# compare the product to itself and pass under any rewording). This is the one string in the
# flow whose wording is a product guarantee, so the harness pins it independently and fails
# when it moves — a deliberate "come and update the eval" tripwire, not a bug.
CONSENT_QUESTION = "Can neighbours ask you more?"

Verdict = Literal["PASS", "SOFT_FAIL", "HARD_FAIL", "UNSCORED"]


# ---------------------------------------------------------------------------
# ARM A — question sets
# ---------------------------------------------------------------------------

@dataclass
class QuestionFixture:
    """One recommendation to generate a question set for. Loaded from fixtures.yaml."""

    id: str
    opening_line: str
    # The tip draft as it stands when tip_share.py:604 writes the set. Mirrors `prev` in
    # _extract_tip_fields — name / category / trait / locality / place_based, and (only for
    # the ordering-hazard fixture) reco_type.
    prior_draft: dict[str, Any] = field(default_factory=dict)
    expect_reco_type: str | None = None
    # §12.1: the subject is a business/place Google already lists, so hours / phone / website
    # / street address / price band are questions that waste a card.
    lookup_able: bool = False
    stated_facts: list[dict[str, Any]] = field(default_factory=list)
    forbidden_topics: list[dict[str, Any]] = field(default_factory=list)
    lang: str | None = None
    # False only for the fixture that pins the known type-before-name hazard, where the
    # product is EXPECTED to fall back to the static set. See fixtures.yaml q11.
    expect_generated: bool = True
    probes: str = ""


@dataclass
class GeneratedSet:
    """What a QuestionSetPort produced for one fixture."""

    # validate_steps output: the middle (model-written, floor-repaired) plus the tail.
    steps: list[dict[str, Any]] = field(default_factory=list)
    reco_type: str | None = None
    # What the extractor pre-filled from the opening line — tip_share.py:60-66 `answers`.
    # This is how the product avoids re-asking something the neighbour already said, so it is
    # ground truth for the `stated_facts` check, not a nice-to-have.
    prefilled: dict[str, str] = field(default_factory=dict)
    # The model's proposal BEFORE validate_steps. Kept so the harness can tell a set the
    # guards repaired from a set that arrived clean — a floor field the model forgot and we
    # added back is a real (if survivable) generation failure, and it is invisible in `steps`.
    raw_steps: list[dict[str, Any]] | None = None
    # False when the middle is the type's STATIC fallback rather than a written set. Not
    # observable from `steps` alone — a fallback set is a perfectly well-formed set — so the
    # port has to declare it. See backends.detect_fallback for how it is decided.
    generated: bool = True
    # Whether the turn-2 prefill probe actually ran. On the generating turn `answers` is
    # structurally {} (tip_share.py:151), so without the probe the "already answered" axis is
    # unmeasured — and unmeasured is UNSCORED, never a pass.
    prefill_measured: bool = False
    error: str | None = None
    # Free-text lines that MUST appear in the report beside any verdict from this fixture.
    flags: list[str] = field(default_factory=list)

    def middle(self) -> list[dict[str, Any]]:
        """The steps that are model territory — everything but the two closing steps. Every
        Arm A check runs on THIS, never on `steps`: scoring the tail as model output would
        fail the consent toggle for not being filterable and would be nonsense."""
        return [s for s in self.steps if s.get("field") not in TAIL_FIELDS]

    def tail(self) -> list[dict[str, Any]]:
        return [s for s in self.steps if s.get("field") in TAIL_FIELDS]


class QuestionSetPort(Protocol):
    """Produce the question set for one recommendation.

    A port may also expose `last_flags: list[str]` describing what it could not honour on the
    last call. Not part of the Protocol: a port that honours everything should not have to
    say so.
    """

    def generate(self, fx: QuestionFixture) -> GeneratedSet:
        ...


# ---------------------------------------------------------------------------
# ARM B — answer acceptance
# ---------------------------------------------------------------------------

@dataclass
class AnswerFixture:
    """One hand-labelled answer to one question. Loaded from fixtures.yaml."""

    id: str
    reco_type: str
    field_name: str
    question: str
    kind: StepKind
    required: bool
    answer: str
    # Ground truth. "accept" = a reader of the card can act on this. "reject" = it is stored
    # and displayed as a fact and is not one.
    verdict: Literal["accept", "reject"]
    reason: str = ""


@dataclass
class AnswerDecision:
    """What a validator did with one answer."""

    accepted: bool
    # What actually lands in the draft. Differs from the input wherever the flow mutates it —
    # today only by whitespace collapse and the 280-char truncation (main.py:2538). None when
    # the answer was dropped entirely.
    stored: str | None
    reason: str = ""
    # The validator could not reach a decision (no LLM key, backend error). Fails closed:
    # scored UNSCORED, never folded into either side of the accept/reject ratio, and it fails
    # `--gate`. An unmeasured answer must not be able to carry a green gate.
    unscorable: bool = False


class AnswerValidatorPort(Protocol):
    """Decide whether one answer is accepted into the recommendation."""

    def validate(self, fx: AnswerFixture) -> AnswerDecision:
        ...


# ---------------------------------------------------------------------------
# Scoring vocabulary shared by both arms
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    verdict: Verdict
    detail: str


def worst(results: list[CheckResult]) -> Verdict:
    """HARD_FAIL > UNSCORED > SOFT_FAIL > PASS.

    UNSCORED outranks SOFT_FAIL for the reason policy_eval/checks.py gives: "I could not
    measure this" is a stronger reason to stop and look than "a defensible alternative might
    have done otherwise". It sits below HARD_FAIL because it is not a finding against Lana.
    """
    if any(r.verdict == "HARD_FAIL" for r in results):
        return "HARD_FAIL"
    if any(r.verdict == "UNSCORED" for r in results):
        return "UNSCORED"
    if any(r.verdict == "SOFT_FAIL" for r in results):
        return "SOFT_FAIL"
    return "PASS"
