"""
n1_thresholds.py — is tau_merge = 0.86 reachable at all? (contract v2 §B2 / D11)

    cd services/lana-worker/simulations/subject_eval
    python n1_thresholds.py --env ../../../../.env.local.dev-backup

READ-ONLY. Selects only. No embedding calls: `latent_signals.embedding` is the embedding of
`entity_text` (verified — identical entity text with different utterances gives cosine 1.0000),
so every pairwise cosine comes straight out of the database.

WHY THIS RUNS BEFORE THE 400-PAIR LABELLING
-------------------------------------------
D11 is specified as: sample 300 pairs, hand-label, pick tau_merge at precision >= 0.95 and
tau_new at recall >= 0.95. That is ~1.5 days of labelling.

Before spending it, one question settles a great deal: **what does the cosine distribution
actually look like on this model?** If almost no pair reaches 0.86, tau_merge can essentially
never fire, the resolver never merges, and every mention mints a new subject — Asjid's
failure mode 2 at scale, silently. No labels are needed to see that.

The repo already documents this failure once, on the same embedding model
(`app/latent_extract.py:28-30`): a spec's 0.65 threshold, borrowed from a different model,
"never cleared here, leaving suggestion_queue empty."

A FREE POSITIVE CLASS
---------------------
Two rows with byte-identical `entity_text` are the same subject by construction. That gives a
known-same population with no hand-labelling at all. Random different-string pairs are
*mostly* different — a noisy negative class, but the two distributions only have to be
separable, not pure. If the known-same class sits below tau_merge, the threshold is wrong and
no amount of labelling will rescue it.

TEST ACCOUNTS ARE EXCLUDED. 81% of `latent_signals` on dev come from phygtl test/team
accounts, and mock-user text is LLM-generated — more canonical than real human phrasing.
Calibrating on it would bias every threshold downstream.
"""

from __future__ import annotations

import argparse
import ast
import math
import random
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

TAU_MERGE = 0.86   # contract v2 §B2
TAU_NEW = 0.62     # contract v2 §B2

# Rough name-class buckets, per Asjid's point that business names and people's names behave
# differently and a threshold tuned on cafes will split plumbers.
_DESCRIPTIVE = ("the ", "a ", "that ", "our ", "my ")


def _vec(v):
    return ast.literal_eval(v) if isinstance(v, str) else v


def cos(a, b) -> float:
    a, b = _vec(a), _vec(b)
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return d / (na * nb) if na and nb else 0.0


