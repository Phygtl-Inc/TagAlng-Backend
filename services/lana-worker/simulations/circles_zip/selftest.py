"""
selftest.py — adversarial self-check: prove the MECHANICAL checks actually FIRE on violations.

Same job as policy_eval/selftest.py, for the circles/zip harness. A green `sweep.py` run is
worthless if its acceptance checks cannot fail: every gate in sweep.GATE_CHECKS is fed a
deliberately broken implementation here and must report passed=False. Where a check could
plausibly be over-eager, the HONEST implementation is run through the same check too, so a
false positive shows up as a failure rather than as reassuring red.

Runs with no API key, no server, no DB — pure fixtures. `python selftest.py` (exit 0 = every
detection fires).

The planted violations are the real divergences this harness exists to catch:
  * place+type scored as a SUM instead of MAX(3,1)      -> onion_scoring_parity
  * zero-score strangers emitted (the RPC has no such row) -> onion_scoring_parity
  * suggested/dismissed/ungrounded rows scored           -> onion_scoring_parity
  * p_limit honoured raw instead of clamped to [1,50]    -> onion_scoring_parity
  * the type-only day-zero floor row dropped             -> day_zero_floor
  * blocked pairs ignored                                -> blocked_pair_exclusion
  * founding eligibility bought with invite volume       -> founding_volume_invariance
  * place_ref leaked at stranger tier                    -> disclosure_correctness
  * a confirmed circle with no place_ref generated       -> population.validate_population
"""

from __future__ import annotations

import copy
import sys
import types
from dataclasses import replace
from datetime import timedelta

import sweep
from backend import Backend
from population import (
    ALL_CIRCLE_TYPES,
    PopulationConfig,
    generate_population,
    validate_population,
)
from ports import (
    ActivationConfig,
    AreaStateConfig,
    CircleAffiliation,
    DisclosedCircleInfo,
    Mom,
    RankedCandidate,
    ScoringConfig,
)
from stub_impl import AreaStateStub, CircleActivationStub, DisclosureStub, MatcherStub

FAILURES: list[str] = []


def expect(cond: bool, msg: str) -> None:
    mark = "ok " if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        FAILURES.append(msg)


def _backend(matcher=None, disclosure=None) -> Backend:
    return Backend(
        matcher=matcher or MatcherStub(),
        disclosure=disclosure or DisclosureStub(),
        area_state=AreaStateStub(),
        activation=CircleActivationStub(),
    )


# ---------------------------------------------------------------------------
# Planted-violation implementations. Each one is a MatcherStub subclass that breaks exactly
# ONE property, so a fired check points at a specific divergence rather than "something".
# ---------------------------------------------------------------------------

class _SumBonusMatcher(MatcherStub):
    """circle_bonus as place + type instead of MAX(place, type) — the single most likely
    misreading of the SQL (migration 20260914120000:81-85 is a max() over a CASE)."""

    def score_matches(self, mom, population, config):
        rows = super().score_matches(mom, population, config)
        for r in rows:
            if r.same_place and r.same_type:
                r.score += config.same_type_weight
                r.same_type_bonus = config.same_type_weight
        return rows


class _ZeroScoreStrangerMatcher(MatcherStub):
    """Emits a row for every peer, including those with no overlap on either arm. The RPC
    cannot produce these: candidates is a FULL OUTER JOIN of circle_scored and concept_scored,
    so a peer with neither is not a row (20260914120000:143-153)."""

    def score_matches(self, mom, population, config):
        rows = super().score_matches(mom, population, config)
        seen = {c.user_id for c in rows} | {mom.user_id}
        rows.extend(
            RankedCandidate(user_id=p.user_id, score=0.0, same_place=False, same_type=False,
                            shared_affinities=(), ring="R4")
            for p in population if p.user_id not in seen
        )
        return rows


class _IneligibleRowsMatcher(MatcherStub):
    """Forgets the candidate-set filter: scores suggested / dismissed / ungrounded rows too
    (the SQL requires status='confirmed' AND dismissed_at IS NULL AND place_ref IS NOT NULL on
    BOTH sides — 20260914120000:61-77)."""

    def score_matches(self, mom, population, config):
        promoted = copy.deepcopy(population)
        for p in promoted:
            for c in p.circles:
                c.status = "confirmed"
                c.dismissed_at = None
                c.place_ref = c.place_ref or f"{c.id}_forced_place"
        return super().score_matches(mom, promoted, config)


