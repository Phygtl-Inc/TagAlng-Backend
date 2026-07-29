"""
population.py — synthetic mom population generator for the Circles + ZIP-Unlock harness.

Every distribution parameter is named on PopulationConfig and is fair game for the
parameter sweep (sweep.py) alongside the scoring/activation/area-state configs in
ports.py. Fully seeded — no wall-clock, no unseeded randomness — so a given
PopulationConfig always produces the identical population, making sweep results
reproducible and diffable.

Reconciled 2026-07-28 with Asjid's reply, then REV-2 for the onion matcher. The population
exercises all THREE onion scoring arms so stub↔built parity is measurable:
  * place overlap (+3) — `places_per_type_per_zip` (smaller ⇒ more moms share an exact
    place_ref within a zip+type);
  * type overlap (+1) — circle_types are global, so any two moms can share a circle_type;
  * concept overlap (+1 each) — the `affinity_*` knobs model each mom's PUBLIC identity
    CONCEPT ids (the built concept arm counts DISTINCT shared ones). `affinity_vocab_size`
    is the OVERLAP knob (smaller vocab ⇒ more shared concepts per pair).
Also retains rev-1's DURABLE accepted intros (the `intros` table): a mom with ≥1 accepted
intro counts toward her home_zip's verified-active total even with zero confirmed circles;
without them the unlock count UNDERCOUNTS vs prod (guess #4). Field names track the as-built
schema (place_ref, phone_verified_at, session rows) — see ports.py.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ports import CircleAffiliation, CircleType, Intro, Mom

ALL_CIRCLE_TYPES: tuple[CircleType, ...] = (
    "school", "faith", "fitness", "kids_activity", "neighborhood",
    "hobby", "support", "heritage", "friends", "other",
)


@dataclass
class PopulationConfig:
    seed: int = 42
    now: datetime = field(default_factory=lambda: datetime(2026, 7, 24))  # fixed reference
    # "now" — never datetime.now() — so a given seed always reproduces identically.

    n_zips: int = 5
    moms_per_zip: int = 30
    # Adjacency topology: zips arranged in a ring, each adjacent to its `adjacency_ring`
    # nearest neighbors on either side.
    # RESOLVED (Asjid 2026-07-28, reaffirmed rev-2): there is NO adjacency concept as-built —
    # the ZIP gate keys on exact users.home_zip, and `zip_centroids` is just a seeded market
    # registry. The DEPLOYED onion matcher confirms this: it has no zip/proximity scoping at
    # all. This synthetic ring has no real counterpart and is NOT invested in; the stub now
    # IGNORES it for scoring (kept only so this config surface doesn't ripple). (guess #2)
    adjacency_ring: int = 1

    circles_per_mom_max: int = 4
    circles_per_mom_weights: tuple[float, ...] = (0.15, 0.35, 0.30, 0.15, 0.05)  # P(0),P(1),P(2),P(3),P(4)
    circle_types: tuple[CircleType, ...] = ALL_CIRCLE_TYPES
    places_per_type_per_zip: int = 3  # SMALLER = more clustering (higher chance two moms
    # in the same zip+type share the exact same place_ref, which is what makes R1/R2
    # matches and circle activation possible at all)

    confirmed_rate: float = 0.7  # fraction of circle rows with status='confirmed'
    grounded_given_confirmed: float = 0.95  # grounded ⇔ place_ref is set (generated column)
    grounded_given_suggested: float = 0.3
    profile_add_rate: float = 0.2  # RESOLVED (Asjid 2026-07-28): source is 3-valued; this
    # is the fraction of rows written via the profile "Add" surface (source='profile_add',
    # self-stated, confidence 1.0). The rest are source='chat_extraction'. (guess: source)

    phone_verified_rate: float = 0.6  # fraction with phone_verified_at stamped (any method)
    active_within_days_rate: float = 0.7  # fraction of moms whose most recent session
    # (lana_sessions.created_at) falls within active_session_window_days of `now`
    active_session_window_days: int = 30

    accepted_intro_rate: float = 0.15  # RESOLVED (Asjid 2026-07-28): fraction of moms who
    # get ≥1 accepted intro (as initiator or candidate). Accepted intros are durable rows
    # counted by the recount + founding stamp — some recipients have no confirmed circle,
    # so this is what keeps unlock counts from undercounting vs prod. (guess #4)

    # Concept arm (rev-2): `affinities` model each mom's PUBLIC, non-dismissed identity
    # CONCEPT ids — the built onion matcher's second arm counts DISTINCT shared ones (+1
    # each; see ScoringConfig.concept_arm_enabled / the dormancy caveat). affinity_vocab_size
    # is the concept-OVERLAP knob: a smaller shared vocab ⇒ more shared concepts per pair ⇒
    # more concept-arm score. Sweep it to exercise the concept arm at varying densities.
    affinity_vocab_size: int = 12
    affinities_per_mom_max: int = 5
    affinities_per_mom_weights: tuple[float, ...] = (0.10, 0.25, 0.30, 0.20, 0.10, 0.05)  # P(0..5)

    invite_rate: float = 0.3  # fraction of (non-spam) moms with invited_by set
    # Invite-spam agents (§I anti-gaming check): a small number of "inviters" who
    # generate many invited_by edges pointing at low-quality recruits (unverified
    # and/or inactive) — used to test that founding-eligibility tracks real engaged
    # moms, not raw invite volume.
    spam_inviter_rate: float = 0.05  # fraction of moms who ARE spam inviters
    spam_recruits_per_spammer: int = 15
    spam_recruit_verified_rate: float = 0.05  # spam recruits are rarely phone-verified
    spam_recruit_active_rate: float = 0.05  # and rarely active


def _weighted_count(rng: random.Random, weights: tuple[float, ...]) -> int:
    return rng.choices(range(len(weights)), weights=list(weights), k=1)[0]


def _zip_adjacency(n_zips: int, ring: int) -> dict[str, set[str]]:
    """A simple ring topology: zip_i is adjacent to zip_(i±1..ring), wrapping around.
    RESOLVED (Asjid 2026-07-28, reaffirmed rev-2): no real counterpart — the deployed onion
    matcher has no proximity/adjacency scoping, so the stub now IGNORES this. Retained only
    so generate_population's return signature and the day-zero fixture don't ripple. (guess #2)"""
    zips = [f"Z{i:03d}" for i in range(n_zips)]
    adjacency: dict[str, set[str]] = {z: set() for z in zips}
    for i, z in enumerate(zips):
        for d in range(1, ring + 1):
            adjacency[z].add(zips[(i + d) % n_zips])
            adjacency[z].add(zips[(i - d) % n_zips])
    return adjacency


def _place_pool(zips: list[str], circle_types: tuple[CircleType, ...], places_per_type_per_zip: int) -> dict[tuple[str, CircleType], list[str]]:
    """Synthetic place_ref pool. Each id stands for a row in the canonical `places` table
    (keyed by google_place_id in prod); here the id doubles as the display name too."""
    pool: dict[tuple[str, CircleType], list[str]] = {}
    for z in zips:
        for ct in circle_types:
            pool[(z, ct)] = [f"{z}_{ct}_{i}" for i in range(places_per_type_per_zip)]
    return pool


def _make_circle(
    rng: random.Random,
    user_id: str,
    idx: int,
    home_zip: str,
    circle_types: tuple[CircleType, ...],
    place_pool: dict[tuple[str, CircleType], list[str]],
    config: PopulationConfig,
) -> CircleAffiliation:
    circle_type = rng.choice(circle_types)
    confirmed = rng.random() < config.confirmed_rate
    status = "confirmed" if confirmed else "suggested"
    grounded_prob = config.grounded_given_confirmed if confirmed else config.grounded_given_suggested
    grounded = rng.random() < grounded_prob  # grounded ⇔ place_ref present (generated col)

    place_ref = None
    place_name = None
    if grounded:
        places = place_pool[(home_zip, circle_type)]
        place_ref = rng.choice(places)
        place_name = place_ref  # stub: no real place-name generation, id doubles as name

    # source is 3-valued; profile_add rows are self-stated with confidence 1.0.
    if rng.random() < config.profile_add_rate:
        source = "profile_add"
        confidence = 1.0
    else:
        source = "chat_extraction"
        confidence = round(rng.uniform(0.4, 0.95), 2)

    return CircleAffiliation(
        id=f"{user_id}_circle_{idx}",
        user_id=user_id,
        circle_type=circle_type,
        circle_key=place_ref or f"{circle_type}_{idx}",
        # RESOLVED (Asjid 2026-07-28): real circle_key is a per-user slug that dedupes ONE
        # user's mentions, NOT the cross-user join key. The matcher joins on place_ref (the
        # kept stand-in), so circle_key is cosmetic here. (guess #3)
        place_ref=place_ref,
        place_name=place_name,  # display-only convenience (not a stored column) — see ports.py
        status=status,
        detail=None,
        source=source,
        invited_by=None,  # set separately, never implied by circle creation
        embedding=None,   # rev-2: the deployed onion RPC does NOT read embedding — its
                          # cross-user arm is shared CONCEPTS (Mom.affinities), so None here
                          # loses no fidelity (see ports.py CircleAffiliation.embedding)
        confidence=confidence,
        created_at=config.now - timedelta(days=rng.randint(0, 90)),
        dismissed_at=None,
    )
    # NB: `grounded` is a generated read-only property on CircleAffiliation (place_ref IS
    # NOT NULL) — it is deliberately NOT passed to the constructor.


def generate_population(config: PopulationConfig) -> tuple[list[Mom], dict[str, set[str]]]:
    """Returns (population, zip_adjacency). Deterministic for a given config.seed."""
    rng = random.Random(config.seed)
    zips = [f"Z{i:03d}" for i in range(config.n_zips)]
    adjacency = _zip_adjacency(config.n_zips, config.adjacency_ring)
    place_pool = _place_pool(zips, config.circle_types, config.places_per_type_per_zip)
    affinity_vocab = [f"aff_{i}" for i in range(config.affinity_vocab_size)]

    population: list[Mom] = []
    all_ids: list[str] = []

    for z in zips:
        for i in range(config.moms_per_zip):
            user_id = f"{z}_mom_{i:03d}"
            all_ids.append(user_id)

            n_circles = min(
                _weighted_count(rng, config.circles_per_mom_weights),
                config.circles_per_mom_max,
            )
            circles = [
                _make_circle(rng, user_id, idx, z, config.circle_types, place_pool, config)
                for idx in range(n_circles)
            ]

            n_affinities = min(
                _weighted_count(rng, config.affinities_per_mom_weights),
                config.affinities_per_mom_max,
            )
            affinities = frozenset(rng.sample(affinity_vocab, n_affinities)) if n_affinities else frozenset()

            phone_verified = rng.random() < config.phone_verified_rate
            phone_verified_at = config.now - timedelta(days=rng.randint(0, 120)) if phone_verified else None
            if rng.random() < config.active_within_days_rate:
                last_session_at = config.now - timedelta(days=rng.randint(0, config.active_session_window_days))
            else:
                last_session_at = config.now - timedelta(days=rng.randint(config.active_session_window_days + 1, 200))

            population.append(
                Mom(
                    user_id=user_id,
                    home_zip=z,
                    circles=circles,
                    phone_verified_at=phone_verified_at,
                    last_session_at=last_session_at,
                    affinities=affinities,
                    invited_by=None,
                )
            )

    by_id = {m.user_id: m for m in population}

    # Accepted intros — durable `intros` rows (guess #4, RESOLVED). Generated as a separate
    # pass and attached to BOTH participants so each counts toward their own home_zip. Some
    # recipients have zero confirmed circles, so this is precisely the "or intro" branch of
    # U2(c) that would otherwise undercount the verified-active total vs prod.
    intro_seq = 0
    for uid in all_ids:
        if rng.random() < config.accepted_intro_rate:
            candidates = [i for i in all_ids if i != uid]
            if not candidates:
                continue
            other = rng.choice(candidates)
            intro = Intro(
                id=f"intro_{intro_seq}",
                initiator=uid,
                candidate=other,
                status="accepted",
                created_at=config.now - timedelta(days=rng.randint(0, 60)),
            )
            intro_seq += 1
            by_id[uid].accepted_intros.append(intro)
            by_id[other].accepted_intros.append(intro)

    # Invite edges — kept as a separate pass so invited_by is never entangled with
    # circle creation (invite ≠ membership, modeled faithfully here: invited_by is set on
    # the Mom independent of whether they have any circles at all).
    non_spam_ids = [uid for uid in all_ids if rng.random() >= config.spam_inviter_rate]
    spam_inviter_ids = [uid for uid in all_ids if uid not in non_spam_ids]

    for uid in non_spam_ids:
        if uid in by_id and rng.random() < config.invite_rate:
            candidates = [i for i in non_spam_ids if i != uid]
            if candidates:
                by_id[uid].invited_by = rng.choice(candidates)

    # Spam inviters: overwrite a batch of recruits' invited_by + degrade their
    # verified/active status, so founding-eligibility (which requires real engagement)
    # doesn't reward raw invite volume — this is the §I acceptance check's fixture.
    for spammer in spam_inviter_ids:
        recruit_pool = [uid for uid in all_ids if uid != spammer]
        recruits = rng.sample(recruit_pool, min(config.spam_recruits_per_spammer, len(recruit_pool)))
        for r in recruits:
            mom = by_id[r]
            mom.invited_by = spammer
            mom.phone_verified_at = (
                config.now - timedelta(days=rng.randint(0, 120))
                if rng.random() < config.spam_recruit_verified_rate
                else None
            )
            if rng.random() >= config.spam_recruit_active_rate:
                mom.last_session_at = config.now - timedelta(days=rng.randint(config.active_session_window_days + 1, 200))

    return population, adjacency