def name_class(text: str) -> str:
    t = text.strip().lower()
    if any(t.startswith(p) for p in _DESCRIPTIVE):
        return "descriptive"
    words = t.split()
    if len(words) == 1:
        return "single_token"
    if any(w[:1].isupper() for w in text.split()) and len(words) <= 3:
        return "proper_short"
    return "phrase"


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def main() -> int:
    ap = argparse.ArgumentParser(description="N1 threshold reachability (read-only)")
    ap.add_argument("--env", default=str(_HERE.parents[3] / ".env.local"))
    ap.add_argument("--pairs", type=int, default=20000, help="random different-string pairs")
    ap.add_argument("--out", default=str(_HERE / "out" / "n1_thresholds.md"))
    args = ap.parse_args()

    cfg = dotenv_values(args.env)
    url, key = cfg.get("SUPABASE_URL") or "", cfg.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        print(f"no credentials in {args.env}")
        return 2
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    print(f"[N1] {url}  (read-only, no embedding calls)")

    with httpx.Client(timeout=90) as c:
        test_ids = {u["id"] for u in c.get(
            f"{url}/rest/v1/users?select=id&email=like.*phygtl*", headers=h).json()}
        rows: list[dict] = []
        while True:
            hh = dict(h); hh["Range"] = f"{len(rows)}-{len(rows) + 999}"
            got = c.get(f"{url}/rest/v1/latent_signals"
                        f"?select=entity_text,entity_type,user_id,embedding"
                        f"&embedding=not.is.null", headers=hh).json()
            rows.extend(got)
            if len(got) < 1000:
                break

    kept = [r for r in rows if r["user_id"] not in test_ids and (r.get("entity_text") or "").strip()]
    print(f"  rows with embeddings: {len(rows)}  ·  after excluding test accounts: {len(kept)}")
    if len(kept) < 50:
        print("  too few non-test rows to calibrate")
        return 2

    # --- free positive class: byte-identical entity_text, different rows -------------------
    by_text: dict[str, list[dict]] = defaultdict(list)
    for r in kept:
        by_text[r["entity_text"].strip().lower()].append(r)
    same_pairs: list[tuple[str, float]] = []
    for t, group in by_text.items():
        for i in range(len(group)):
            for j in range(i + 1, min(len(group), i + 4)):
                same_pairs.append((t, cos(group[i]["embedding"], group[j]["embedding"])))

    # --- noisy negative class: random pairs with DIFFERENT text ---------------------------
    uniq = [g[0] for g in by_text.values()]
    rng = random.Random("n1-thresholds")
    diff: list[float] = []
    if len(uniq) > 1:
        for _ in range(args.pairs):
            a, b = rng.sample(uniq, 2)
            diff.append(cos(a["embedding"], b["embedding"]))

    same = [c for _, c in same_pairs]
    n_s, n_d = len(same), len(diff)
    above_merge_s = sum(1 for c in same if c >= TAU_MERGE)
    above_merge_d = sum(1 for c in diff if c >= TAU_MERGE)
    below_new_s = sum(1 for c in same if c <= TAU_NEW)
    band_d = sum(1 for c in diff if TAU_NEW < c < TAU_MERGE)

    lo_s, hi_s = wilson_ci(above_merge_s, max(1, n_s))

    L = ["# N1 — are the resolver thresholds reachable?", "",
         f"- `{url}` · read-only · **no embedding calls** "
         f"(`latent_signals.embedding` is the entity-text embedding, verified)",
         f"- {len(kept)} non-test rows of {len(rows)} with embeddings "
         f"(**{1 - len(kept)/max(1,len(rows)):.0%} excluded as phygtl test/team accounts**)",
         f"- {len(uniq)} distinct entity strings · {n_s} known-same pairs · {n_d} random pairs",
         "", "## The headline", "",
         f"**{above_merge_s}/{n_s} ({above_merge_s/max(1,n_s):.0%}, CI {lo_s:.0%}-{hi_s:.0%}) of "
         f"pairs that are the SAME SUBJECT BY CONSTRUCTION reach tau_merge = {TAU_MERGE}.**", ""]

    if n_s:
        L += ["| population | n | median | p90 | p99 | >= 0.86 | <= 0.62 |", "|---|---|---|---|---|---|---|",
              f"| known-same (identical text) | {n_s} | {pct(same,.5):.3f} | {pct(same,.9):.3f} | "
              f"{pct(same,.99):.3f} | {above_merge_s/n_s:.0%} | {below_new_s/n_s:.0%} |"]
    if n_d:
        L += [f"| random different text | {n_d} | {pct(diff,.5):.3f} | {pct(diff,.9):.3f} | "
              f"{pct(diff,.99):.3f} | {above_merge_d/n_d:.1%} | "
              f"{sum(1 for c in diff if c <= TAU_NEW)/n_d:.0%} |"]
    L += ["", f"- Random pairs landing in the **ambiguous band** ({TAU_NEW}–{TAU_MERGE}), each of "
          f"which costs one LLM adjudication call: **{band_d/max(1,n_d):.0%}**. D11's own "
          f"escalation clause fires above 25%.", ""]

    # by name class
    L += ["## Known-same pairs by name class", "",
          "_Asjid: business names and people's names behave differently; a threshold tuned on "
          "cafes will split plumbers._", "",
          "| class | pairs | median | >= 0.86 |", "|---|---|---|---|"]
    cls: dict[str, list[float]] = defaultdict(list)
    for t, c in same_pairs:
        cls[name_class(t)].append(c)
    for k, v in sorted(cls.items(), key=lambda kv: -len(kv[1])):
        L.append(f"| {k} | {len(v)} | {pct(v,.5):.3f} | {sum(1 for x in v if x >= TAU_MERGE)/len(v):.0%} |")
    L.append("")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")

    print(f"\n  known-same pairs      {n_s}   median cos {pct(same,.5):.3f}")
    print(f"    reaching tau_merge  {above_merge_s}/{n_s} = {above_merge_s/max(1,n_s):.0%}")
    print(f"    at/below tau_new    {below_new_s}/{n_s} = {below_new_s/max(1,n_s):.0%}  <-- these mint a NEW subject")
    print(f"  random pairs          {n_d}   median cos {pct(diff,.5):.3f}")
    print(f"    false merges        {above_merge_d}/{n_d} = {above_merge_d/max(1,n_d):.1%}")
    print(f"    ambiguous band      {band_d/max(1,n_d):.0%}  (one LLM call each)")
    print(f"  report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
