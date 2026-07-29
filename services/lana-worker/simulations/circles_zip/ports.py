"""
ports.py — the swap seam for the Circles + ZIP-Unlock sim/eval harness.

Everything in this file is a Protocol (structural interface) + the data models and
config objects that cross it. StubImpl (stub_impl.py) implements these now. LiveImpl
(live_impl.py) implements the SAME protocols against Asjid's real backend. The harness
(sweep.py) only ever imports these Protocol types + whichever impl SIM_BACKEND selects —
swapping backends is a one-file change (live_impl.py), never a harness change.

RECONCILED 2026-07-28 with Asjid's authoritative reply
------------------------------------------------------
The original build assumed "nothing in Circles/ZIP exists in the repo yet." That was
WRONG — Phase 1 IS built (branch `circles`, migrations 20260906/20260907/20260908, worker
in services/lana-worker, live in dev). The grep missed it only because the as-built names
diverge from spec: the table is `zip_unlock` (NOT a `zips` alter); places live on
`place_ref → places(id)` (NOT inline place_id/place_name/lat/lng columns); there is no
literal `ZIP_OPENED` constant. This file has been reconciled against Asjid's written
corrections — it was NOT verified against branch `circles` (not visible from here).

Every field/default below is now one of:
  (a) confirmed spec/as-built,
  (b) "# RESOLVED (Asjid 2026-07-28): ..." — a rev-1 guess, answered, or
  (c) "# RESOLVED (Asjid 2026-07-28 rev-2): ..." — the onion-matcher corrections below.

REV-2 (Asjid 2026-07-28) — the onion matcher (§C) is now BUILT, wired, and deployed
------------------------------------------------------------------------------------
The rev-1 block "§C is genuinely NOT BUILT, owned by a separate developer, keep the stub
as the reference" is LIFTED. §C shipped: circles branch merged to `main` (PR #107/#114),
migrations 20260913 (`count_shared_concepts_for_user`) + 20260914 (the scoring RPC
`score_onion_candidates_for_user`), prod worker live (Cloud Run rev 00010). There is now a
REAL RPC + a REAL algorithm the stub must mirror faithfully, and a real serving/blend path.
As a result ZERO of the original 8 guesses remain open: #1 (proximity) and #7 (n_results)
are now RESOLVED below. The scoring is:

    score        = circle_bonus + shared_concept_count
    circle_bonus = MAX over circle pairs of (same place_ref → +3) ELSE (same circle_type → +1)
                   *** place and type do NOT stack — it is MAX, not SUM ***
    shared_concept_count = COUNT(DISTINCT shared concept_ids) over PUBLIC, non-dismissed claims
    candidate set = status='confirmed' AND dismissed_at IS NULL AND place_ref IS NOT NULL
                    (suggested/dismissed/ungrounded rows NEVER score; blocked pairs excluded)
    order = score desc, then nickname asc ; p_limit default 20 clamp [1,50] ; p_min_score 1
    NO proximity term, NO block-scoping (users store no coordinates; locality lives in the
    serving layer). This file was reconciled against Asjid's rev-2 written corrections — it
    was NOT verified against branch `main` (not visible from here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Literal, Protocol

# ---------------------------------------------------------------------------
# Enums — as-built (branch `circles`)
# ---------------------------------------------------------------------------

CircleType = Literal[
    "school", "faith", "fitness", "kids_activity", "neighborhood",
    "hobby", "support", "heritage", "friends", "other",
]  # circle_affiliations.circle_type CHECK constraint — 10 values, confirmed as-built

AffiliationStatus = Literal["suggested", "confirmed"]  # confirmed as-built

AffiliationSource = Literal["chat_extraction", "invite_confirmed", "profile_add"]
# RESOLVED (Asjid 2026-07-28): source is THREE-valued as-built. `profile_add` is the
# profile "Add" surface (self-stated), written with confidence 1.0. The original guess
# only modeled the first two.

ZipUnlockState = Literal["closed", "warming", "open"]  # zip_unlock.unlock_state

IntroStatus = Literal["accepted"]
# RESOLVED (Asjid 2026-07-28): intros are DURABLE rows in the `intros` table. Only
# status='accepted' is counted by the recount (U2) and the founding stamp — other
# lifecycle states may exist but do not count. (Original guess treated intros as
# ephemeral matcher output and did not model them at all — see Mom.accepted_intros.)

# §F.1's ladder has 5 named tiers. Below Direct, nothing place-identifying leaves the
# worker, so stranger/nudge/acquaintance collapse to one disclosure level (type only, no
# place). The port still accepts all 5 tier names as input so copy-layer distinctions
# aren't lost upstream.
# RESOLVED (Asjid 2026-07-28): the tier-collapse is CORRECT/confirmed (original guess #8).
# §F is enforced STRUCTURALLY (RLS + worker serializers), NOT by per-request logic — there
# is no per-tier data variation to un-collapse below Direct.
DisclosureTier = Literal["stranger", "nudge", "acquaintance", "direct", "irl_peer"]

RingId = Literal["R1", "R2", "R3", "R4", "R5"]

ZipEventType = Literal["signup", "activity_recount"]
# RESOLVED (Asjid 2026-07-28): the ZIP machine is a RECOUNT, not an event-driven
# transition. There is NO `ZipEvent` and no discrete event type in the real backend — one
# atomic SQL function `recount_zip_unlock(zip5)` re-derives verified_active_count from
# scratch and maps count→state, read-repaired from 3 call sites (no cron, no trigger).
# This literal + the ZipEvent dataclass below are retained ONLY as a harness-internal
# driver: they carry the in-memory population snapshot the pure recount runs over (the
# DB's role in prod). The stub's transition_zip already recounts, so it is faithful — only
# the live wiring changes shape (see live_impl.py / AreaStatePort).


# ---------------------------------------------------------------------------
# Data models — as-built column names where a real column exists
# ---------------------------------------------------------------------------

@dataclass
class CircleAffiliation:
    """`circle_affiliations` (migration 20260906). Place identity is a FK, NOT inline
    columns. Uniqueness as-built: UNIQUE (user_id, circle_key) WHERE dismissed_at IS NULL
    — one live (non-dismissed) row per user per concept slug; re-mentioning a dismissed
    concept makes a new row. Not enforced structurally here (population.py mints unique
    ids); noted for parity."""
    id: str
    user_id: str
    circle_type: CircleType  # 10-type CHECK, as-built
    circle_key: str
    # RESOLVED (Asjid 2026-07-28): real `circle_key` is a PER-USER slug
    # (^[a-z][a-z0-9_]{1,63}$), extractor-emitted or slugified from the user's phrase. It
    # dedupes ONE user's mentions — it is NOT the cross-user join key. Membership joins on
    # `place_ref`; cross-user semantics live in the 768-dim `embedding` column. Asjid:
    # "your place_id stand-in models the real join better than real circle_keys would —
    # keep it." So the harness keeps using the place identifier (now `place_ref`) as the
    # join key and treats circle_key as the per-user slug it actually is. (original guess #3)
    place_ref: str | None
    # RESOLVED (Asjid 2026-07-28): place identity is `place_ref uuid → places(id)` (the
    # canonical shared `places` table keyed by google_place_id, one row per real-world
    # place) — NOT the inline place_id/place_name/lat/lng columns the original guess used.
    # `place_ref` is ALSO the cross-user membership join key (see circle_key note above).
    status: AffiliationStatus  # suggested|confirmed
    detail: str | None
    source: AffiliationSource  # 3-valued — see AffiliationSource
    invited_by: str | None
    # RESOLVED (Asjid 2026-07-28): kept separate from membership — an invite never implies
    # a circle row. (Growth attribution proper is users.invited_by; see Mom.invited_by.)
    embedding: tuple[float, ...] | None = None
    # RESOLVED (Asjid 2026-07-28 rev-2): the `embedding vector(768)` column still exists, but
    # the DEPLOYED onion scoring RPC does NOT use it — the built cross-user arm is
    # `shared_concept_count` over PUBLIC non-dismissed identity claims (`claim_concept_links`),
    # not embedding cosine. So this stays None with no loss of fidelity; Mom.affinities is the
    # stand-in for shared CONCEPT ids (see Mom.affinities). (rev-1 guess #3, embedding half —
    # was "blocked on the matcher"; the matcher shipped and does not read this column)
    confidence: float = 0.6  # default 0.6 as-built; profile_add rows are written 1.0
    created_at: datetime | None = None
    dismissed_at: datetime | None = None  # soft-delete

    # --- NOT a stored column ---------------------------------------------------------
    place_name: str | None = None
    # RESOLVED (Asjid 2026-07-28): §F is STRUCTURAL — place NAME is NOT a column on
    # circle_affiliations. It lives on `places(id).name` and only ever leaves the backend
    # through worker serializers (e.g. POST /lana/circles/mine returns place_name/
    # place_address by joining place_ref → places). Kept here as a denormalized DISPLAY
    # convenience so the §F disclosure reference (DisclosureStub) can demonstrate the
    # place-name redaction below Direct. Do NOT treat as a real column.

    @property
    def grounded(self) -> bool:
        """RESOLVED (Asjid 2026-07-28): `grounded` is a GENERATED STORED column
        (`place_ref IS NOT NULL`) — it can't be written and can't drift. Modeled as a
        derived read-only property for exactly that reason (was a writable bool field in
        the original guess)."""
        return self.place_ref is not None


@dataclass
class Intro:
    """`intros` table (as-built) — DURABLE rows. RESOLVED (Asjid 2026-07-28): accepted
    intros are counted by BOTH the recount (U2) and the founding stamp; they are NOT the
    ephemeral matcher output the original guess assumed. A mom with ≥1 accepted intro
    counts toward her home_zip even with zero confirmed circles. (original guess #4)"""
    id: str
    initiator: str  # user_id
    candidate: str  # user_id
    status: IntroStatus = "accepted"
    created_at: datetime | None = None


@dataclass
class Mom:
    """A synthetic (or real) user, as far as this harness needs to know."""
    user_id: str
    home_zip: str  # users.home_zip — the EXACT key the ZIP gate/recount uses. No
    # adjacency/topology exists as-built (see zip_adjacency in population.py).
    circles: list[CircleAffiliation] = field(default_factory=list)
    accepted_intros: list[Intro] = field(default_factory=list)
    # RESOLVED (Asjid 2026-07-28): modeled now (was the unmodeled "or intro" branch).
    # See Intro + Mom.has_confirmed_thing(). Without this, unlock counts UNDERCOUNT vs
    # prod. (original guess #4)
    phone_verified_at: datetime | None = None
    # RESOLVED (Asjid 2026-07-28): as-built this is `users.phone_verified_at timestamptz`
    # (a timestamp, not a bool). Email verification ALSO stamps it — treat as "verified by
    # any method." U2(a) predicate is `phone_verified_at IS NOT NULL`.
    last_session_at: datetime | None = None
    # RESOLVED (Asjid 2026-07-28): there is NO `users.last_active_at` column. "Active" =
    # EXISTS a `lana_sessions` row with created_at > now − 30d. Behaviorally equivalent to
    # the original reading; source of truth is session rows. Modeled here as the most
    # recent session timestamp (MAX(lana_sessions.created_at)) — its presence + recency is
    # what the recount checks. (original guess #5)
    affinities: frozenset[str] = frozenset()
    # RESOLVED (Asjid 2026-07-28 rev-2): this tag-set is the stand-in for a user's PUBLIC,
    # non-dismissed identity CONCEPT ids. The built onion matcher's second arm is
    # `shared_concept_count = COUNT(DISTINCT shared concept_ids)` (reading `claim_concept_links`),
    # so |mom.affinities & candidate.affinities| is the faithful stub for that arm — +1 each.
    # (Supersedes the rev-1 "embedding cosine stand-in / blocked on the matcher" note: the
    # deployed scoring counts shared concepts, not embedding similarity.) Subject to the
    # DORMANCY caveat — see ScoringConfig.concept_arm_enabled.
    invited_by: str | None = None
    # RESOLVED (Asjid 2026-07-28): `users.invited_by` is growth attribution, set ONCE at
    # invite redemption — deliberately NOT membership (an invite never creates a circle).
    founding_area: str | None = None
    founding_earned_at: datetime | None = None  # both exist as-built.

    def confirmed_circles(self) -> list[CircleAffiliation]:
        return [c for c in self.circles if c.status == "confirmed" and c.dismissed_at is None]

    @property
    def phone_verified(self) -> bool:
        """U2(a): phone_verified_at IS NOT NULL (email verification also stamps it)."""
        return self.phone_verified_at is not None

    def has_recent_session(self, as_of: datetime, window_days: int) -> bool:
        """U2(b): EXISTS a lana_sessions row with created_at in the last `window_days`."""
        if self.last_session_at is None:
            return False
        return (as_of - self.last_session_at).days <= window_days

    def has_confirmed_thing(self) -> bool:
        """U2(c): ≥1 confirmed thing — a confirmed, non-dismissed circle_affiliation OR an
        `intros` row with status='accepted' where she is initiator or candidate.
        RESOLVED (Asjid 2026-07-28): the "or intro" branch is real and durable — both
        count. (original guess #4)"""
        return bool(self.confirmed_circles()) or bool(self.accepted_intros)


@dataclass
class ZipState:
    """Models the net-new `zip_unlock` table (migration 20260908) — NOT a `zips` alter.
    RESOLVED (Asjid 2026-07-28): there is no `zips` table extension; rows in `zip_unlock`
    are created ON DEMAND at first recount."""
    zip_code: str  # = zip_unlock.zip5 (PK)
    unlock_state: ZipUnlockState = "closed"
    verified_active_count: int = 0
    unlock_threshold: int = 10  # zip_unlock.unlock_threshold — default 10, PER-ZIP
    opened_at: datetime | None = None
    # RESOLVED (Asjid 2026-07-28): stamped when the count first crosses threshold
    # (warming→open), inside the recount transaction, alongside the founding stamp + one
    # celebration push. PERSISTS even if a later recount drops the count back below
    # threshold (state returns to warming; opened_at/founding do NOT un-stamp).


@dataclass
class ZipEvent:
    """HARNESS-INTERNAL driver, not a real entity.
    RESOLVED (Asjid 2026-07-28): the real backend has NO event — `recount_zip_unlock(zip5)`
    is a PURE function over the DB, read-repaired from 3 call sites. This carries the
    in-memory population snapshot the pure recount runs over (standing in for the DB) plus
    a reproducible reference `now` so seeded sweeps stay deterministic."""
    event_type: ZipEventType
    zip_code: str
    population_snapshot: list[Mom]
    as_of: datetime


# ---------------------------------------------------------------------------
# Schema-parity models — as-built tables the sweep does not yet exercise but that the
# harness should represent so the interface stays honest. (Documented, not wired.)
# ---------------------------------------------------------------------------

@dataclass
class Place:
    """Canonical `places` table (net-new; the deployed matcher joins peers on place_ref →
    this row). One row per real-world place, keyed by google_place_id.
    RESOLVED (Asjid 2026-07-28): `place_type` is ADVISORY; `claimed_by` is always NULL in
    Phase 2. This is the row `circle_affiliations.place_ref` points at, and the source of
    the place_name/place_address the worker serializers emit."""
    id: str
    google_place_id: str
    name: str
    address: str | None = None
    place_type: str | None = None   # advisory
    claimed_by: str | None = None   # Phase 2: always null


@dataclass
class PlaceFeature:
    """`place_features` table (net-new) — learned shared attributes (serving/rapport, not a
    scoring input to the deployed onion RPC).
    RESOLVED (Asjid 2026-07-28): one truth per (place × key × sub_group); owner-source
    rows are NEVER overwritten by rapport writes."""
    place_ref: str
    key: str
    sub_group: str | None
    value: str
    source: str  # owner-source rows are protected from rapport overwrites


@dataclass
class CircleInvite:
    """`circle_invites` table (net-new). Token links resolve at `/i/<token>`."""
    id: str
    token: str
    inviter: str                     # user_id
    affiliation_id: str | None = None
    created_at: datetime | None = None


@dataclass
class CircleInviteRedemption:
    """`circle_invite_redemptions` — UNIQUE (invite, user): the rate-limit substrate
    (30 redemptions/hour/invite). Redeem also triggers the ZIP recount and sets the
    redeemer's users.invited_by (growth attribution, not membership)."""
    invite_id: str
    user_id: str
    redeemed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Config objects — the sweep surface
# ---------------------------------------------------------------------------

@dataclass
class ScoringConfig:
    """§C scoring — OFFLINE RE-WEIGHTING of the RPC's returned components.
    REV-2 (Asjid 2026-07-28): the real weights (place +3, type +1, +1/concept) are SQL
    CONSTANTS inside `score_onion_candidates_for_user`, NOT tunable server params. They are
    NOT swept against a live server. But the RPC returns each component as its own column
    (same_place_bonus, same_type_bonus, shared_concept_count) *precisely* so an offline
    consumer can re-derive score under alternative weights — that IS the intended tuning
    loop, and it is what these fields drive against the stub. The stub is now the faithful
    parity mirror of the built algorithm (not a placeholder for absent code)."""
    exact_place_weight: float = 3.0   # SQL constant +3 for a shared place_ref
    same_type_weight: float = 1.0     # SQL constant +1 for a shared circle_type
    affinity_weight: float = 1.0      # SQL constant +1 per DISTINCT shared concept
    # *** circle_bonus = MAX(place_present·exact_place_weight, type_present·same_type_weight) —
    # place and type do NOT stack; the SQL is MAX-over-pairs (place ELSE type), not a sum.
    # score = circle_bonus + affinity_weight·shared_concept_count. ***
    concept_arm_enabled: bool = True
    # DORMANCY (rev-2): the concept arm reads `claim_concept_links`, populated only when
    # IDENTITY_CONCEPT_LINK_ENABLED is on AND the 20260905 backfill has run (PR #96 pending).
    # Until then EVERY real score is circle-arm only — a 0-concept score is flag-off, NOT a
    # scoring bug. Default True mirrors the ALGORITHM (concepts contribute); set False to
    # reproduce current-prod dormancy (circle-arm only). The sweep exercises both.
    proximity_tiebreak_fn: Callable[[Mom, Mom], float] | None = None
    # RESOLVED (Asjid 2026-07-28 rev-2): there is NO proximity/distance term at all — users
    # store no coordinates; locality lives in the SERVING layer, not the matcher. The rev-1
    # placeholder is neutralized (it must not contribute score). Kept only so the stub's
    # signature and older sweep call-sites don't ripple. (original guess #1 — now ANSWERED)
    p_min_score: int = 1
    # RESOLVED (Asjid 2026-07-28 rev-2): the RPC's p_min_score default is 1 — a peer is
    # returned only when circle_bonus + shared_concept_count >= p_min_score. (part of #7)
    n_results: int = 20
    # RESOLVED (Asjid 2026-07-28 rev-2): this is the RPC's p_limit, default 20, CLAMPED to
    # [1, 50]. The serving/blend layer fetches max(20, limit·4) candidates and returns <=5 to
    # the turn (see BlendConfig). (original guess #7 — now ANSWERED)


@dataclass
class ActivationConfig:
    activation_threshold: int = 2
    # RESOLVED (Asjid 2026-07-28): activation is DERIVED-AT-READ — `active = member_count
    # ≥ 2`, computed per place inside POST /lana/circles/mine. No stored flag. (U3)


@dataclass
class AreaStateConfig:
    unlock_threshold: int = 10  # zip_unlock.unlock_threshold — per-ZIP, default 10 (U1)
    active_session_window_days: int = 30
    # RESOLVED (Asjid 2026-07-28): U2(b) is `EXISTS lana_sessions WHERE created_at > now −
    # 30d` — behaviorally equivalent to the original last_active_at reading; source of
    # truth is session rows, not a user column. (original guess #5)


GateMode = Literal["off", "soft", "hard"]


@dataclass
class GateConfig:
    """The env knobs Asjid flagged the harness should parameterize. Documented here for
    parity; only unlock_threshold (on AreaStateConfig/ZipState) is exercised by the
    current sweep — the rest gate CONSUMPTION / copy cadence the harness does not yet
    model."""
    zip_unlock_gate: GateMode = "soft"
    # LANA_ZIP_UNLOCK_GATE (off|soft|hard, default soft). Gate touches CONSUMPTION of
    # OTHERS' supply ONLY — it never blocks create/host/invite, and FAILS OPEN on any error.
    circle_gap_max_open: int = 2               # LANA_CIRCLE_GAP_MAX_OPEN
    rapport_circle_every_n: int = 3            # LANA_RAPPORT_CIRCLE_EVERY_N
    redemptions_per_hour_per_invite: int = 30  # constant, NOT an env var
    onion_matcher_enabled: bool = True
    # RESOLVED (Asjid 2026-07-28 rev-2): LANA_ONION_MATCHER gates the SERVING/BLEND path
    # (app.onion_blend.blend_onion_matches), default ON, FAILS OPEN — when off/erroring the
    # find-peers list is byte-identical to the pre-onion vector-only list. The scoring RPC
    # itself is UNFLAGGED (always available to service-role callers).


@dataclass
class BlendConfig:
    """The find-peers serving/blend layer (`app.onion_blend.blend_onion_matches(peers,
    user_id, limit=5)`), which merges onion candidates into the vector-matched peer list.
    REV-2 (Asjid 2026-07-28) — documented for parity; the current sweep exercises the RPC
    scoring, not the blend merge (that lives in the find-peers chat turn, not a HTTP route).

    SEAT RULE: an onion peer NOT already vector-matched earns a seat ONLY when
    same_place_bonus > 0 AND the caller has a confirmed circle at that place. Concept-only or
    type-only strangers NEVER earn a seat. Onion-only rows keep `similarity_score: null` (no
    invented cosine). Disclosure tag is caller-relative and never a place name."""
    serve_cap: int = 5             # blend returns <=5 rows to the turn (limit)
    labels_per_card: int = 3       # merges <=3 concept labels per card (RPC returns <=50)
    # candidate fetch = max(20, limit*4); seat requires same_place_bonus > 0 (see docstring).


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

@dataclass
class RankedCandidate:
    """One row of the onion RPC's output. REV-2 (Asjid 2026-07-28): the built RPC
    `score_onion_candidates_for_user` returns each scoring COMPONENT as its own column so an
    offline consumer can re-weight (see ScoringConfig). `same_place_bonus`/`same_type_bonus`
    are the RAW component values (0/3 and 0/1) — the score applies MAX(place, type), they do
    NOT stack. `ring` is a spec-§C.1 artifact the built matcher does NOT emit; it is derived
    by the stub for backward-compatible reporting only and is not a scoring/ordering input."""
    user_id: str            # RPC: peer_user_id
    score: float
    same_place: bool
    same_type: bool
    shared_affinities: tuple[str, ...]
    # --- real RPC output columns (rev-2) ---------------------------------------------
    same_place_bonus: float = 0.0   # RPC column: 0 or +3 (shares a confirmed place_ref)
    same_type_bonus: float = 0.0    # RPC column: 0 or +1 (shares a circle_type)
    shared_concept_count: int = 0   # RPC column: COUNT(DISTINCT shared concept_ids)
    shared_concept_labels: tuple[str, ...] = ()  # RPC column: <=50 labels (serving trims to 3)
    shared_place_ref: str | None = None          # RPC column: bare uuid of the shared place
    ring: RingId | None = None      # spec-§C.1 artifact — reporting-only, see class docstring


@dataclass
class DisclosedCircleInfo:
    """What a requester at a given tier is allowed to see about ONE candidate's circles.
    RESOLVED (Asjid 2026-07-28): §F is STRUCTURAL (RLS + worker serializers), not a
    callable filter. In LIVE mode DisclosurePort is a PASSTHROUGH (the user's OWN /mine
    view always sees its own place; OTHERS' views are gated by RLS+serializers, not by
    this call). The stub keeps the tier-collapse behavior as the §F reference."""
    circle_type: CircleType | None  # visible from stranger tier up (§F.1)
    place_name: str | None  # ONLY populated at direct/irl_peer (§F.2) — None below that.
    # In the stub this is the serializer-derived display name (see CircleAffiliation.place_name).
    place_ref: str | None  # renamed from place_id — the FK identifier that must never
    # leak below Direct (§F.3's acceptance criterion, as demonstrated by the stub).
    can_vouch: bool  # True only at irl_peer (§F.1's "circle vouching")


# ---------------------------------------------------------------------------
# Protocols — the actual seam. StubImpl and LiveImpl both satisfy these.
# ---------------------------------------------------------------------------

class MatcherPort(Protocol):
    def score_matches(
        self, mom: Mom, population: list[Mom], config: ScoringConfig
    ) -> list[RankedCandidate]:
        """§C — the onion matcher. Returns candidates ordered by (score desc, nickname asc),
        clamped to p_limit in [1,50], filtered to score >= p_min_score.
        REV-2 (Asjid 2026-07-28): §C is now BUILT + deployed. LIVE backs this with the SQL
        RPC `score_onion_candidates_for_user(p_user_id, p_limit=20, p_min_score=1)`
        (migration 20260914, service-role only), or the gated wrapper
        `app.onion.score_onion_candidates()` which enforces the §D.2 peers ZIP gate BEFORE
        the RPC and fails OPEN on gate errors. INVARIANT: any caller hitting the RPC directly
        bypasses the gate — the wrapper is the gate. STUB mirrors the algorithm faithfully
        (circle_bonus is MAX not sum; only confirmed+grounded+non-dismissed rows score)."""
        ...


class DisclosurePort(Protocol):
    def filter_by_tier(
        self, circle: CircleAffiliation, requester_tier: DisclosureTier
    ) -> DisclosedCircleInfo:
        """§F — strips place_ref/place_name below Direct.
        RESOLVED (Asjid 2026-07-28): enforced STRUCTURALLY in prod (RLS with zero client
        policies + worker serializers), NOT by a callable tier filter. The stub keeps this
        method as the §F reference (tier-collapse below Direct is confirmed, guess #8); the
        LIVE impl is a passthrough."""
        ...


class AreaStatePort(Protocol):
    def transition_zip(
        self, zip_state: ZipState, event: ZipEvent, config: AreaStateConfig
    ) -> ZipState:
        """The ZIP unlock derivation.
        RESOLVED (Asjid 2026-07-28): this is a RECOUNT, not an event-driven transition —
        `recount_zip_unlock(zip5)` re-derives verified_active_count from scratch and maps
        count→state. The stub already recounts over event.population_snapshot, so it is
        faithful; only the LIVE wiring reshapes (POST /lana/area/progress recounts on read,
        or call the recount_zip_unlock RPC directly). `event` is a harness driver, not a
        real entity."""
        ...


class CircleActivationPort(Protocol):
    def is_active(
        self, circle_members: list[CircleAffiliation], config: ActivationConfig
    ) -> bool:
        """§B.2/U3 — a place is active when >= activation_threshold confirmed members share
        the same place_ref. circle_members is expected to be pre-filtered to one
        place_ref's rows by the caller.
        RESOLVED (Asjid 2026-07-28): derived-at-read as `active = member_count ≥ 2`,
        computed per place inside POST /lana/circles/mine (no stored flag)."""
        ...
