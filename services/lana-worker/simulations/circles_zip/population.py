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

Two fixture invariants worth knowing before you tune a knob:
  * every generated row is checked against the REAL constraints by validate_population()
    before it is handed back — notably `status='confirmed' ⇒ place_ref is not null`
    (20260916120000). A row that cannot exist in the DB describes an impossible world, so
    the generator DEMOTES it rather than emitting it (see _make_circle);
  * `blocked_pair_rate` generates symmetric `user_blocks` pairs, drawn from moms who already
    share a place so the matcher's exclusion path is genuinely exercised (see _blocked_pairs).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ports import CircleAffiliation, CircleType, Intro, Mom

ALL_CIRCLE_TYPES: tuple[CircleType, ...] = (
    "school", "faith", "fitness", "kids_activity", "neighborhood",
    "hobby", "support", "heritage", "friends", "other",
)

# circle_affiliations.circle_key CHECK, verbatim from
# 20260906120000_circles_places_phase_a.sql:103 — a per-user slug, NOT the join key.
CIRCLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ALLOWED_STATUSES = frozenset({"suggested", "confirmed"})
ALLOWED_SOURCES = frozenset({"chat_extraction", "invite_confirmed", "profile_add"})


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

    confirmed_rate: float = 0.7  # fraction of circle rows DRAWN as status='confirmed'
    grounded_given_confirmed: float = 0.95  # grounded ⇔ place_ref is set (generated column)
    # NB: a row drawn confirmed-but-ungrounded is DEMOTED to 'suggested' — it cannot exist in
    # the real DB (see _make_circle + the constraint note there). So the effective confirmed
    # rate is confirmed_rate · grounded_given_confirmed; this knob now controls how many
    # would-be confirmations get parked as ungrounded grounding candidates instead.
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

    blocked_pair_rate: float = 0.02
    # `user_blocks` (20260618130000) — both onion arms drop blocked peers via
    # lana_is_blocked (20260914120000:76,:113), which is SYMMETRIC, so pairs are unordered.
    # Kept DELIBERATELY SMALL: blocks are rare in real data, and every blocked pair removes a
    # would-be match from the coverage measurement, so a large value would quietly depress the
    # headline number. Pairs are drawn from moms who ALREADY share a place — blocking two
    # people who could never have matched exercises nothing. n_pairs = round(rate · n_moms).


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
    grounded_prob = config.grounded_given_confirmed if confirmed else config.grounded_given_suggested
    grounded = rng.random() < grounded_prob  # grounded ⇔ place_ref present (generated col)

    place_ref = None
    place_name = None
    if grounded:
        places = place_pool[(home_zip, circle_type)]
        place_ref = rng.choice(places)
        place_name = place_ref  # stub: no real place-name generation, id doubles as name

    if confirmed and place_ref is None:
        # A CONFIRMED ROW WITHOUT A PLACE CANNOT EXIST. Migration
        # 20260916120000_circle_place_mandatory.sql adds
        #     check (status <> 'confirmed' or place_ref is not null)
        # ("a community's place is MANDATORY"), and that same migration REPAIRS pre-existing
        # rows with exactly this demotion:
        #     update circle_affiliations set status='suggested'
        #      where status='confirmed' and place_ref is null;
        # Generating them anyway (the old grounded_given_confirmed=0.95 produced ~5% of
        # confirmed rows ungrounded) meant every measurement over those rows described an
        # impossible world — and, worse, they were invisible in the results because the
        # matcher's own confirmed+grounded filter silently dropped them. So we demote instead
        # of dropping: an ungrounded mention stays a legal 'suggested' grounding candidate,
        # which is what it is in prod. validate_population() re-asserts this per row.
        confirmed = False
    status = "confirmed" if confirmed else "suggested"

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
        circle_key=f"{circle_type}_{idx}",
        # RESOLVED (Asjid 2026-07-28): real circle_key is a per-user slug that dedupes ONE
        # user's mentions, NOT the cross-user join key. The matcher joins on place_ref (the
        # kept stand-in), so circle_key is cosmetic here. (guess #3)
        # It is now derived from (type, per-user idx) rather than from place_ref so that the
        # partial unique index `(user_id, circle_key) where dismissed_at is null`
        # (20260906120000:125-127) holds BY CONSTRUCTION — the old place-derived key collided
        # whenever one mom drew the same place twice. It also satisfies the column's
        # `^[a-z][a-z0-9_]{1,63}$` CHECK for every circle_type. Both are asserted in
        # validate_population().
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


@dataclass
class GeneratedPopulation:
    """What generate_population hands back. A dataclass rather than a widening tuple so the
    next fixture axis (blocks were the third) doesn't break every call site again."""
    moms: list[Mom]
    zip_adjacency: dict[str, set[str]]
    blocked_pairs: set[frozenset[str]]


