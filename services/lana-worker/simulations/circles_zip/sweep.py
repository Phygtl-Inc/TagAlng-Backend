"""
sweep.py — parameter-sensitivity harness for the Circles + ZIP-Unlock onion matcher
(§C) and ZIP-unlock state machine (§D).

This is a SENSITIVITY + PARITY tool, not a "correct answer" against real population data.
Phase 1 IS built (branch `circles`, migrations 20260906-08) and REV-2: the onion matcher
(§C) is now BUILT + deployed too (branch `main`, PR #107/#114, migrations 20260913/14).
Neither branch is visible from here, so the harness still runs against the stub — but the
stub now MIRRORS the deployed onion algorithm, so its scoring measurements are PARITY checks
(max-not-sum circle bonus; confirmed+grounded only; order score desc/nickname asc; clamp
[1,50]; concept arm gated by dormancy), not merely spec-sensitivity. Offline re-weighting of
the RPC's returned component columns (place/type/concept) is the intended tuning loop and is
what the weight sweeps drive. It becomes a full regression tool the moment SIM_BACKEND=live
is wired up in backend.py/live_impl.py.

Usage:
    cd services/lana-worker/simulations/circles_zip
    python sweep.py                  # full sweep + report (writes report.md, plots/*.png)
    python sweep.py --quick          # smaller population, faster, for iterating on the harness itself
"""

from __future__ import annotations

import argparse
import copy
import statistics
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from backend import Backend, get_backend
from population import PopulationConfig, generate_population
from ports import (
    ActivationConfig,
    AreaStateConfig,
    CircleAffiliation,
    Mom,
    ScoringConfig,
    ZipEvent,
    ZipState,
)

RING_ORDER = ("R1", "R2", "R3", "R4", "R5")
OUT_DIR = Path(__file__).parent / "out"


# ---------------------------------------------------------------------------
# Measurement 1 · match coverage + which ring served each mom
# ---------------------------------------------------------------------------

def measure_match_coverage(
    population: list[Mom], backend: Backend, scoring_config: ScoringConfig
) -> dict[str, Any]:
    top_ring_counts = {r: 0 for r in RING_ORDER}
    unmatched = 0
    for mom in population:
        candidates = backend.matcher.score_matches(mom, population, scoring_config)
        if not candidates:
            unmatched += 1
            continue
        top_ring_counts[candidates[0].ring] += 1

    n = len(population)
    matched = n - unmatched
    return {
        "n_moms": n,
        "pct_with_match": round(matched / n, 4) if n else None,
        "top_match_ring_distribution": {r: round(c / n, 4) for r, c in top_ring_counts.items()} if n else {},
    }


# ---------------------------------------------------------------------------
# Measurement 2 · day-zero floor (§C.4's acceptance criterion, as a dedicated fixture)
# ---------------------------------------------------------------------------

