"""
ports.py — the swap seam + data contract for the Conversational-Policy eval harness.

WHAT THIS HARNESS TESTS
-----------------------
The three external design docs (LANA_CONVERSATIONAL_POLICY_ENGINEERING_v1,
LANA_CONVERSATIONAL_POLICY_PROMPT_v1, LANA_LINGO_v1 — none of which are in this repo)
describe a *single* conversational-policy call per turn returning one NextAction.

THAT ENGINE NOW EXISTS (this file was revised 2026-08-18 against the shipped code).
It is:

    app/policy/decide.py:499
        decide_turn(*, user_id, session_ctx, history, user_message,
                    answering_question=None) -> NextAction | None
        (None = "no decision" — the caller falls through to the legacy path, which is
         behaviourally the same as kind='handoff')
    app/policy/decide.py:37    the real NextAction dataclass
    app/policy/decide.py:59    NextAction.routing_dict() — the wire view (omits chips)
    app/policy/world.py:105    world_state(user_id) -> {user, area, circles, states}
    app/policy/goals.py:391    candidate_goals(...)

This module was originally written against a spec for an architecture that did not
exist, so several of its `# GUESSED` markers were FALSE — they asserted "no such column
exists" for columns that had since shipped. Those are replaced with citations below.
`# GUESSED` now survives ONLY where the real system is still genuinely undetermined.

Backends (see backend.py):
  * stub   -> a reference policy that follows the PART 1-7 constitution (stub_policy.py);
              runnable today, and the PART-5 reference implementation.
  * inproc -> the REAL decide_turn, imported and called IN-PROCESS (live_policy.py).
              kind / why / defer_goal_id / distress_turn / chip labels are REAL here.
              Needs a DB, a service-role key and a real user_id, and CANNOT honour a
              scenario-pinned world unless world injection is explicitly enabled — see
              live_policy.InProcPolicy for the fail-closed rules.
  * live   -> HTTP adapter onto POST /lana/sessions/{id}/messages (live_policy.py). Kept
              on purpose: it exercises the whole shipped pipeline (guardrail, ui_actions,
              session plumbing, the legacy fall-through) end to end, and needs no repo
              import path and no service-role key — only an account.
  * dry    -> a deterministic canned policy (backend.py) so `run_eval.py --dry-run`
              smoke-tests the whole pipeline with no OpenAI key and no live backend.

GUESSED / FLAGGED CONVENTION (identical to circles_zip)
------------------------------------------------------
`# GUESSED` = the real system genuinely has not decided this yet.
`# FLAGGED` = adapter lossiness: the thing is real, but THIS backend cannot observe it.
`grep -rnE "GUESSED|FLAGGED" policy_eval/` lists them. Nothing that is now backed by a
migration or by app/ code may carry either marker — a stale GUESSED is worse than none,
because it tells a reader to distrust something that is actually ground truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

SCHEMA_VERSION = "policy-eval/1"

# ---------------------------------------------------------------------------
# Enums (verbatim from LANA_CONVERSATIONAL_POLICY_PROMPT_v1 PART 5 + LANA_LINGO §3/§4)
# ---------------------------------------------------------------------------

# NextAction.kind — PART 5 output contract.
# REAL: app/policy/decide.py:26 `KINDS`. Kept in the same order as the tuple there so a diff is
# obvious. This list must stay a superset of nothing and an exact mirror of KINDS — it is a
# pydantic Literal, so a kind the real policy emits and this list lacks does not degrade
# gracefully: it fails validation, the backend call raises, and run_eval scores a HARD_FAIL against
# a decision that was correct. That is a false positive on the most expensive axis.
ActionKind = Literal[
    "reply",          # low-signal continue / acknowledge, no tool
    # Added 2026-08-25, mirroring decide.py:27. Asking someone more about the thing THEY just
    # raised, in Lana's own words, with no goal_id. Deliberately excluded from decide.py's
    # `_ASK_KINDS` annoyance streak (decide.py:193-196) because it is ordinary conversation, not
    # the interrogation pattern that guard exists to stop — and the dead-end backstop now names it
    # the FIRST-choice revision (decide.py:487), ahead of ask_gap, so it is not a rare kind.
    "follow_thread",
    "ask_gap",        # ask one open rapport question (its question is appended AFTER decide_turn)
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

# Candidate-goal kinds — REAL: the `kind` values app/policy/goals.py emits
# (rapport_gap:197, ungrounded_circle:243, circle_offer:282, pending_offer:324 & 358,
# capability:382), unified by candidate_goals(...) at goals.py:391. `circle_offer` was
# missing from the harness's list; the other four matched.
GoalKind = Literal[
    "rapport_gap", "ungrounded_circle", "circle_offer", "pending_offer", "capability",
]

# Person-role for address selection — LANA_LINGO §3.1, AS SHIPPED.
# REAL: `users.role`, added by
# supabase/migrations/20260909120000_policy_role_gender_lingo.sql:14 with
#   check (role is null or role in
#          ('parent','expecting','grandparent','caregiver','guardian','relative'))
# and surfaced to the policy by app/policy/world.py:139 (world["user"]["role"]).
# The check constraint is the authority: 'family_friend' / 'other' / 'unspecified' were
# harness inventions the DB would REJECT, so they are gone. NULL ("not inferred yet") is
# expressed as Python None, exactly as the column expresses it — see WorldState.role.
UserRole = Literal[
    "parent", "expecting", "grandparent", "caregiver", "guardian", "relative",
]

# Grammatical gender for ES/PT agreement — LANA_LINGO §4.1, AS SHIPPED.
# REAL: `users.grammatical_gender`, same migration (20260909120000:17), with
#   check (grammatical_gender is null or grammatical_gender in ('feminine','masculine'))
# There is NO third "unknown" value in the DB and there must not be one here. The column
# comment is explicit: "null = rephrase neutrally, never default feminine". So unknown is
# modelled the way SQL models it — the ABSENCE of a value (None), not an enum member.
# Consequence for the harness: `world.grammatical_gender is None` IS the neutral-rephrase
# trigger (WorldState.gender_is_null), and a scenario that pins gender writes exactly one
# of these two strings. WorldState.__post_init__ maps the harness's old "unknown"/"" spelling
# onto None so an out-of-date caller degrades to the neutral (safe) branch rather than
# silently claiming a gender.
GrammaticalGender = Literal["feminine", "masculine"]

# users.locale exists in the DB (20260529000000): 'en'|'pt'|'es'.
Locale = Literal["en", "es", "pt"]

# ZIP unlock state — REAL: `zip_unlock.unlock_state`, created in
# supabase/migrations/20260906120000_circles_places_phase_a.sql:201
# (zip5 PK, unlock_state check in ('closed','warming','open'), verified_active_count,
# unlock_threshold default 10, opened_at). app/policy/world.py:141 turns state=='open'
# into the `zip_open` token.
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
    # WIRE FORM IS A STRING, DELIBERATELY. PART 5 specifies `tool_args` as a mapping, but this
    # model is handed to OpenAI as a strict `response_format`, and strict mode requires every
    # object to declare `additionalProperties: false` — which an open-ended dict[str, Any] cannot,
    # by definition. A `dict` field therefore makes EVERY decide_turn call fail with a 400 before
    # the policy is ever exercised. Carrying the args as a JSON string keeps the field expressible
    # while preserving the contract at the read site (see the `tool_args` property below).
    tool_args_json: str = Field(
        default="",
        description='Arguments for `tool` as a JSON object STRING, e.g. \'{"when":"saturday"}\'. '
                    'Empty string when the tool takes no arguments.',
    )

    @property
    def tool_args(self) -> dict[str, Any]:
        """PART 5's mapping view of `tool_args_json`. Malformed or non-object JSON reads as {} —
        no check consumes this field today, so a parse failure must not take down a run whose
        real subject is the utterance and the tool choice."""
        raw = self.tool_args_json.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    defer_goal_id: str | None = Field(
        default=None, description="Set when kind == capture_defer: the goal being deferred."
    )
    chips: list[Chip] = Field(default_factory=list)
    distress_turn: bool = Field(
        default=False,
        description="The person is in pain / ill / wrung out AS THEY WRITE and wants nothing "
                    "done. REAL field: app/policy/decide.py:55, judged by the policy (no regex) "
                    "and ENFORCED by _apply_distress_gate (decide.py:300), which downgrades the "
                    "kind AND CLEARS CHIPS. checks.check_chips exempts distress turns from the "
                    "chip rules because of that gate — a chipless distress turn is the product "
                    "working, not a violation.",
    )
    why: str = Field(
        default="",
        description="One line rationale — written to lana_audit_log and read by evals. REAL "
                    "field (app/policy/decide.py:56, exposed via routing_dict()). Only the `live` "
                    "HTTP adapter cannot see it, because the messages endpoint does not return "
                    "the routing payload.",
    )


# ---------------------------------------------------------------------------
# Inputs to decide_turn — dataclasses (they are constructed by scenarios.py, never
# produced by an LLM, so they don't need to be Pydantic parse targets).
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    """A candidate goal the policy MAY pursue — PART 5 Goal shape / engineering §C.2.

    REAL: app/policy/goals.py:391 `candidate_goals(user_id, world, ...)` IS the unifier, and
    it emits exactly this shape — {id, kind, summary, value_hint, context}. A capability goal
    carries id="cap:<capability_id>" and context={"capability_id": ...} (goals.py:378-386);
    that prefix is how the `inproc` backend recovers which capability an offer pitched, since
    the real NextAction has no `tool` field of its own.

    value_hint is a WEAK prior (surface_priority/10 for capability goals) that the policy may
    ignore — goals.py and world.capabilities_available both say so explicitly.
    """
    id: str
    kind: GoalKind
    summary: str
    value_hint: float = 0.5  # SOFT prior only (may carry surface_priority); policy may override.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Community:
    """A circle_affiliations row — app/policy/world.py:56 selects
    (circle_key, circle_type, grounded, status, detail, place_ref).

    `confirmed` mirrors `status == 'confirmed'`, which is the EXACT condition world.py:134
    uses to emit the `has_circle` state token. `grounded` mirrors the boolean column of the
    same name (a place has been pinned). Both default False so a fixture that only wants a
    neighbour for a privacy scenario does not accidentally grant the user a confirmed circle.
    """
    circle_type: str
    place_name: str | None = None  # only revealed at Direct tier; None below it.
    tier: RelationshipTier = "stranger"
    confirmed: bool = False        # circle_affiliations.status == 'confirmed'
    grounded: bool = False         # circle_affiliations.grounded


@dataclass
class WorldState:
    """Everything the policy sees about the user's world this turn.

    This is the harness's mirror of app/policy/world.py:105 `world_state(user_id)`, which
    returns {user:{nickname,locale,role,grammatical_gender,kids_count,verified},
             area:{state,count,threshold}, circles:[...], states:[...]}.
    Every field below is backed by a real column; the citation is on the field.
    """
    user_id: str
    locale: Locale = "en"                      # users.locale (20260529000000): 'en'|'pt'|'es'
    # users.role — NULL means "not inferred yet", which is the default state of a new account.
    role: UserRole | None = None
    # users.grammatical_gender — NULL means "rephrase neutrally, never default feminine"
    # (the column comment, verbatim). None is the neutral-rephrase trigger; see gender_is_null.
    grammatical_gender: GrammaticalGender | None = None
    # zip_unlock.unlock_state for the user's home ZIP -> world["area"]["state"].
    zip_unlock_state: ZipUnlockState = "closed"
    # world.py:130 emits `verified` when phone_verified_at OR email_verified_at is set — NOT
    # phone alone. The harness previously called this `phone_verified`, which was both the
    # wrong name and a narrower condition than the one that actually gates anything.
    verified: bool = False
    # users.home_zip -> the `has_home_zip` token (world.py:132). A user with no home ZIP has
    # no area at all, so `area.state` is empty and zip_open can never hold.
    home_zip: str | None = None
    communities: list[Community] = field(default_factory=list)
    # Free-form extras a scenario may attach (e.g. a known neighbor at a given tier).
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Back-compat shim, deliberately one-directional. The harness used to spell "gender not
        # known" as the string "unknown" and "role not known" as "unspecified"; neither value can
        # exist in the DB. Normalising them to None means an out-of-date caller lands on the
        # NEUTRAL branch (rephrase, don't guess) instead of asserting a gender/role that the
        # check constraint would reject. Anything else is left alone so a genuine typo still
        # shows up as a wrong value rather than being silently swallowed.
        if self.grammatical_gender in ("unknown", "", "null", "none"):
            self.grammatical_gender = None  # type: ignore[assignment]
        if self.role in ("unspecified", "", "null", "none"):
            self.role = None  # type: ignore[assignment]

    @property
    def gender_is_null(self) -> bool:
        """SQL-NULL semantics: gender not known -> rephrase neutrally (LINGO §4.2)."""
        return self.grammatical_gender is None

    def current_state_tokens(self) -> set[str]:
        """The state tokens `capability_index.required_state` is checked against.

        NOT GUESSED ANY MORE. This is a line-for-line mirror of app/policy/world.py:124-134,
        which is the only place the vocabulary is produced:

            verified      <- users.phone_verified_at OR users.email_verified_at
            has_home_zip  <- users.home_zip is set
            zip_open      <- zip_unlock.unlock_state == 'open' for that home ZIP
            has_circle    <- any circle_affiliations row with status == 'confirmed'

        There is no `phone_verified` token in the real system — that was a harness invention
        (the seed migration's illustrative column comment "e.g. {phone_verified}" was read as
        if it were data). Emitting it here made every availability comparison meaningless,
        because no capability row has ever required it.
        """
        tokens: set[str] = set()
        if self.verified:
            tokens.add("verified")
        if self.home_zip:
            tokens.add("has_home_zip")
        if self.zip_unlock_state == "open":
            tokens.add("zip_open")
        if any(c.confirmed for c in self.communities):
            tokens.add("has_circle")
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

@dataclass
class TurnNote:
    """What the BACKEND knows about its own honesty for the turn it just decided.

    A NextAction says what Lana did. This says what the harness was able to observe while she
    did it — and it exists because the two are not the same thing once a backend touches the
    real system. `decide_turn(user_id, ...)` reads the world from the DB (app/policy/world.py:105),
    so a scenario that PINS a world is not necessarily the world the decision was made in, and a
    scenario-shaped PASS from an account-shaped world is a lie the report would otherwise tell.

    Carried out-of-band rather than as NextAction fields on purpose: NextAction is handed to
    OpenAI as a strict `response_format` by stub_policy, so every field added to it becomes
    something a policy MODEL is asked to fill in. Harness bookkeeping has no business there.

    Read by run_eval via `getattr(backend, "last_note", None)`, so a PolicyPort that has
    nothing to declare (stub, dry) simply doesn't define it and gets the honoured-by-
    construction default.
    """

    # "scenario" — the backend consumed ctx.world directly, so the pinned world IS the world.
    # "account"  — the world came from the DB for some real user; the pinned world was IGNORED.
    # "injected" — the pinned world was forced into the real code path at a seam (harness-supplied).
    # "seeded"   — the pinned world was WRITTEN to the DB for the real user before the decision,
    #              so decide_turn's own reads (world_state AND the goals/claims/capability reads
    #              injection cannot reach) all saw one consistent world. Only possible against a
    #              LOCAL stack (local_world.py refuses otherwise) — on the shared dev project this
    #              would be seeding a contended account from an eval.
    world_source: Literal["scenario", "account", "injected", "seeded"] = "scenario"
    # The state tokens actually in force, when the backend can read them. None = couldn't tell,
    # which is treated as "not honoured", never as "matches".
    observed_state_tokens: set[str] | None = None
    # decide_turn returned None: no decision, legacy fall-through. The turn the user would have
    # received was produced by code this harness did not run, so the turn is UNSCORABLE.
    decision_declined: bool = False
    # Free-text lines that MUST be surfaced in the report next to any verdict from this turn.
    flags: list[str] = field(default_factory=list)


class PolicyPort(Protocol):
    """One method: decide the single next action for a turn. This is the whole contract a
    real `decide_turn` (or an adapter onto the current runtime) must satisfy.

    Optionally, a port may also expose `last_note: TurnNote` describing what it could and
    could not honour on the last call (see TurnNote). It is not part of the Protocol because
    a port that honours everything should not have to say so.
    """

    def decide_turn(self, ctx: TurnContext) -> NextAction:
        ...