class _NoClampMatcher(MatcherStub):
    """Honours the RAW p_limit instead of clamping to [1,50]
    (`limit greatest(1, least(coalesce(p_limit,20), 50))`, 20260914120000:180). Lower bound:
    p_limit=0 returns nothing. Upper bound: an over-50 request over-returns — simulated by
    repeating rows, since an honest stub can never over-return."""

    def score_matches(self, mom, population, config):
        rows = super().score_matches(mom, population, replace(config, n_results=50))
        raw = int(config.n_results)
        if raw <= 0:
            return []
        return (rows * 3)[:raw] if raw > 50 else rows[:raw]


class _NoTypeFloorMatcher(MatcherStub):
    """Drops the type-only floor row, so a lone pioneer whose only overlap is a shared
    circle_type gets nothing — the §C.4 day-zero failure mode."""

    def score_matches(self, mom, population, config):
        return [c for c in super().score_matches(mom, population, config) if c.same_place]


class _IgnoreBlocksMatcher(MatcherStub):
    """Never excludes a blocked pair (the SQL filters `not lana_is_blocked(...)` on both
    arms, 20260914120000:76 and :113)."""

    def _is_blocked(self, a: str, b: str) -> bool:
        return False


class _LeakyDisclosure(DisclosureStub):
    """§F.3 violation: hands the place identity to a stranger-tier requester."""

    def filter_by_tier(self, circle, requester_tier):
        return DisclosedCircleInfo(
            circle_type=circle.circle_type,
            place_name=circle.place_name,
            place_ref=circle.place_ref,
            can_vouch=False,
        )


def _volume_sensitive_eligible_for(population: list[Mom]):
    """Planted §E.5 violation: founding can be BOUGHT — 6+ invitees makes you eligible
    regardless of your own verification or engagement."""
    counts = {m.user_id: 0 for m in population}
    for m in population:
        if m.invited_by in counts:
            counts[m.invited_by] += 1

    def _fn(mom, zip_states):
        return sweep.is_founding_eligible(mom, zip_states) or counts.get(mom.user_id, 0) >= 6

    return _fn


def _live_seed_module():
    """Import live_seed WITHOUT importing live_impl. live_impl does load_dotenv(.env.local)
    at import time; the selftest is offline-only and has no business pulling credentials into
    the process, so a stub module is registered first. Only the pure slug helper is exercised."""
    if "live_impl" not in sys.modules:
        stub = types.ModuleType("live_impl")
        stub._ALLOW_WRITES = False
        stub._URL = ""
        stub._KEY = ""
        stub._headers = lambda *a, **k: {}
        stub._require_creds = lambda: None
        stub._select = lambda *a, **k: []
        sys.modules["live_impl"] = stub
    import live_seed
    return live_seed


# ---------------------------------------------------------------------------

