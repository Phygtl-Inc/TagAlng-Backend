"""
stub_impl.py — the spec-accurate reference implementation of ports.py.

Written directly from LANA_CIRCLES_ZIP_MASTER_v1.md §B.2/§C.1/§C.2/§D.1/§D.4/U1-U4.
Pure functions, no I/O, fully deterministic given the inputs (no internal randomness —
population.py owns all randomness, seeded).

Reconciled 2026-07-28 with Asjid's reply, then REV-2 for the onion matcher. Up front:
  * §C (the onion matcher, MatcherStub) is now BUILT + deployed (branch `main`, PR #107/#114,
    migration 20260914's `score_onion_candidates_for_user`). This stub is no longer a
    placeholder for absent code — it MIRRORS the built algorithm exactly and is the parity
    target for LIVE:
        score        = circle_bonus + shared_concept_count
        circle_bonus = MAX(same place_ref → +3, same circle_type → +1)   # MAX, not SUM
        candidate set = confirmed AND non-dismissed AND place_ref IS NOT NULL (grounded)
        order = score desc, nickname asc ; p_limit clamp [1,50] ; p_min_score default 1
        NO proximity term (users store no coordinates); blocked pairs excluded.
  * §D (AreaStateStub.transition_zip) is already a RECOUNT internally — it re-derives the
    count from event.population_snapshot rather than mutating on an event. That happens to
    match the as-built reality (`recount_zip_unlock(zip5)` is pure over the DB), so the
    stub needed no structural change here; only the live wiring reshapes.
The verified-active predicate (_is_active_mom) was corrected to count accepted intros
(U2(c)) — see below.
"""

from __future__ import annotations

from datetime import datetime

from ports import (
    ActivationConfig,
    AreaStateConfig,
    CircleAffiliation,
    DisclosedCircleInfo,
    DisclosureTier,
    Mom,
    RankedCandidate,
    RingId,
    ScoringConfig,
    ZipEvent,
    ZipState,
)

# ---------------------------------------------------------------------------
# MatcherPort — §C the onion
# ---------------------------------------------------------------------------


def _grounded_confirmed(mom: Mom) -> list[CircleAffiliation]:
    """The candidate SET, exactly the funnel terminal set (rev-2): status='confirmed' AND
    dismissed_at IS NULL AND place_ref IS NOT NULL. suggested/dismissed/ungrounded rows
    NEVER score, on EITHER side of the pair."""
    return [
        c for c in mom.circles
        if c.status == "confirmed" and c.dismissed_at is None and c.place_ref is not None
    ]


def _shared_place_and_type(mom: Mom, candidate: Mom) -> tuple[bool, bool, str | None]:
    """same_place: the pair shares an exact place_ref. same_type: the pair shares a
    circle_type (at any place). Both computed over the confirmed+grounded candidate set on
    BOTH sides (rev-2). Returns (same_place, same_type, shared_place_ref) — the last is one
    shared place uuid (deterministic pick), matching the RPC's `shared_place_ref` column.
    place_ref is the real cross-user membership join key (guess #3)."""
    mom_rows = _grounded_confirmed(mom)
    cand_rows = _grounded_confirmed(candidate)
    mom_places = {c.place_ref for c in mom_rows}
    cand_places = {c.place_ref for c in cand_rows}
    mom_types = {c.circle_type for c in mom_rows}
    cand_types = {c.circle_type for c in cand_rows}
    shared_places = mom_places & cand_places
    same_place = bool(shared_places)
    same_type = bool(mom_types & cand_types)
    shared_place_ref = min(shared_places) if shared_places else None
    return same_place, same_type, shared_place_ref


def _default_proximity_bonus(mom: Mom, candidate: Mom) -> float:
    """RESOLVED (Asjid 2026-07-28 rev-2): the real matcher has NO proximity term (users
    store no coordinates; locality lives in the serving layer, not scoring). This is
    neutralized to a hard 0.0 — it is still CALLED below so its zero contribution is
    explicit, but it can never move a score. config.proximity_tiebreak_fn is likewise
    ignored: there is no proximity signal to tune. (guess #1 — now ANSWERED)"""
    return 0.0


def _clamp_limit(n_results: int) -> int:
    """RESOLVED (Asjid 2026-07-28 rev-2): the RPC clamps p_limit to [1, 50]."""
    return max(1, min(50, int(n_results)))


def _reporting_ring(same_place: bool, same_type: bool, concept_count: int) -> RingId:
    """Cosmetic §C.1-style label for backward-compatible sweep reporting ONLY. The BUILT
    matcher emits NO ring — this is derived purely from the score components (no
    proximity/adjacency, which have no real counterpart, guess #2) and is not a scoring or
    ordering input. Ordered tightest→loosest by circle strength."""
    if same_place and concept_count > 0:
        return "R1"
    if same_place:
        return "R2"
    if same_type and concept_count > 0:
        return "R3"
    if same_type:
        return "R5"   # type-only floor
    return "R4"       # concept-only (no shared place or type)