def check_day_zero_floor(scoring_config: ScoringConfig) -> dict[str, Any]:
    """A lone pioneer: 1 grounded circle, 0 co-members sharing her exact place — but
    someone (anywhere — the built matcher has no zip/adjacency scoping) shares her
    circle_type. Per §C.4 she must still receive >=1 match; under the BUILT algorithm that
    match is a TYPE-ONLY floor row (same_type True, same_place False, score == +1 >=
    p_min_score), NOT an adjacency ring. The far stranger (nothing shared) must score 0 and
    be excluded by the p_min_score filter."""
    now = PopulationConfig().now  # reproducible reference; matcher ignores it anyway
    pioneer = Mom(
        user_id="pioneer",
        home_zip="Z000",
        circles=[
            CircleAffiliation(
                id="pioneer_c0", user_id="pioneer", circle_type="fitness",
                circle_key="pioneer_gym", place_ref="pioneer_gym_place",
                place_name="Pioneer's Gym", status="confirmed", detail=None,
                source="chat_extraction", invited_by=None,
            )
        ],
        phone_verified_at=now,
        last_session_at=None,
        affinities=frozenset(),
    )
    # A neighbor in an adjacent zip, same circle_type, DIFFERENT place — qualifies
    # only via R5 (adjacent + same circle_type), never R1-R4.
    neighbor = Mom(
        user_id="neighbor",
        home_zip="Z001",  # adjacent to Z000 under a ring-1 adjacency
        circles=[
            CircleAffiliation(
                id="neighbor_c0", user_id="neighbor", circle_type="fitness",
                circle_key="other_gym", place_ref="other_gym_place",
                place_name="Other Gym", status="confirmed", detail=None,
                source="chat_extraction", invited_by=None,
            )
        ],
        phone_verified_at=now,
        last_session_at=None,
        affinities=frozenset(),
    )
    # A stranger with nothing in common at all, far away — should NOT be returned.
    stranger = Mom(user_id="stranger", home_zip="Z999", circles=[], phone_verified_at=now, last_session_at=None, affinities=frozenset())

    adjacency = {"Z000": {"Z001"}, "Z001": {"Z000"}}
    backend = get_backend(zip_adjacency=adjacency)
    population = [pioneer, neighbor, stranger]

    candidates = backend.matcher.score_matches(pioneer, population, scoring_config)
    top = candidates[0] if candidates else None
    passed = (
        top is not None
        and top.user_id == "neighbor"
        and top.same_type and not top.same_place
        and top.score >= scoring_config.p_min_score
        and all(c.user_id != "stranger" for c in candidates)  # score-0 stranger excluded
    )
    return {
        "passed": passed,
        "candidates": [(c.user_id, c.ring, c.score) for c in candidates],
        "acceptance": "§C.4: lone pioneer (1 grounded circle, 0 co-members) still receives >=1 "
                      "match — a TYPE-ONLY floor row (same_type, no place); score-0 stranger excluded",
    }


# ---------------------------------------------------------------------------
# Measurement 2b · onion scoring PARITY (rev-2) — asserts the stub reproduces the
# deployed algorithm exactly on a hand-seeded fixture. This is the regression gate for
# stub↔built parity: max-not-sum circle bonus; only confirmed+grounded rows score;
# suggested/dismissed/ungrounded never score; order score desc / nickname asc; clamp [1,50];
# p_min_score filter; concept-arm dormancy toggle.
# ---------------------------------------------------------------------------

def _parity_circle(
    uid: str, idx: int, ctype: str, place_ref: str | None,
    status: str = "confirmed", dismissed: bool = False,
) -> CircleAffiliation:
    now = PopulationConfig().now
    return CircleAffiliation(
        id=f"{uid}_c{idx}", user_id=uid, circle_type=ctype,  # type: ignore[arg-type]
        circle_key=f"{uid}_k{idx}", place_ref=place_ref, place_name=place_ref,
        status=status, detail=None, source="chat_extraction", invited_by=None,  # type: ignore[arg-type]
        dismissed_at=(now if dismissed else None),
    )


def _parity_mom(uid: str, zip_code: str, circles: list[CircleAffiliation], concepts: frozenset[str]) -> Mom:
    now = PopulationConfig().now
    return Mom(user_id=uid, home_zip=zip_code, circles=circles,
               phone_verified_at=now, last_session_at=now, affinities=concepts)


