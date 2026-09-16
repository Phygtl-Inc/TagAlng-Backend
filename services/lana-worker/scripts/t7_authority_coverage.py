#!/usr/bin/env python3
"""T7 · Coverage — is domain authority a now-feature or a later one?

SPEC_RECOMMENDER_AUTHORITY §6/T7: "What share of recommendations have any attester at
authority >= 0.35? Half a day, tells us if this is a now-feature or one that waits."

The spec sequences this before anyone builds, and §8 then says to build A1 anyway because
"it is cheap and it is the measurement instrument" — A1 IS what T7 measures with, so the
function ships first and this script is the gate that decides whether the ranking work
(P1/P2/P3, T1-T6) starts now or waits for the rapport work to fill the claim graph.

Read-only. Touches nothing, writes nothing.

    python -m scripts.t7_authority_coverage              # summary
    python -m scripts.t7_authority_coverage --verbose    # per-tip lines

Requires the worker's env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GCP_VERTEX_PROJECT.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from app.auth import service_client
from app.authority import MIN_EXPLICIT_SCORE, authority_for, concepts_for_ask


def _self_check() -> None:
    """One assertion, run before the number is reported: an attester with NO claims for a
    concept must score 0.0.

    This is not a formality. `least(0.60, NULL)` returns 0.60 in Postgres — LEAST ignores
    null arguments — so the spec's own expression handed the full claim cap to anyone who
    had never made a claim, ranking them above someone who actually made one. If this
    assertion ever fires again, every coverage number below is meaningless and the
    ordering is inverted in production.
    """
    sb = service_client()
    concepts = sb.table("identity_concepts").select("id").limit(1).execute().data or []
    users = sb.table("users").select("id").limit(1).execute().data or []
    if not concepts or not users:
        print("self-check skipped: no concepts or no users on this database")
        return

    # A user id that cannot own claims for this concept: the concept is real, the claims
    # are not. Scoring it must produce 0.0, never the 0.60 cap.
    fake_user = "00000000-0000-0000-0000-000000000000"
    scored = authority_for(fake_user, [str(concepts[0]["id"])])
    got = scored.get(str(concepts[0]["id"]), {}).get("score", 0.0)
    assert got == 0.0, (
        f"REGRESSION: an attester with no claims scored {got}, not 0.0. "
        "least(0.60, NULL) is 0.60 in Postgres — check the coalesce guards in "
        "20261209120000_attester_authority.sql before trusting any number below."
    )
    print("self-check ok: no claims -> 0.0\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="one line per tip")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    _self_check()

    sb = service_client()
    tips = (
        sb.table("local_signals")
        .select("id, user_id, detail_text, created_at")
        .eq("intent", "tip_share")
        .order("created_at", desc=True)
        .limit(args.limit)
        .execute()
        .data
        or []
    )
    if not tips:
        print("no tip_share rows on this database — T7 is unmeasurable here")
        return 1

    covered = 0
    unresolved = 0
    evidence = Counter()

    for t in tips:
        # p_as_of is the tip's own moment, not now(): authority is evaluated as of when
        # the recommendation was made, or a later claim would inflate an older tip.
        concept_ids = concepts_for_ask(t.get("detail_text") or "")
        if not concept_ids:
            unresolved += 1
            continue

        scored = authority_for(
            str(t["user_id"]), concept_ids, as_of=str(t.get("created_at") or "")
        )
        best = max((r["score"] for r in scored.values()), default=0.0)
        if best >= MIN_EXPLICIT_SCORE:
            covered += 1
            for row in scored.values():
                evidence.update(row["evidence"])
        if args.verbose:
            print(f"  {best:.2f}  {(t.get('detail_text') or '')[:60]}")

    n = len(tips)
    pct = 100.0 * covered / n
    print(f"\nT7 coverage · {covered}/{n} tips ({pct:.1f}%) have an attester >= {MIN_EXPLICIT_SCORE}")
    print(f"  ask resolved to no concept: {unresolved}/{n}")
    if evidence:
        print(f"  evidence mix: {dict(evidence)}")

    # §6.3 and §8 both hang off this number. Stated here so the decision is not re-derived
    # from memory three weeks later.
    print()
    if pct < 10:
        print("VERDICT: invisible today. Build stops at A1/A2 — defer P1/P2/P3 and T1-T6")
        print("until the rapport work has filled the claim graph (SPEC_MATURITY_RAPPORT,")
        print("pillar 10). A reason line this rare reads as endorsement, not explanation.")
    elif pct < 30:
        print("VERDICT: real but thin. Ranking is worth building; the reason line (P3)")
        print("should stay off until coverage rises, per §6.3.")
    else:
        print("VERDICT: now-feature. Proceed with P1/P2/P3 and Tim's T1-T6.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
