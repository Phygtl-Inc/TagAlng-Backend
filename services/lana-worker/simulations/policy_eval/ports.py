"""
ports.py — the swap seam + data contract for the Conversational-Policy eval harness.

WHAT THIS HARNESS TESTS
-----------------------
The three external design docs (LANA_CONVERSATIONAL_POLICY_ENGINEERING_v1,
LANA_CONVERSATIONAL_POLICY_PROMPT_v1, LANA_LINGO_v1 — none of which are in this repo)
describe a *single* conversational-policy call per turn:

    decide_turn(session, user_text) -> NextAction

That engine DOES NOT EXIST in the codebase. The live runtime is
`run_lana_unified_pipeline` (services/lana-worker/app/lana_unified_pipeline.py), which
returns a 5-tuple (reply, status, ctx, ui, event_draft) behind
POST /lana/sessions/{id}/messages — a different shape, and the in-repo architecture docs
(QUESTION_ASKING_AND_PROACTIVITY_v1 §2) explicitly commit to the OPPOSITE design
("Lana has no single 'ask' function — question-asking is spread across purpose-built
flows ... This is deliberate."). See POLICY_EVAL_HARNESS_HANDOFF.md for the full
architecture divergence.

So — exactly like the circles_zip/ harness — this is a spec-accurate stand-in with a
swappable backend:
  * stub  -> a reference policy that follows the PART 1-7 constitution (stub_policy.py),
             runnable today; proves the eval end-to-end and serves as the §PART-5 reference.
  * live  -> a best-effort adapter onto the CURRENT runtime (live_policy.py) so the
             tone/lingo/safety axes score REAL Lana output today; NextAction fields the
             runtime does not emit (kind/tool/why/typed-chip-actions) are None + flagged.
  * dry   -> a deterministic canned policy (backend.py) so `run_eval.py --dry-run` smoke-
             tests the whole pipeline with no OpenAI key and no live backend.

GUESSED CONVENTION (identical to circles_zip)
---------------------------------------------
Every field/value that the external spec left implementation-unspecified, or that has no
backing in the current DB schema, is marked with an inline `# GUESSED` comment and a
one-line reason. `grep -rn GUESSED policy_eval/` lists them; POLICY_EVAL_HARNESS_HANDOFF.md
summarizes them for Asjid/Yunchao sign-off. None of these are guesses about what the
PRODUCT should do (the docs are clear) — they are shapes the prose correctly left open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

SCHEMA_VERSION = "policy-eval/1"

# ---------------------------------------------------------------------------
# Enums (verbatim from LANA_CONVERSATIONAL_POLICY_PROMPT_v1 PART 5 + LANA_LINGO §3/§4)
# ---------------------------------------------------------------------------

# NextAction.kind — PART 5 output contract.
ActionKind = Literal[
    "reply",          # low-signal continue / acknowledge, no tool
    "ask_gap",        # ask one open rapport question
    "ground_place",   # ground a mentioned place ("which spot?")
    "bridge_offer",   # acknowledge -> bridge -> offer the one best next step (usually a tool)
    "capture_defer",  # mid-task interruption: note it, don't derail
    "handoff",        # hand to a human / resource (safety, crisis)
]

# Chip.action — PART 5 / LINGO §5.1: the typed intent is stable; the LABEL is generated.
ChipAction = Literal[
    "CREATE_GATHERING",
    "ACCEPT_INTRO",
    "SEE_MATCHES",
    "GROUND_PLACE",
    "ADD_DETAIL",
    "CONTINUE",
    "NOT_NOW",   # the graceful, non-punishing escape (LINGO §5.2)
    "SHARE",
]

# Candidate-goal kinds — PART 5 "Candidate-goal input shape" + engineering §C.2.
GoalKind = Literal["rapport_gap", "ungrounded_circle", "pending_offer", "capability"]

# Person-role for address selection — LANA_LINGO §3.1 taxonomy.
# NOTE: no `users.role` column exists in the DB (only founder_role: internal|founding_member,
# a cohort field). This person-role is therefore harness-supplied. # GUESSED source of truth.
UserRole = Literal[
    "parent", "expecting", "grandparent", "caregiver",
    "guardian", "relative", "family_friend", "other", "unspecified",
]

# Grammatical gender for ES/PT agreement — LANA_LINGO §4.1.
# NOTE: no `users.grammatical_gender` column exists (gender is deliberately NOT extracted per
# vertex_extract.py). Harness-supplied. # GUESSED source of truth.
GrammaticalGender = Literal["feminine", "masculine", "unknown"]

# users.locale exists in the DB (20260529000000): 'en'|'pt'|'es'.
Locale = Literal["en", "es", "pt"]

# ZIP unlock state — LANA_CIRCLES_ZIP_MASTER §D. Lives in the `zip_unlock` table per Asjid
# (zip5 PK, unlock_state, verified_active_count, unlock_threshold default 10, opened_at).
ZipUnlockState = Literal["closed", "warming", "open"]

# 5-tier relationship ladder — relationship_tier enum in the DB.
RelationshipTier = Literal["stranger", "nudge", "acquaintance", "direct", "irl_peer"]


# ---------------------------------------------------------------------------
# NextAction — the policy's output. Pydantic (not a dataclass) because stub_policy.py
# uses it directly as an OpenAI structured-output `response_format`.
# ---------------------------------------------------------------------------

class Chip(BaseModel):
    """A quick-reply chip. LINGO §5.1: fixed typed `action`, generated contextual `label`."""
    label: str = Field(description="Generated, lexicon-clean chip text (guardrail-checked).")
    action: ChipAction


class NextAction(BaseModel):
    """The single decision returned per turn — PART 5 output contract, verbatim shape."""
    kind: ActionKind
    utterance: str = Field(description="What Lana says (guardrail-checked before send).")
    tool: str | None = Field(
        default=None,
        description="A capability_id the user's state makes AVAILABLE, or null. "
                    "Must be a REGISTERED capability_id (see world_state.REGISTERED_CAPABILITIES).",
    )
    tool_args: dict[str, Any] = Field(default_factory=dict)
    defer_goal_id: str | None = Field(
        default=None, description="Set when kind == capture_defer: the goal being deferred."
    )
    chips: list[Chip] = Field(default_factory=list)
    why: str = Field(
        default="",
        description="One line rationale — for lana_audit_log + evals. Empty from `live` "
                    "backend (the current runtime does not emit a rationale field).",
    )


# ---------------------------------------------------------------------------
# Inputs to decide_turn — dataclasses (they are constructed by scenarios.py, never
# produced by an LLM, so they don't need to be Pydantic parse targets).
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    """A candidate goal the policy MAY pursue — PART 5 Goal shape / engineering §C.2.

    In the real system these come unified from four queues (capability_index, rapport_gaps,
    suggestion_queue, pending_signal_asks). No `candidate_goals` unifier exists in code and
    QUESTION_ASKING §3.3 explicitly declines the unification for v1 — so this unified Goal
    shape is harness-supplied.  # GUESSED (the unified shape; the four tables are real).
    """
    id: str
    kind: GoalKind
    summary: str
    value_hint: float = 0.5  # SOFT prior only (may carry surface_priority); policy may override.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Community:
    """A grounded circle the user belongs to (for privacy/over-reveal scenarios)."""
    circle_type: str
    place_name: str | None = None  # only revealed at Direct tier; None below it.
    tier: RelationshipTier = "stranger"


@dataclass
class WorldState:
    """Everything the policy sees about the user's world this turn — engineering §C.1.

    Fields map to real DB state where it exists (locale, phone_verified) and are harness-
    supplied placeholders where the DB has no backing (role, grammatical_gender, zip unlock,
    the required_state gate) — those are flagged # GUESSED at their source.
    """
    user_id: str
    locale: Locale = "en"
    role: UserRole = "unspecified"                 # GUESSED: no users.role column exists.
    grammatical_gender: GrammaticalGender = "unknown"  # GUESSED: no gender column exists.
    zip_unlock_state: ZipUnlockState = "closed"    # GUESSED: no zip_unlock table on this branch.
    phone_verified: bool = False                   # real: users.phone_verified_at IS NOT NULL.
    communities: list[Community] = field(default_factory=list)
    # Free-form extras a scenario may attach (e.g. a known neighbor at a given tier).
    extra: dict[str, Any] = field(default_factory=dict)

    def current_state_tokens(self) -> set[str]:
        """The state tokens capability required_state[] is checked against (engineering §C.3).

        # GUESSED vocabulary: capability_index.required_state is EMPTY on every seeded row,
        # so the token set that gates capabilities is invented here. Confirm the real gate
        # vocabulary ({zip_open}, {phone_verified}, ...) with Asjid before treating capability-
        # grounding failures as load-bearing.
        """
        tokens: set[str] = set()
        if self.zip_unlock_state == "open":
            tokens.add("zip_open")
        if self.phone_verified:
            tokens.add("phone_verified")
        return tokens


@dataclass
class TurnContext:
    """The full input to one decide_turn call."""
    user_text: str
    world: WorldState
    goals: list[Goal] = field(default_factory=list)
    recent: list[dict[str, str]] = field(default_factory=list)  # [{"role","content"}, ...]


# ---------------------------------------------------------------------------
# The swap seam
# ---------------------------------------------------------------------------

class PolicyPort(Protocol):
    """One method: decide the single next action for a turn. This is the whole contract a
    real `decide_turn` (or an adapter onto the current runtime) must satisfy."""

    def decide_turn(self, ctx: TurnContext) -> NextAction:
        ...