def measure_onion_scoring_parity(scoring_config: ScoringConfig) -> dict[str, Any]:
    backend = get_backend()  # stub; no adjacency (built matcher has none)
    cfg_on = replace(scoring_config, concept_arm_enabled=True, n_results=20, p_min_score=1)
    cfg_off = replace(scoring_config, concept_arm_enabled=False, n_results=20, p_min_score=1)

    focus = _parity_mom("focus", "Z000", [_parity_circle("focus", 0, "fitness", "P_shared")], frozenset({"c1"}))
    peers = [
        # shares BOTH a place and a type — circle_bonus must be MAX(3,1)=3, NOT 4 (sum):
        _parity_mom("both", "Z000",
                    [_parity_circle("both", 0, "school", "P_shared"),
                     _parity_circle("both", 1, "fitness", "P_other")], frozenset()),
        _parity_mom("place_only", "Z000", [_parity_circle("place_only", 0, "school", "P_shared")], frozenset()),
        _parity_mom("type_only", "Z001", [_parity_circle("type_only", 0, "fitness", "P_far")], frozenset()),
        _parity_mom("concept_only", "Z002", [_parity_circle("concept_only", 0, "heritage", "P_iso")], frozenset({"c1"})),
        # these three share focus's place/type ONLY via a non-eligible row → must NOT score:
        _parity_mom("suggested_sharer", "Z000", [_parity_circle("suggested_sharer", 0, "fitness", "P_shared", status="suggested")], frozenset()),
        _parity_mom("dismissed_sharer", "Z000", [_parity_circle("dismissed_sharer", 0, "fitness", "P_shared", dismissed=True)], frozenset()),
        _parity_mom("ungrounded_sharer", "Z000", [_parity_circle("ungrounded_sharer", 0, "fitness", None)], frozenset()),
        _parity_mom("nothing", "Z003", [_parity_circle("nothing", 0, "other", "P_none")], frozenset()),
    ]
    pop = [focus, *peers]

    res_on = backend.matcher.score_matches(focus, pop, cfg_on)
    on = {c.user_id: c for c in res_on}
    res_off = backend.matcher.score_matches(focus, pop, cfg_off)
    off = {c.user_id: c for c in res_off}

    excluded = {"suggested_sharer", "dismissed_sharer", "ungrounded_sharer", "nothing"}

    checks: dict[str, bool] = {}
    # circle_bonus is MAX, not SUM (place+type peer scores 3, never 4):
    checks["max_not_sum"] = "both" in on and on["both"].score == 3.0 and on["both"].same_place and on["both"].same_type
    # components are returned SEPARATELY (0/3 and 0/1) even though the score MAXes them:
    checks["components_returned_raw"] = (
        "both" in on and on["both"].same_place_bonus == 3.0 and on["both"].same_type_bonus == 1.0
    )
    checks["place_only_is_3"] = "place_only" in on and on["place_only"].score == 3.0 and not on["place_only"].same_type
    checks["type_only_is_1"] = "type_only" in on and on["type_only"].score == 1.0 and not on["type_only"].same_place
    checks["concept_arm_adds_1"] = "concept_only" in on and on["concept_only"].score == 1.0 and on["concept_only"].shared_concept_count == 1
    checks["shared_place_ref_reported"] = "both" in on and on["both"].shared_place_ref == "P_shared" and on["type_only"].shared_place_ref is None
    # only confirmed+grounded rows score; suggested/dismissed/ungrounded/nothing excluded:
    checks["ineligible_rows_never_score"] = excluded.isdisjoint(on.keys())
    checks["min_score_filters_zeros"] = "nothing" not in on
    # order: score desc, then nickname (user_id) asc:
    checks["order_score_desc_then_nickname"] = res_on == sorted(res_on, key=lambda c: (-c.score, c.user_id))
    # dormancy: with the concept arm off, a concept-only peer drops out; circle rows unchanged:
    checks["concept_dormancy_toggle"] = (
        "concept_only" not in off and "type_only" in off and off["both"].score == 3.0
    )

    # limit clamp [1,50]:
    clamp_lo = backend.matcher.score_matches(focus, pop, replace(cfg_on, n_results=0))
    checks["clamp_lower_bound_1"] = len(clamp_lo) == 1  # n_results=0 clamps up to 1
    big_focus = _parity_mom("bigfocus", "Z000", [_parity_circle("bigfocus", 0, "fitness", "BP")], frozenset())
    big_pop = [big_focus] + [
        _parity_mom(f"peer_{i:03d}", "Z000", [_parity_circle(f"peer_{i:03d}", 0, "fitness", f"BP_{i}")], frozenset())
        for i in range(60)
    ]
    clamp_hi = backend.matcher.score_matches(big_focus, big_pop, replace(cfg_on, n_results=100))
    checks["clamp_upper_bound_50"] = len(clamp_hi) == 50  # 60 type-sharers, clamped to 50

    checks["all_passed"] = all(checks.values())
    return {
        "checks": checks,
        "passed": checks["all_passed"],
        "top_scores_concept_on": [(c.user_id, c.score) for c in res_on],
        "acceptance": "rev-2 §C: stub reproduces the deployed onion algorithm — MAX(place,type) "
                      "circle bonus (not sum), confirmed+grounded-only candidates, order "
                      "score desc/nickname asc, p_limit clamp [1,50], concept-arm dormancy toggle",
    }