def validate_population(population: list[Mom]) -> None:
    """Fail CLOSED on any generated row the real schema would reject. Runs on every
    generate_population() call, so a distribution knob can never silently reintroduce an
    impossible world (the 'confirmed with no place' case shipped for weeks precisely because
    nothing asserted it). Cheap: one pass, no I/O.

    Only constraints that ACTUALLY exist in a migration are asserted — an invented rule here
    would be a false gate failure on a legitimate fixture:
      * status='confirmed' ⇒ place_ref is not null  (20260916120000:20-22)
      * status ∈ (suggested, confirmed)             (20260906120000:106-107)
      * circle_type ∈ the 10-value CHECK             (20260906120000:100-102)
      * source ∈ the 3-value CHECK                   (20260906120000:109-110)
      * confidence ∈ [0,1]                           (20260906120000:112)
      * circle_key ~ ^[a-z][a-z0-9_]{1,63}$          (20260906120000:103)
      * UNIQUE (user_id, circle_key) where dismissed_at is null (20260906120000:125-127)
    """
    violations: list[str] = []
    for mom in population:
        live_keys: set[str] = set()
        for c in mom.circles:
            if c.status == "confirmed" and c.place_ref is None:
                violations.append(
                    f"{c.id}: status='confirmed' with place_ref IS NULL violates "
                    f"circle_affiliations_confirmed_has_place (20260916120000)"
                )
            if c.status not in ALLOWED_STATUSES:
                violations.append(f"{c.id}: status={c.status!r} not in {sorted(ALLOWED_STATUSES)}")
            if c.circle_type not in ALL_CIRCLE_TYPES:
                violations.append(f"{c.id}: circle_type={c.circle_type!r} fails the 10-value CHECK")
            if c.source not in ALLOWED_SOURCES:
                violations.append(f"{c.id}: source={c.source!r} fails the source CHECK")
            if not (0.0 <= c.confidence <= 1.0):
                violations.append(f"{c.id}: confidence={c.confidence} outside [0,1]")
            if not CIRCLE_KEY_RE.match(c.circle_key):
                violations.append(f"{c.id}: circle_key={c.circle_key!r} fails ^[a-z][a-z0-9_]{{1,63}}$")
            if c.dismissed_at is None:
                if c.circle_key in live_keys:
                    violations.append(
                        f"{c.id}: duplicate live circle_key={c.circle_key!r} for {mom.user_id} "
                        f"violates UNIQUE (user_id, circle_key) WHERE dismissed_at IS NULL"
                    )
                live_keys.add(c.circle_key)
    if violations:
        head = "\n  ".join(violations[:10])
        more = f"\n  ... and {len(violations) - 10} more" if len(violations) > 10 else ""
        raise ValueError(
            f"generated population violates {len(violations)} real DB constraint(s):\n  {head}{more}"
        )


def _blocked_pairs(
    rng: random.Random, population: list[Mom], rate: float
) -> set[frozenset[str]]:
    """Draw symmetric blocks from pairs that WOULD otherwise match (they already share a
    confirmed+grounded place_ref, i.e. a +3 pair). Blocking two people who could never have
    scored is a vacuous fixture — it would leave the exclusion path untested while looking
    exercised. Unordered frozensets because lana_is_blocked is symmetric."""
    if rate <= 0 or not population:
        return set()
    by_place: dict[str, set[str]] = {}
    for mom in population:
        for c in mom.circles:
            if c.status == "confirmed" and c.dismissed_at is None and c.place_ref:
                by_place.setdefault(c.place_ref, set()).add(mom.user_id)
    pairs = sorted(
        {
            (a, b)
            for members in by_place.values()
            for a in sorted(members)
            for b in sorted(members)
            if a < b
        }
    )
    if not pairs:
        return set()
    n = min(len(pairs), int(round(rate * len(population))))
    return {frozenset(p) for p in rng.sample(pairs, n)}


def generate_population(config: PopulationConfig) -> GeneratedPopulation:
    """Returns moms + zip_adjacency + blocked_pairs. Deterministic for a given config.seed."""
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
                # STRICTLY inside the window: the SQL is `created_at > now() - interval
                # '30 days'` (20260906120000:302-304), so a session at exactly the edge does
                # NOT count (Mom.has_recent_session now matches). Drawing the edge day here
                # would silently produce "active" moms the recount treats as inactive, and
                # active_within_days_rate would stop meaning what it says.
                last_session_at = config.now - timedelta(days=rng.randint(0, config.active_session_window_days - 1))
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

    # Blocks last, so adding this axis leaves every earlier rng draw (and therefore every
    # earlier fixture) byte-identical for a given seed.
    blocked = _blocked_pairs(rng, population, config.blocked_pair_rate)

    validate_population(population)  # fail closed — never hand back an impossible world
    return GeneratedPopulation(moms=population, zip_adjacency=adjacency, blocked_pairs=blocked)