class MatcherStub:
    """§C — implements MatcherPort. REV-2: faithful mirror of the deployed onion algorithm
    (score = MAX(place +3, type +1) + shared_concept_count; confirmed+grounded only; order
    score desc, nickname asc; p_limit clamp [1,50]; p_min_score filter; no proximity)."""

    def __init__(
        self,
        zip_adjacency: dict[str, set[str]] | None = None,
        blocked_pairs: set[frozenset[str]] | None = None,
    ):
        # zip_adjacency — RESOLVED (Asjid 2026-07-28, reaffirmed rev-2): the built matcher
        # has NO adjacency/proximity. Retained (IGNORED by scoring) only so backend.get_backend
        # and the day-zero fixture don't ripple. `zip_centroids` is just a market registry. (#2)
        self.zip_adjacency = zip_adjacency or {}
        # blocked_pairs — the real matcher excludes blocked pairs (lana_is_blocked). Modeled
        # here as an optional set of unordered {a, b} pairs; empty by default. Hook for the
        # blend/gate invariant tests.
        self.blocked_pairs = blocked_pairs or set()

    def _is_blocked(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self.blocked_pairs

    def score_matches(
        self, mom: Mom, population: list[Mom], config: ScoringConfig
    ) -> list[RankedCandidate]:
        limit = _clamp_limit(config.n_results)  # RPC p_limit clamp [1,50]
        results: list[RankedCandidate] = []

        for candidate in population:
            if candidate.user_id == mom.user_id:
                continue
            if self._is_blocked(mom.user_id, candidate.user_id):
                continue  # both arms exclude blocked pairs (lana_is_blocked)

            same_place, same_type, shared_place_ref = _shared_place_and_type(mom, candidate)

            # Concept arm — gated by the DORMANCY toggle. When off (current-prod: the
            # 20260905 backfill / IDENTITY_CONCEPT_LINK_ENABLED not yet live), EVERY score is
            # circle-arm only, which is flag-off, not a scoring bug.
            shared_concepts = (
                mom.affinities & candidate.affinities
                if config.concept_arm_enabled else frozenset()
            )
            concept_count = len(shared_concepts)

            # Raw component columns (for offline re-weighting + RPC-column parity):
            same_place_bonus = config.exact_place_weight if same_place else 0.0
            same_type_bonus = config.same_type_weight if same_type else 0.0

            # circle_bonus = MAX(place, type) — place and type DO NOT STACK (rev-2 fidelity
            # rule; the SQL is MAX-over-pairs of place ELSE type, never a sum).
            circle_bonus = max(same_place_bonus, same_type_bonus)
            score = circle_bonus + config.affinity_weight * concept_count
            # NO proximity term (guess #1 RESOLVED) — the call contributes a hard 0.0.
            score += _default_proximity_bonus(mom, candidate)

            if score < config.p_min_score:
                continue  # RPC returns only rows with score >= p_min_score (default 1)

            results.append(
                RankedCandidate(
                    user_id=candidate.user_id,
                    score=score,
                    same_place=same_place,
                    same_type=same_type,
                    shared_affinities=tuple(sorted(shared_concepts)),
                    same_place_bonus=same_place_bonus,
                    same_type_bonus=same_type_bonus,
                    shared_concept_count=concept_count,
                    shared_concept_labels=tuple(sorted(shared_concepts)),
                    shared_place_ref=shared_place_ref,
                    ring=_reporting_ring(same_place, same_type, concept_count),
                )
            )

        # ORDER: score desc, then nickname asc. The harness has no nicknames, so user_id is
        # the deterministic nickname stand-in for the tiebreak.
        results.sort(key=lambda c: (-c.score, c.user_id))
        return results[:limit]


# ---------------------------------------------------------------------------
# DisclosurePort — §F the 5-tier ladder
# ---------------------------------------------------------------------------

# §F.2: stranger/nudge/acquaintance all resolve to "type only, no place" structurally —
# only the UI copy differs between them (§F.1's table), which is out of this port's scope.
# RESOLVED (Asjid 2026-07-28): this collapse is CONFIRMED (guess #8). In prod §F is
# enforced STRUCTURALLY (RLS + worker serializers), not by this call — the LIVE impl is a
# passthrough. This stub keeps the collapse as the §F reference so the §F.3 leak check has
# something to exercise.
_TYPE_ONLY_TIERS: frozenset[DisclosureTier] = frozenset({"stranger", "nudge", "acquaintance"})
_PLACE_REVEALED_TIERS: frozenset[DisclosureTier] = frozenset({"direct", "irl_peer"})


class DisclosureStub:
    """§F — implements DisclosurePort (reference for the structural rule)."""

    def filter_by_tier(
        self, circle: CircleAffiliation, requester_tier: DisclosureTier
    ) -> DisclosedCircleInfo:
        if requester_tier in _TYPE_ONLY_TIERS:
            # §F.3's acceptance criterion, verbatim: a Stranger-tier requester never
            # receives place_ref/place_name in any payload — enforced here by simply
            # never setting them, not by a client-side hide.
            return DisclosedCircleInfo(
                circle_type=circle.circle_type,
                place_name=None,
                place_ref=None,
                can_vouch=False,
            )
        # direct / irl_peer
        return DisclosedCircleInfo(
            circle_type=circle.circle_type,
            place_name=circle.place_name,  # serializer-derived display name (places.name)
            place_ref=circle.place_ref,
            can_vouch=(requester_tier == "irl_peer"),  # §F.1: vouching is IRL-peer only
        )


# ---------------------------------------------------------------------------
# AreaStatePort — §D the ZIP-unlock state machine
# ---------------------------------------------------------------------------


def _is_active_mom(mom: Mom, config: AreaStateConfig, as_of: datetime) -> bool:
    """U2 verified-active predicate — a mom counts toward her home_zip iff ALL three:
      (a) phone_verified_at IS NOT NULL (email verification also stamps it);
      (b) EXISTS a lana_sessions row with created_at in the last 30 days;
      (c) ≥1 confirmed thing: a confirmed non-dismissed circle_affiliation OR an `intros`
          row with status='accepted' where she is initiator or candidate.

    RESOLVED (Asjid 2026-07-28): (c) now counts accepted intros via
    Mom.has_confirmed_thing() — the "or intro" branch is real and durable, and omitting it
    UNDERCOUNTS vs prod. (b) checks session rows (Mom.has_recent_session), behaviorally
    equivalent to the old last_active_at reading. (guesses #4, #5)"""
    if not mom.phone_verified:
        return False
    if not mom.has_confirmed_thing():
        return False
    return mom.has_recent_session(as_of, config.active_session_window_days)


def _recount(population_snapshot: list[Mom], zip_code: str, config: AreaStateConfig, as_of: datetime) -> int:
    return sum(
        1
        for mom in population_snapshot
        if mom.home_zip == zip_code and _is_active_mom(mom, config, as_of)
    )


class AreaStateStub:
    """§D — implements AreaStatePort. RESOLVED (Asjid 2026-07-28): this is a RECOUNT, not
    an event-driven transition — it re-derives the count from scratch over
    event.population_snapshot and maps count→state, exactly like the as-built pure SQL
    function `recount_zip_unlock(zip5)` does over the DB. The `event` is just the
    harness's snapshot carrier; there is no real ZipEvent (see ports.py)."""

    def transition_zip(
        self, zip_state: ZipState, event: ZipEvent, config: AreaStateConfig
    ) -> ZipState:
        count = _recount(event.population_snapshot, event.zip_code, config, event.as_of)
        if count == 0:
            new_state = "closed"
        elif count < config.unlock_threshold:
            new_state = "warming"
        else:
            new_state = "open"
        # There is no literal ZIP_OPENED constant as-built. In prod the recount, inside its
        # transaction, stamps opened_at + the founding award + fires ONE celebration push
        # at the instant it first crosses threshold. opened_at PERSISTS: if a later recount
        # drops the count back below threshold the state returns to "warming" but opened_at
        # (and founding) do NOT un-stamp. This pure function has no side effects (no push),
        # so the harness/sweep detects the warming→open edge by diffing the returned state;
        # opened_at is carried forward here to model the persistence rule.
        opened_at = zip_state.opened_at or (event.as_of if new_state == "open" else None)
        return ZipState(
            zip_code=zip_state.zip_code,
            unlock_state=new_state,
            verified_active_count=count,
            unlock_threshold=config.unlock_threshold,
            opened_at=opened_at,
        )


# ---------------------------------------------------------------------------
# CircleActivationPort — §B.2 / U3
# ---------------------------------------------------------------------------


class CircleActivationStub:
    """§B.2/U3 — implements CircleActivationPort. Caller is expected to have already
    filtered circle_members to rows sharing one place_ref (the "activation" concept is
    inherently per-place, per §B.2: "≥2 confirmed members share the same place_ref").
    RESOLVED (Asjid 2026-07-28): in prod this is derived-at-read (`active = member_count
    ≥ 2`) inside POST /lana/circles/mine — no stored flag."""

    def is_active(
        self, circle_members: list[CircleAffiliation], config: ActivationConfig
    ) -> bool:
        confirmed_count = sum(1 for c in circle_members if c.status == "confirmed" and c.dismissed_at is None)
        return confirmed_count >= config.activation_threshold
