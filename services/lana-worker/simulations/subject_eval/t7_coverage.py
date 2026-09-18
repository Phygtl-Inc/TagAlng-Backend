"""
t7_coverage.py — T7, the authority coverage probe (recommender-authority spec §6).

    cd services/lana-worker/simulations/subject_eval
    python t7_coverage.py                     # against the DB in .env.local
    python t7_coverage.py --env ../../../../.env.local.dev-backup

THE QUESTION: what share of today's recommendations have at least one attester who
demonstrably knows the subject area (authority >= 0.35)? It is a go/no-go — if almost nobody
clears the bar, the ranking work waits for the claim graph to fill.

READ-ONLY. Selects only. No writes, no RPC that mutates, no env file modified.

WHAT THIS CAN AND CANNOT MEASURE TODAY
--------------------------------------
`attester_authority()` does not exist yet, and its behavioural half queries `public.attestation`
and `public.subject` — neither table is shipped. So this computes the CLAIM half only:

    claim_component = min(0.60, 0.10*has_bare + 0.25*has_specific + 0.15*min(corroborations,2))

0.35 is reachable from claims alone (bare 0.10 + specific 0.25), and behavioural evidence only
ever ADDS, so the claim-only figure is a genuine **lower bound** on the real coverage.

It also reports a **ceiling**, and the gap between the two is the point:

    ceiling   the author has >=1 specific claim about ANYTHING. Domain-matched authority can
              never exceed this, because a domain match is a subset of having a claim at all.
    floor     ...and that claim's concept matches the recommendation's subject area.

The floor needs an embedding call per recommendation to resolve the subject to concepts
(`match_concepts_by_embedding`). The ceiling needs no embeddings at all, so it runs anywhere and
is reported first — if the ceiling is already near zero, the answer is decided and nobody needs
to spend the embeddings.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import httpx
from dotenv import dotenv_values

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from run_eval import wilson_ci  # noqa: E402

# Mirrors the authority spec §3.2. A claim counts as `specific` when its source_quote carries
# detail beyond the label — the spec's test is length >= 40 and quote != label.
_SPECIFIC_MIN_CHARS = 40
_BAR = 0.35


def _page(client: httpx.Client, url: str, table: str, select: str, headers: dict,
          extra: str = "", page: int = 1000) -> list[dict]:
    """Read every row, paged. PostgREST caps a response, and a silently truncated read would
    understate every rate in this file."""
    out: list[dict] = []
    while True:
        h = dict(headers)
        h["Range"] = f"{len(out)}-{len(out) + page - 1}"
        r = client.get(f"{url}/rest/v1/{table}?select={select}{extra}", headers=h, timeout=60)
        r.raise_for_status()
        rows = r.json()
        out.extend(rows)
        if len(rows) < page:
            return out


def claim_component(has_bare: bool, has_specific: bool, corroborations: int) -> float:
    return min(0.60, 0.10 * has_bare + 0.25 * has_specific + 0.15 * min(corroborations, 2))


def main() -> int:
    ap = argparse.ArgumentParser(description="T7 authority coverage (read-only)")
    ap.add_argument("--env", default=str(_HERE.parents[3] / ".env.local"))
    ap.add_argument("--out", default=str(_HERE / "out" / "t7_coverage.md"))
    args = ap.parse_args()

    cfg = dotenv_values(args.env)
    url = cfg.get("SUPABASE_URL") or ""
    key = cfg.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        print(f"no SUPABASE_URL / SERVICE_ROLE_KEY in {args.env}")
        return 2
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    print(f"[T7] {url}  (read-only)")

    with httpx.Client() as c:
        recos = _page(c, url, "local_signals", "id,user_id,reco_type,reco_subject,detail_text",
                      headers, extra="&intent=eq.tip_share")
        claims = _page(c, url, "user_identity_claims",
                       "id,user_id,label,source_quote,created_at", headers,
                       extra="&dismissed_at=is.null&transient=is.false")

    if not recos:
        print("no tip_share recommendations found — nothing to measure")
        return 2

    # Claim side, per author.
    by_user: dict[str, list[dict]] = defaultdict(list)
    for cl in claims:
        by_user[cl["user_id"]].append(cl)

    def author_score(uid: str) -> float:
        rows = by_user.get(uid, [])
        if not rows:
            return 0.0
        specific = any(
            len((r.get("source_quote") or "").strip()) >= _SPECIFIC_MIN_CHARS
            and (r.get("source_quote") or "").strip() != (r.get("label") or "").strip()
            for r in rows
        )
        return claim_component(True, specific, max(0, len(rows) - 1))

    authors = {r["user_id"] for r in recos}
    per_author = {u: author_score(u) for u in authors}
    recos_clearing = [r for r in recos if per_author.get(r["user_id"], 0.0) >= _BAR]

    n_r, k_r = len(recos), len(recos_clearing)
    n_a = len(authors)
    k_a = sum(1 for v in per_author.values() if v >= _BAR)
    lo_r, hi_r = wilson_ci(k_r, n_r)
    lo_a, hi_a = wilson_ci(k_a, n_a)

    typed = sum(1 for r in recos if (r.get("reco_type") or "").strip())
    no_claims = sum(1 for u in authors if not by_user.get(u))

    L = ["# T7 — authority coverage (ceiling)", "",
         f"- source: `{url}` · **read-only** · claim component only",
         "- `attester_authority()` is unshipped and its behavioural half needs `attestation` "
         "and `subject`, which do not exist. Behavioural evidence only ADDS, so this is a "
         "**lower bound** on real authority — and a **ceiling** on domain-matched authority, "
         "since a domain match is a subset of having a specific claim at all.", "",
         f"**{k_r} of {n_r} recommendations ({k_r / n_r:.0%}, 95% CI {lo_r:.0%}–{hi_r:.0%}) "
         f"have an author who could clear {_BAR} on some concept.**", "",
         "| | n | share |", "|---|---|---|",
         f"| recommendations (`intent=tip_share`) | {n_r} | |",
         f"| …with a `reco_type` set | {typed} | {typed / n_r:.0%} |",
         f"| …whose author clears the bar on *some* concept | {k_r} | **{k_r / n_r:.0%}** |",
         f"| distinct authors | {n_a} | |",
         f"| …clearing the bar | {k_a} | {k_a / n_a:.0%} (CI {lo_a:.0%}–{hi_a:.0%}) |",
         f"| …with no identity claims at all | {no_claims} | {no_claims / n_a:.0%} |", ""]

    buckets = {"0.00": 0, "0.10": 0, "0.35": 0, "0.50": 0, "0.60": 0}
    for v in per_author.values():
        key_ = f"{v:.2f}"
        buckets[key_] = buckets.get(key_, 0) + 1
    L += ["## Author claim-score distribution", "", "| score | authors | meaning |", "|---|---|---|"]
    meaning = {"0.00": "no claims", "0.10": "bare only", "0.35": "one specific claim",
               "0.50": "specific + 1 corroboration", "0.60": "specific + 2 (capped)"}
    for k_, v in sorted(buckets.items()):
        if v:
            L.append(f"| {k_} | {v} | {meaning.get(k_, '')} |")
    L += ["", "## What this does not yet measure", "",
          "The **domain match** — whether that specific claim is about the recommendation's "
          "subject area — needs one embedding per recommendation plus "
          "`match_concepts_by_embedding`. The real T7 is at or below the number above. If the "
          "ceiling is already low, the answer is decided without spending the embeddings.", ""]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")

    print(f"\n  recommendations          {n_r}")
    print(f"  distinct authors         {n_a}  ({no_claims} with no claims at all)")
    print(f"  CEILING on coverage      {k_r}/{n_r} = {k_r / n_r:.0%}  (CI {lo_r:.0%}-{hi_r:.0%})")
    print(f"  authors clearing {_BAR}     {k_a}/{n_a} = {k_a / n_a:.0%}")
    print(f"  report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