def main() -> int:
    print("[selftest] circles_zip mechanical checks must fire on planted violations\n")
    scoring = ScoringConfig()

    # --- onion scoring parity (the stub<->RPC regression gate) ----------------------------
    honest = sweep.measure_onion_scoring_parity(scoring)
    expect(honest["passed"], "onion parity PASSES the honest stub (no false positive)")

    broken = sweep.measure_onion_scoring_parity(scoring, backend=_backend(_SumBonusMatcher()))
    expect(not broken["passed"] and not broken["checks"]["max_not_sum"],
           "onion parity FAILS when place+type is SUMmed instead of MAXed")
    expect(not broken["checks"]["components_are_max_split"],
           "onion parity FAILS when the component columns stop being a split of the MAX")

    broken = sweep.measure_onion_scoring_parity(scoring, backend=_backend(_ZeroScoreStrangerMatcher()))
    expect(not broken["passed"] and not broken["checks"]["min_score_filters_zeros"],
           "onion parity FAILS when zero-score strangers are emitted (p_min_score not applied)")

    broken = sweep.measure_onion_scoring_parity(scoring, backend=_backend(_IneligibleRowsMatcher()))
    expect(not broken["passed"] and not broken["checks"]["ineligible_rows_never_score"],
           "onion parity FAILS when suggested/dismissed/ungrounded rows score")

    broken = sweep.measure_onion_scoring_parity(scoring, backend=_backend(_NoClampMatcher()))
    expect(not broken["checks"]["clamp_lower_bound_1"],
           "onion parity FAILS when p_limit=0 is not clamped up to 1")
    expect(not broken["checks"]["clamp_upper_bound_50"],
           "onion parity FAILS when an over-50 p_limit is not clamped down to 50")

    # --- the candidate SET (not merely the score filter) ---------------------------------
    # The RPC never emits a peer with no overlap on either arm, even at p_min_score=0.
    focus = Mom(user_id="f", home_zip="Z0",
                circles=[CircleAffiliation(id="f0", user_id="f", circle_type="fitness",
                                           circle_key="fitness_0", place_ref="P", place_name="P",
                                           status="confirmed", detail=None,
                                           source="chat_extraction", invited_by=None)],
                affinities=frozenset({"a"}))
    stranger = Mom(user_id="s", home_zip="Z9", circles=[], affinities=frozenset({"z"}))
    zero_cfg = replace(scoring, p_min_score=0)
    honest_rows = MatcherStub().score_matches(focus, [focus, stranger], zero_cfg)
    expect(all(c.user_id != "s" for c in honest_rows),
           "stub emits NO zero-score stranger even at p_min_score=0 (FULL OUTER JOIN parity)")
    leaky_rows = _ZeroScoreStrangerMatcher().score_matches(focus, [focus, stranger], zero_cfg)
    expect(any(c.user_id == "s" for c in leaky_rows),
           "...and that assertion is non-vacuous: the planted matcher DOES emit the stranger")

    # --- day-zero floor (§C.4) -----------------------------------------------------------
    expect(sweep.check_day_zero_floor(scoring)["passed"],
           "day-zero floor PASSES the honest stub (no false positive)")
    expect(not sweep.check_day_zero_floor(scoring, backend=_backend(_NoTypeFloorMatcher()))["passed"],
           "day-zero floor FAILS when the type-only floor row is dropped")
    expect(not sweep.check_day_zero_floor(replace(scoring, p_min_score=2))["passed"],
           "day-zero floor FAILS when p_min_score is raised above the +1 type-only floor")

    # --- blocked-pair exclusion ----------------------------------------------------------
    expect(sweep.check_blocked_pair_exclusion(scoring)["passed"],
           "blocked-pair exclusion PASSES the honest stub (no false positive)")
    blocked_broken = sweep.check_blocked_pair_exclusion(
        scoring,
        backend_factory=lambda zip_adjacency=None, blocked_pairs=None: _backend(
            _IgnoreBlocksMatcher(zip_adjacency=zip_adjacency, blocked_pairs=blocked_pairs)),
    )
    expect(not blocked_broken["passed"] and not blocked_broken["checks"]["blocked_peer_excluded"],
           "blocked-pair exclusion FAILS when the matcher ignores blocks")
    expect(blocked_broken["checks"]["control_pair_would_have_matched"],
           "...and its negative control still holds (the blocked pair really would have matched)")

    # --- founding invariance under invite volume (§E.5/§I) --------------------------------
    expect(sweep.check_founding_volume_invariance()["passed"],
           "founding invariance PASSES the product-mirroring predicate (no false positive)")
    fi = sweep.check_founding_volume_invariance(eligible_fn_for=_volume_sensitive_eligible_for)
    expect(not fi["passed"], "founding invariance FAILS when eligibility is bought with invites")
    expect(fi["flipped_eligible_by_invite_volume"],
           "...and the one-sided counterfactual arm fires (a mom flips eligible on volume alone)")

    # --- founding_earned disjunct (the product's `founding_earned or …`) ------------------
    _, zip_states = sweep._founding_invariance_fixture()
    earned = Mom(user_id="e", home_zip="Z_OPEN", circles=[],
                 founding_earned_at=PopulationConfig().now)
    plain = Mom(user_id="p", home_zip="Z_OPEN", circles=[])
    expect(sweep.is_founding_eligible(earned, zip_states) is True,
           "an already-earned founding member stays eligible after her area opens")
    expect(sweep.is_founding_eligible(plain, zip_states) is False,
           "...while an unstamped peer in the same open area does not")

    # --- disclosure (§F.3) ---------------------------------------------------------------
    pop = generate_population(PopulationConfig(moms_per_zip=5, n_zips=2)).moms
    expect(sweep.check_disclosure_correctness(pop, _backend())["passed"],
           "disclosure PASSES the honest stub (no false positive)")
    leak = sweep.check_disclosure_correctness(pop, _backend(disclosure=_LeakyDisclosure()))
    expect(not leak["passed"] and leak["leaks_at_stranger_tier"] > 0,
           "disclosure FAILS when place_ref/place_name leaks at stranger tier")
    expect(leak["circles_checked"] > 0,
           "...and the disclosure check is non-vacuous (it actually inspected grounded circles)")

    # --- population validity (an impossible row must never be generated) ------------------
    expect(validate_population(pop) is None,
           "validate_population accepts a real generated population (no false positive)")
    bad = copy.deepcopy(pop[0])
    bad.circles = [CircleAffiliation(id="bad0", user_id=bad.user_id, circle_type="fitness",
                                     circle_key="fitness_0", place_ref=None, place_name=None,
                                     status="confirmed", detail=None,
                                     source="chat_extraction", invited_by=None)]
    try:
        validate_population([bad])
        fired = False
    except ValueError as exc:
        fired = "confirmed_has_place" in str(exc)
    expect(fired, "validate_population RAISES on status='confirmed' with place_ref IS NULL")

    dup = copy.deepcopy(pop[0])
    dup.circles = [
        CircleAffiliation(id=f"dup{i}", user_id=dup.user_id, circle_type="fitness",
                          circle_key="fitness_0", place_ref="P", place_name="P",
                          status="confirmed", detail=None, source="chat_extraction",
                          invited_by=None)
        for i in range(2)
    ]
    try:
        validate_population([dup])
        fired = False
    except ValueError as exc:
        fired = "duplicate live circle_key" in str(exc)
    expect(fired, "validate_population RAISES on a duplicate live (user_id, circle_key)")

    expect(all(len(t) <= 63 and t.replace("_", "a").isalnum() for t in ALL_CIRCLE_TYPES),
           "every circle_type is a legal circle_key stem (the generator builds keys from them)")

    # --- session recency boundary (U2(b) is STRICTLY inside the window) -------------------
    now = PopulationConfig().now
    edge = Mom(user_id="edge", home_zip="Z0", last_session_at=now - timedelta(days=30))
    inside = Mom(user_id="in", home_zip="Z0", last_session_at=now - timedelta(days=29, hours=23))
    stale = Mom(user_id="old", home_zip="Z0", last_session_at=now - timedelta(days=30, hours=12))
    expect(edge.has_recent_session(now, 30) is False,
           "a session at EXACTLY 30 days does NOT count (SQL is created_at > now() - 30d)")
    expect(inside.has_recent_session(now, 30) is True,
           "a session 29d23h old still counts (the check is not over-strict)")
    expect(stale.has_recent_session(now, 30) is False,
           "a 30d12h session does not sneak in through timedelta.days truncation")

    # --- live_seed circle_key slug (Postgres CHECK, not a style preference) --------------
    ls = _live_seed_module()
    uuid_label = "3f9a1c22-77bd-4f0e-9c31-0aa2b7e5d901"
    expect(bool(ls._CIRCLE_KEY_RE.match(ls._circle_key_for("P1"))),
           "live_seed builds a legal circle_key from a persona label")
    expect(bool(ls._CIRCLE_KEY_RE.match(ls._circle_key_for(uuid_label))),
           "live_seed builds a legal circle_key from a RAW UUID (hyphens are illegal in the key)")
    expect(len({ls._circle_key_for(uuid_label), ls._circle_key_for("P1")}) == 2,
           "...and distinct labels still produce distinct keys (per-user uniqueness)")

    # --- the gate wiring itself must not drift -------------------------------------------
    result = sweep.run_one_config(PopulationConfig(moms_per_zip=4, n_zips=2), scoring,
                                  ActivationConfig(), AreaStateConfig())
    missing = [g for g in sweep.GATE_CHECKS if not isinstance(result.get(g, {}).get("passed"), bool)]
    expect(not missing,
           f"every name in sweep.GATE_CHECKS is a real measurement with a bool 'passed' "
           f"(missing: {missing})")

    print()
    if FAILURES:
        print(f"[selftest] {len(FAILURES)} DETECTION(S) DID NOT FIRE — the harness is not trustworthy:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("[selftest] all mechanical detections fire correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