# ---------------------------------------------------------------------------
# Measurement 3 · founding-eligibility vs raw invite volume (§I anti-gaming, §E.5)
# ---------------------------------------------------------------------------

def measure_founding_vs_invite_volume(
    population: list[Mom], zip_states: dict[str, ZipState], area_config: AreaStateConfig
) -> dict[str, Any]:
    invited_counts: dict[str, int] = {m.user_id: 0 for m in population}
    for mom in population:
        if mom.invited_by is not None and mom.invited_by in invited_counts:
            invited_counts[mom.invited_by] += 1

    def is_founding_eligible(mom: Mom) -> bool:
        # RESOLVED (Asjid 2026-07-28): the founding stamp fires for EVERYONE qualifying at
        # the instant the ZIP transitions to open, inside the recount transaction — no
        # first-N ordinal tracking exists or is needed. The qualifying state is "NOT YET
        # OPEN" (closed OR warming) + phone-verified + ≥1 confirmed thing (a confirmed
        # circle OR an accepted intro). Two corrections vs the original guess: "not closed"
        # → "not yet open" (guess #6), and the "or intro" branch now counts (guess #4).
        zs = zip_states.get(mom.home_zip)
        if zs is None or zs.unlock_state == "open":
            return False
        return mom.phone_verified and mom.has_confirmed_thing()

    buckets = {"0_invites": [], "1_5_invites": [], "6_plus_invites": []}
    for mom in population:
        n = invited_counts[mom.user_id]
        bucket = "0_invites" if n == 0 else ("1_5_invites" if n <= 5 else "6_plus_invites")
        buckets[bucket].append(is_founding_eligible(mom))

    return {
        "founding_rate_by_own_invite_count": {
            b: (round(sum(vals) / len(vals), 4) if vals else None) for b, vals in buckets.items()
        },
        "n_per_bucket": {b: len(vals) for b, vals in buckets.items()},
        "acceptance": "§E.5/§I: founding rate should NOT rise with raw invite volume — only with the inviter's own verification/engagement",
    }


# ---------------------------------------------------------------------------
# Measurement 3b · circle activation rate (§B.2/U3) — exercises CircleActivationPort,
# which nothing else in this sweep otherwise calls.
# ---------------------------------------------------------------------------

def measure_circle_activation(
    population: list[Mom], backend: Backend, activation_config: ActivationConfig
) -> dict[str, Any]:
    by_place: dict[str, list[CircleAffiliation]] = {}
    for mom in population:
        for c in mom.circles:
            if c.place_ref:
                by_place.setdefault(c.place_ref, []).append(c)

    active_count = sum(1 for circles in by_place.values() if backend.activation.is_active(circles, activation_config))
    total = len(by_place)
    return {
        "n_grounded_places": total,
        "pct_places_active": round(active_count / total, 4) if total else None,
        "acceptance": "§B.2/U3: a place is 'active' once >= activation_threshold confirmed members share it",
    }


# ---------------------------------------------------------------------------
# Measurement 4 · disclosure correctness (§F.3)
# ---------------------------------------------------------------------------

def check_disclosure_correctness(population: list[Mom], backend: Backend) -> dict[str, Any]:
    total = 0
    leaks = 0
    for mom in population:
        for circle in mom.circles:
            if circle.place_ref is None:
                continue
            total += 1
            disclosed = backend.disclosure.filter_by_tier(circle, "stranger")
            if disclosed.place_ref is not None or disclosed.place_name is not None:
                leaks += 1
    return {
        "circles_checked": total,
        "leaks_at_stranger_tier": leaks,
        "passed": leaks == 0,
        "acceptance": "§F.3: a Stranger-tier requester never receives place_ref/place_name "
                      "(stub reference; enforced structurally by RLS+serializers in prod)",
    }


