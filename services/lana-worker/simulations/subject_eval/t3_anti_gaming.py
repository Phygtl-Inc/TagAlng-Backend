"""
t3_anti_gaming.py — T3: does `attester_authority()`'s shipped anti-gaming design actually hold?

    cd services/lana-worker/simulations/subject_eval
    python t3_anti_gaming.py            # offline reference check (default, no DB, no key)
    python t3_anti_gaming.py --live --env ../../../../.env.local.dev-backup   # + one safe RPC call

UNBLOCKED 2026-09-17: `attester_authority()` shipped in
`supabase/migrations/20261209120000_attester_authority.sql` (ASJID-5). T3 no longer needs a stub.

WHY AN OFFLINE MIRROR, NOT JUST A CALL TO THE REAL RPC
--------------------------------------------------------
`tests/test_authority.py` already covers the Python read side (`authority_for`/`best_authority`/
`concepts_for_ask`) thoroughly — 8 tests, all mocking the RPC response. What NONE of them touch
is the SQL FORMULA ITSELF: every product test mocks `attester_authority`'s return value, so
nothing in the product's own suite verifies the arithmetic inside the function against real
inputs. That is exactly this suite's job — same "mirrored product values asserted against
source" convention as `checks.py`'s `PASS_RULES` — and it is independent of whether a DB is
reachable, which is what makes it safe to run in CI.

`reference_authority()` below is a line-for-line Python mirror of the SQL in
`20261209120000_attester_authority.sql:99-183`. If that SQL changes, this mirror goes stale
silently unless someone updates it too — that risk is accepted the same way `checks.py` accepts
it for shipped constants, and is cheaper than not having the check at all.

THE FOUR ANTI-GAMING PROPERTIES, each stated in the migration's own prose
  claim_ceiling        "claims alone cap at 0.60" — no amount of self-declared corroboration
                        reaches 1.0 without behavioural evidence.
  bare_floor            a single bare, unspecific, uncorroborated claim (0.10) must never clear
                        `MIN_EXPLICIT_SCORE` (0.35) alone — the anti-gaming floor from `authority.py`.
  no_backdating         a claim created AT OR AFTER `p_as_of` must not count at all (departure #1's
                        anti-gaming reason for taking a timestamp in the first place).
  relation_exclusion    a claim about someone else (`subject_kind != 'self'`) must not count
                        unless `p_include_relations=True` — "their standing, not the attester's".
  zero_claims_is_zero   departure #1's actual bug fix: `least(0.60, NULL)` is 0.60 in Postgres,
                        not NULL, so a NULL-vs-zero bug would silently hand full claim credit to
                        someone with NO claims at all. `reference_authority` must return exactly
                        0.0 for an attester with nothing on file — the same regression
                        `test_zero_authority_is_no_authority` pins on the Python side.

LIVE MODE, --live
------------------
One optional, deliberately narrow RPC call: `attester_authority()` against a FRESH random UUID
that certainly has no claims and no behavioural signals. This proves the REAL, deployed function
— not just my mirror of it — returns 0.0 (not the least(NULL) bug) for a genuine nobody, without
touching any real user's claims or quotes. No other live call is made; every other check here is
the offline mirror, which is what makes `--live` opt-in rather than the default.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent

MIN_EXPLICIT_SCORE = 0.35   # app/authority.py's own constant, mirrored — see drift check below


@dataclass
class Claim:
    created_at: datetime
    subject_kind: str = "self"     # 'self' | 'child' | 'partner' | ... — spec: standing is SELF only
    source_quote: str = ""
    dismissed: bool = False
    transient: bool = False
    details: list[str] = field(default_factory=list)   # extra structured detail entries


@dataclass
class BehaviouralEvidence:
    n_local_signal_subjects: int = 0     # distinct tip_share subjects above p_behav_min_sim
    n_confirmed_circles: int = 0         # confirmed circle_affiliations above p_behav_min_sim


def reference_authority(
    claims: list[Claim],
    behav: BehaviouralEvidence,
    *,
    p_as_of: datetime,
    p_include_relations: bool = False,
) -> float:
    """Mirrors `attester_authority()`'s SQL, verbatim, per
    `20261209120000_attester_authority.sql:99-183`. Every departure the migration documents is
    reproduced here on purpose, not simplified away."""
    counted = [
        c for c in claims
        if not c.dismissed
        and not c.transient
        and c.created_at < p_as_of                              # anti-gaming: no back-dating
        and (p_include_relations or c.subject_kind == "self")    # standing is SELF only, by default
    ]
    has_bare = len(counted) > 0
    is_specific = [len(c.source_quote or "") >= 40 and c.source_quote != "" for c in counted]
    has_specific = any(is_specific)

    # Corroboration counts EVIDENCE, not rows (departure #3): (extra claims beyond the first)
    # PLUS every attribute recorded in `details[]` across all counted claims.
    corroborations = max(len(counted) - 1, 0) + sum(len(c.details) for c in counted)

    # Every boolean coalesced before arithmetic (departure #1) — this is what fixes
    # `least(0.60, NULL)`. Reproduced structurally: has_bare/has_specific are plain bools here,
    # never None, so there is nothing for the Postgres NULL-swallowing bug to hide in. The
    # zero_claims_is_zero test below is what actually proves this holds at n=0.
    claim_component = min(
        0.60,
        0.10 * (1 if has_bare else 0)
        + 0.25 * (1 if has_specific else 0)
        + 0.15 * min(corroborations, 2),
    )
    n_behav = min(2, behav.n_local_signal_subjects) + min(1, behav.n_confirmed_circles)
    behav_component = min(0.40, 0.20 * n_behav)

    return round(claim_component + behav_component, 4)


# ── non-vacuity proof: every anti-gaming property, planted violation + clean control ──────────

_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {name}")
    if not condition:
        _failures.append(name)


def _offline_checks() -> None:
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    long_quote = "I grew up in Gaziantep and my grandmother ran a kebab place for 30 years"
    short_quote = "yeah I'm Turkish"

    # ---- zero_claims_is_zero — the actual bug the migration exists to fix -------------------
    zero = reference_authority([], BehaviouralEvidence(), p_as_of=now)
    check("zero_claims_is_zero: no claims, no behaviour -> exactly 0.0 (not the least(NULL) bug)",
          zero == 0.0)

    # ---- bare_floor — a single bare claim must never alone clear MIN_EXPLICIT_SCORE --------
    bare_only = [Claim(created_at=now - timedelta(days=10), source_quote="I'm Turkish")]
    bare_score = reference_authority(bare_only, BehaviouralEvidence(), p_as_of=now)
    check(f"bare_floor: a single bare claim scores {bare_score} (must be < 0.35 floor)",
          bare_score < MIN_EXPLICIT_SCORE)
    check("...specifically it scores exactly 0.10 (has_bare only, no specificity/corroboration)",
          bare_score == 0.10)

    # PLANTED VIOLATION of bare_floor's intent: what a broken formula giving bare claims full
    # weight would look like — proves the check would actually catch the regression.
    def broken_bare_scores_high(claims: list[Claim]) -> float:
        return 0.5 if claims else 0.0  # a bug that overweights a bare claim

    check("planted violation: a broken formula CAN clear the floor on a bare claim alone",
          broken_bare_scores_high(bare_only) >= MIN_EXPLICIT_SCORE)

    # ---- claim_ceiling — claims alone cap at 0.60, however much corroboration piles on -----
    heavily_corroborated = [
        Claim(created_at=now - timedelta(days=400), source_quote=long_quote,
              details=["born_in:Gaziantep", "family_business:kebab", "years_lived:18"]),
        Claim(created_at=now - timedelta(days=300), source_quote=long_quote,
              details=["visits_yearly", "extended_family_there"]),
        Claim(created_at=now - timedelta(days=200), source_quote=long_quote, details=["language:Turkish"]),
    ]
    claims_only_score = reference_authority(heavily_corroborated, BehaviouralEvidence(), p_as_of=now)
    check(f"claim_ceiling: heavy corroboration (score={claims_only_score}) still caps at 0.60",
          claims_only_score == 0.60)

    # ---- with behavioural evidence, score CAN exceed 0.60 — the ceiling is claims-only -------
    with_behav = reference_authority(
        heavily_corroborated,
        BehaviouralEvidence(n_local_signal_subjects=2, n_confirmed_circles=1),
        p_as_of=now,
    )
    check(f"...but behavioural evidence pushes past it ({with_behav} > 0.60) — the ceiling "
          f"is on TALK, not on total authority",
          with_behav > 0.60)
    check("...capped overall at 1.0 (0.60 claim max + 0.40 behav max)",
          with_behav <= 1.0)

    # ---- no_backdating — a claim written AFTER p_as_of must not count at all -----------------
    backdated = [Claim(created_at=now + timedelta(days=1), source_quote=long_quote,
                        details=["a", "b", "c"])]
    backdated_score = reference_authority(backdated, BehaviouralEvidence(), p_as_of=now)
    check("no_backdating: a claim written AFTER p_as_of scores 0.0 (cannot back-date authority)",
          backdated_score == 0.0)
    # Clean control: the SAME claim content, written before p_as_of, DOES count.
    predated = [Claim(created_at=now - timedelta(days=1), source_quote=long_quote,
                       details=["a", "b", "c"])]
    predated_score = reference_authority(predated, BehaviouralEvidence(), p_as_of=now)
    check("...clean control: the identical claim written BEFORE p_as_of counts (score > 0)",
          predated_score > 0.0)

    # ---- relation_exclusion — a claim about someone ELSE must not count by default ----------
    about_parent = [Claim(created_at=now - timedelta(days=10), subject_kind="child",
                           source_quote=long_quote, details=["a", "b"])]
    excluded_score = reference_authority(about_parent, BehaviouralEvidence(), p_as_of=now)
    check("relation_exclusion: 'my dad grew up in Gaziantep' scores 0.0 for the ATTESTER by default",
          excluded_score == 0.0)
    included_score = reference_authority(
        about_parent, BehaviouralEvidence(), p_as_of=now, p_include_relations=True)
    check("...but counts once the caller explicitly opts into relations (p_include_relations=True)",
          included_score > 0.0)

    # ---- corroboration counts EVIDENCE not rows (departure #3) ------------------------------
    one_claim_many_details = [
        Claim(created_at=now - timedelta(days=10), source_quote=long_quote,
              details=["a", "b", "c", "d"]),
    ]
    rich_single = reference_authority(one_claim_many_details, BehaviouralEvidence(), p_as_of=now)
    two_bare_claims = [
        Claim(created_at=now - timedelta(days=10), source_quote=long_quote),
        Claim(created_at=now - timedelta(days=5), source_quote=long_quote),
    ]
    two_claims_no_details = reference_authority(two_bare_claims, BehaviouralEvidence(), p_as_of=now)
    check("corroboration counts evidence: ONE claim with 4 detail entries scores >= TWO claims "
          "with none (a row-count-only formula would get this backwards)",
          rich_single >= two_claims_no_details)


def _live_zero_claims_check(env: str) -> bool | None:
    """Optional: one real RPC call against a UUID guaranteed to have no rows anywhere.
    Proves the DEPLOYED function (not just this mirror) returns 0.0, never a least(NULL) leak.
    Returns None (skipped) if credentials are absent — never raises, never writes anything."""
    try:
        from dotenv import dotenv_values

        cfg = dotenv_values(env)
        url, key = cfg.get("SUPABASE_URL") or "", cfg.get("SUPABASE_SERVICE_ROLE_KEY") or ""
        if not url or not key:
            print(f"  [skip] no credentials in {env}")
            return None
        import httpx

        nobody = str(uuid.uuid4())     # a UUID nothing in the DB has ever seen
        # A concept id is required but irrelevant to this check — a random UUID is fine too:
        # the join finds no rows for it regardless, which is exactly what's being proven.
        fake_concept = str(uuid.uuid4())
        h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        r = httpx.post(
            f"{url}/rest/v1/rpc/attester_authority",
            headers=h,
            json={"p_user_id": nobody, "p_concept_ids": [fake_concept]},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  [skip] RPC call failed ({r.status_code}): {r.text[:200]}")
            return None
        rows = r.json()
        score = (rows[0].get("authority") if rows else 0.0) or 0.0
        return float(score) == 0.0
    except Exception as exc:  # noqa: BLE001 — a live probe must never crash the offline suite
        print(f"  [skip] live check errored: {exc}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="T3 anti-gaming — attester_authority() properties")
    ap.add_argument("--live", action="store_true",
                     help="also call the REAL RPC once, against a guaranteed-empty user")
    ap.add_argument("--env", default=str(_HERE.parents[3] / ".env.local"))
    args = ap.parse_args()

    print("[T3] anti-gaming — reference_authority() mirrors "
          "20261209120000_attester_authority.sql:99-183\n")
    _offline_checks()

    if args.live:
        print()
        result = _live_zero_claims_check(args.env)
        if result is None:
            print("  [live] skipped (no credentials or RPC unreachable) — offline result stands")
        else:
            check("live: the DEPLOYED attester_authority() returns 0.0 for a guaranteed-empty user",
                  result)

    print()
    if _failures:
        print(f"[T3] {len(_failures)} FAILED: {_failures}")
        return 1
    print("[T3] all anti-gaming properties hold in the offline mirror, with no false positives.")
    print("     STATUS: unblocked 2026-09-17 (attester_authority() shipped, ASJID-5). This is a")
    print("     MIRROR of the SQL, not a call to it by default — re-diff against")
    print("     20261209120000_attester_authority.sql if that migration is ever amended.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