# ---------------------------------------------------------------------------
# Measurement 5 · unlock-timing distribution under varied arrival rates
# ---------------------------------------------------------------------------

def simulate_unlock_timing(
    backend: Backend,
    area_config: AreaStateConfig,
    arrivals_per_day: int,
    max_days: int = 90,
) -> int | None:
    """A single zip, starting empty, gaining `arrivals_per_day` newly-active
    phone-verified moms (each with >=1 confirmed circle) per simulated day. Returns
    the day index unlock flips warming->open, or None if it never does within
    max_days."""
    zip_code = "SIM"
    day0 = timedelta(days=0)
    state = ZipState(zip_code=zip_code, unlock_state="closed", verified_active_count=0, unlock_threshold=area_config.unlock_threshold)
    as_of_base = area_config and None  # placeholder to keep linters quiet; real value set below
    population: list[Mom] = []

    from population import PopulationConfig as _PC  # local import to avoid unused-at-module-scope
    now = _PC().now  # reuse the harness's fixed reference "now"

    for day in range(max_days):
        for i in range(arrivals_per_day):
            uid = f"arrival_{day}_{i}"
            population.append(
                Mom(
                    user_id=uid,
                    home_zip=zip_code,
                    circles=[
                        CircleAffiliation(
                            id=f"{uid}_c0", user_id=uid, circle_type="neighborhood",
                            circle_key=f"{uid}_circle", place_ref=f"{uid}_place",
                            place_name=None, status="confirmed", detail=None,
                            source="chat_extraction", invited_by=None,
                        )
                    ],
                    phone_verified_at=now,
                    last_session_at=now,  # session "today" (day of arrival)
                    affinities=frozenset(),
                )
            )
        event = ZipEvent(event_type="activity_recount", zip_code=zip_code, population_snapshot=population, as_of=now)
        prev_state = state.unlock_state
        state = backend.area_state.transition_zip(state, event, area_config)
        if prev_state != "open" and state.unlock_state == "open":
            return day
    return None


def measure_unlock_timing_distribution(
    backend: Backend, area_config: AreaStateConfig, arrival_rates: list[int]
) -> dict[str, Any]:
    results = {}
    for rate in arrival_rates:
        day = simulate_unlock_timing(backend, area_config, arrivals_per_day=rate)
        results[f"{rate}_per_day"] = day
    return {
        "days_to_open_by_arrival_rate": results,
        "acceptance": "§D.4: crossing unlock_threshold flips warming->open — faster arrival should monotonically reduce days-to-open",
    }


# ---------------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------------

def _build_zip_states(population: list[Mom], area_config: AreaStateConfig, backend: Backend, now) -> dict[str, ZipState]:
    zips = sorted({m.home_zip for m in population})
    states: dict[str, ZipState] = {}
    for z in zips:
        zs = ZipState(zip_code=z, unlock_state="closed", verified_active_count=0, unlock_threshold=area_config.unlock_threshold)
        event = ZipEvent(event_type="activity_recount", zip_code=z, population_snapshot=population, as_of=now)
        states[z] = backend.area_state.transition_zip(zs, event, area_config)
    return states


def run_one_config(
    pop_config: PopulationConfig,
    scoring_config: ScoringConfig,
    activation_config: ActivationConfig,
    area_config: AreaStateConfig,
) -> dict[str, Any]:
    population, adjacency = generate_population(pop_config)
    backend = get_backend(zip_adjacency=adjacency)

    zip_states = _build_zip_states(population, area_config, backend, pop_config.now)

    return {
        "match_coverage": measure_match_coverage(population, backend, scoring_config),
        "day_zero_floor": check_day_zero_floor(scoring_config),
        "onion_scoring_parity": measure_onion_scoring_parity(scoring_config),
        "founding_vs_invite_volume": measure_founding_vs_invite_volume(population, zip_states, area_config),
        "disclosure_correctness": check_disclosure_correctness(population, backend),
        "circle_activation": measure_circle_activation(population, backend, activation_config),
        "zip_states_summary": {z: s.unlock_state for z, s in zip_states.items()},
    }


def run_sweep(quick: bool = False) -> dict[str, Any]:
    base_pop = PopulationConfig(moms_per_zip=10 if quick else 30, n_zips=3 if quick else 5)
    base_scoring = ScoringConfig()
    base_activation = ActivationConfig()
    base_area = AreaStateConfig()

    sweep_results: dict[str, Any] = {"baseline": run_one_config(base_pop, base_scoring, base_activation, base_area)}

    # unlock_threshold sweep
    for threshold in (5, 10, 20):
        cfg = replace(base_area, unlock_threshold=threshold)
        sweep_results[f"unlock_threshold_{threshold}"] = run_one_config(base_pop, base_scoring, base_activation, cfg)

    # activation_threshold sweep (U3)
    for threshold in (2, 3, 5):
        cfg = replace(base_activation, activation_threshold=threshold)
        sweep_results[f"activation_threshold_{threshold}"] = run_one_config(base_pop, base_scoring, cfg, base_area)

    # scoring weight sweep — exact_place_weight (OFFLINE re-weighting of the RPC's returned
    # same_place_bonus column; the server value is a fixed SQL constant of 3)
    for w in (1.0, 3.0, 6.0):
        cfg = replace(base_scoring, exact_place_weight=w)
        sweep_results[f"exact_place_weight_{w}"] = run_one_config(base_pop, cfg, base_activation, base_area)

    # concept-arm dormancy sweep — off = current prod (20260905 backfill / IDENTITY_CONCEPT_
    # LINK_ENABLED not yet live → circle-arm only); on = post-backfill full algorithm.
    for enabled in (False, True):
        cfg = replace(base_scoring, concept_arm_enabled=enabled)
        sweep_results[f"concept_arm_{'on' if enabled else 'off'}"] = run_one_config(base_pop, cfg, base_activation, base_area)

    # concept OVERLAP density sweep — smaller shared vocab ⇒ more shared concepts per pair
    for vocab in ((6, 20) if quick else (6, 12, 24)):
        cfg = replace(base_pop, affinity_vocab_size=vocab)
        sweep_results[f"concept_vocab_{vocab}"] = run_one_config(cfg, base_scoring, base_activation, base_area)

    # population density sweep — moms_per_zip
    for density in ((5, 10) if quick else (10, 30, 60)):
        cfg = replace(base_pop, moms_per_zip=density)
        sweep_results[f"density_{density}_per_zip"] = run_one_config(cfg, base_scoring, base_activation, base_area)

    # circles-per-mom distribution sweep — shift weight toward 0 circles (sparse) vs 3-4 (rich)
    sparse = replace(base_pop, circles_per_mom_weights=(0.5, 0.3, 0.15, 0.04, 0.01))
    rich = replace(base_pop, circles_per_mom_weights=(0.02, 0.13, 0.30, 0.35, 0.20))
    sweep_results["circles_per_mom_sparse"] = run_one_config(sparse, base_scoring, base_activation, base_area)
    sweep_results["circles_per_mom_rich"] = run_one_config(rich, base_scoring, base_activation, base_area)

    # invite conversion rate sweep
    for rate in (0.1, 0.3, 0.6):
        cfg = replace(base_pop, invite_rate=rate)
        sweep_results[f"invite_rate_{rate}"] = run_one_config(cfg, base_scoring, base_activation, base_area)

    # unlock-timing distribution under varied arrival rates (separate from run_one_config
    # since it's a temporal simulation, not a single-snapshot measurement)
    backend = get_backend()
    sweep_results["unlock_timing"] = measure_unlock_timing_distribution(
        backend, base_area, arrival_rates=[1, 2, 5, 10] if not quick else [2, 10]
    )

    return sweep_results


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def _try_plot(sweep_results: dict[str, Any]) -> list[str]:
    written: list[str] = []
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return written

    OUT_DIR.mkdir(exist_ok=True)

    # Plot 1: pct_with_match across unlock_threshold sweep
    thresholds, pct_match = [], []
    for k, v in sweep_results.items():
        if k.startswith("unlock_threshold_"):
            thresholds.append(int(k.rsplit("_", 1)[1]))
            pct_match.append(v["match_coverage"]["pct_with_match"])
    if thresholds:
        order = sorted(range(len(thresholds)), key=lambda i: thresholds[i])
        fig, ax = plt.subplots()
        ax.plot([thresholds[i] for i in order], [pct_match[i] for i in order], marker="o")
        ax.set_xlabel("unlock_threshold")
        ax.set_ylabel("% moms with >=1 match")
        ax.set_title("Match coverage vs unlock_threshold")
        path = OUT_DIR / "match_coverage_vs_unlock_threshold.png"
        fig.savefig(path)
        plt.close(fig)
        written.append(str(path))

    # Plot 2: days-to-open vs arrival rate
    timing = sweep_results.get("unlock_timing", {}).get("days_to_open_by_arrival_rate", {})
    if timing:
        rates = sorted(timing.keys(), key=lambda k: int(k.split("_")[0]))
        days = [timing[r] for r in rates]
        fig, ax = plt.subplots()
        ax.bar([r.replace("_per_day", "/day") for r in rates], [d if d is not None else -1 for d in days])
        ax.set_ylabel("days to open (unlock_threshold reached)")
        ax.set_title("Unlock timing vs arrival rate")
        path = OUT_DIR / "unlock_timing_vs_arrival_rate.png"
        fig.savefig(path)
        plt.close(fig)
        written.append(str(path))

    # Plot 3: founding rate by own-invite-count bucket (baseline config)
    fbucket = sweep_results["baseline"]["founding_vs_invite_volume"]["founding_rate_by_own_invite_count"]
    fig, ax = plt.subplots()
    ax.bar(list(fbucket.keys()), [v if v is not None else 0 for v in fbucket.values()])
    ax.set_ylabel("founding-eligibility rate")
    ax.set_title("Founding rate by own invite-count bucket (should be ~flat)")
    path = OUT_DIR / "founding_vs_invite_volume.png"
    fig.savefig(path)
    plt.close(fig)
    written.append(str(path))

    return written


def render_report(sweep_results: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Circles + ZIP-Unlock — parameter-sensitivity report")
    lines.append("")
    lines.append("Sensitivity analysis against a synthetic population + StubImpl (spec-literal), NOT a")
    lines.append("correctness verdict — no real population data exists yet to calibrate against.")
    lines.append("")

    lines.append("## Acceptance checks (pass/fail against Master spec §C.4/§F.3 + rev-2 onion parity)")
    dz = sweep_results["baseline"]["day_zero_floor"]
    dc = sweep_results["baseline"]["disclosure_correctness"]
    op = sweep_results["baseline"]["onion_scoring_parity"]
    lines.append(f"- Onion scoring parity (rev-2 §C): {'PASS' if op['passed'] else 'FAIL'} — "
                 f"stub mirrors the deployed algorithm (MAX-not-sum circle bonus, confirmed+grounded "
                 f"only, order score desc/nickname asc, clamp [1,50], concept dormancy toggle)")
    if not op["passed"]:
        failed = [k for k, v in op["checks"].items() if not v and k != "all_passed"]
        lines.append(f"    - failing sub-checks: {failed}")
    lines.append(f"- Day-zero floor (§C.4): {'PASS' if dz['passed'] else 'FAIL'} — {dz['candidates']}")
    lines.append(f"- Disclosure correctness (§F.3): {'PASS' if dc['passed'] else 'FAIL'} — "
                 f"{dc['leaks_at_stranger_tier']}/{dc['circles_checked']} circles leaked place info at stranger tier")
    lines.append("")

    lines.append("## Match coverage across the sweep")
    lines.append("| config | % moms w/ >=1 match | R1 | R2 | R3 | R4 | R5 |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, result in sweep_results.items():
        if name == "unlock_timing":
            continue
        mc = result["match_coverage"]
        dist = mc["top_match_ring_distribution"]
        lines.append(
            f"| {name} | {mc['pct_with_match']:.1%} | "
            + " | ".join(f"{dist.get(r, 0):.1%}" for r in RING_ORDER)
            + " |"
        )
    lines.append("")

    lines.append("## Circle-activation rate vs activation_threshold (U3)")
    lines.append("Doesn't move match coverage (a different metric) — this is what activation_threshold actually affects.")
    lines.append("| config | grounded places | % active |")
    lines.append("|---|---|---|")
    for name in ("baseline", "activation_threshold_2", "activation_threshold_3", "activation_threshold_5"):
        ca = sweep_results[name]["circle_activation"]
        pct = f"{ca['pct_places_active']:.1%}" if ca["pct_places_active"] is not None else "n/a"
        lines.append(f"| {name} | {ca['n_grounded_places']} | {pct} |")
    lines.append("")

    lines.append("## ZIP unlock state vs unlock_threshold (U1)")
    lines.append("Doesn't move match coverage either — this is what unlock_threshold actually affects.")
    lines.append("| config | zip states (closed/warming/open per zip) |")
    lines.append("|---|---|")
    for name in ("baseline", "unlock_threshold_5", "unlock_threshold_10", "unlock_threshold_20"):
        zs = sweep_results[name]["zip_states_summary"]
        lines.append(f"| {name} | {zs} |")
    lines.append("")

    lines.append("## Founding-eligibility rate by own invite-count bucket (baseline config)")
    fbucket = sweep_results["baseline"]["founding_vs_invite_volume"]
    lines.append("| bucket | n | founding-eligible rate |")
    lines.append("|---|---|---|")
    for b in ("0_invites", "1_5_invites", "6_plus_invites"):
        n = fbucket["n_per_bucket"][b]
        rate = fbucket["founding_rate_by_own_invite_count"][b]
        rate_str = f"{rate:.1%}" if rate is not None else "n/a"
        lines.append(f"| {b} | {n} | {rate_str} |")
    lines.append("")
    lines.append(f"_{fbucket['acceptance']}_")
    lines.append("")

    lines.append("## Unlock-timing distribution under varied arrival rates")
    timing = sweep_results["unlock_timing"]["days_to_open_by_arrival_rate"]
    lines.append("| arrivals/day | days to open |")
    lines.append("|---|---|")
    for k, v in timing.items():
        lines.append(f"| {k.replace('_per_day', '')} | {v if v is not None else 'never within window'} |")
    lines.append("")

    plots = _try_plot(sweep_results)
    if plots:
        lines.append("## Plots")
        for p in plots:
            lines.append(f"- `{p}`")
        lines.append("")

    lines.append("## What's RESOLVED (reconciled with Asjid 2026-07-28, rev-2)")
    lines.append("The onion matcher (§C) is now BUILT + wired + DEPLOYED — branch `main`, PR #107/#114,")
    lines.append("migrations 20260913 (`count_shared_concepts_for_user`) + 20260914 (the scoring RPC")
    lines.append("`score_onion_candidates_for_user`), prod worker live (Cloud Run rev 00010). The stub now")
    lines.append("MIRRORS the deployed algorithm and the parity check above is its regression gate. ALL 8")
    lines.append("original guesses are RESOLVED — ZERO remain open: #1 (NO proximity term — users store no")
    lines.append("coordinates; locality lives in the serving layer) and #7 (RPC p_limit 20, clamp [1,50],")
    lines.append("p_min_score 1; blend serve cap 5 / <=3 labels) are the two that flipped in rev-2.")
    lines.append("")
    lines.append("Caveats: the concept arm is DORMANT in current prod (needs IDENTITY_CONCEPT_LINK_ENABLED")
    lines.append("+ the 20260905 backfill, PR #96 pending) — a 0-concept score is flag-off, not a bug. And")
    lines.append("prod DB still lacks migrations 20260913/14, so the blend FAILS OPEN there (peers list")
    lines.append("byte-identical to pre-onion) until that push — dev has everything. Live scoring (RPC/")
    lines.append("wrapper) is not exercised here: no dev DB/service credential is reachable from this branch.")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="smaller population, fewer sweep points")
    args = parser.parse_args()

    results = run_sweep(quick=args.quick)
    report = render_report(results)

    OUT_DIR.mkdir(exist_ok=True)
    report_path = OUT_DIR / "report.md"
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[sweep] report written to {report_path}")


if __name__ == "__main__":
    main()
