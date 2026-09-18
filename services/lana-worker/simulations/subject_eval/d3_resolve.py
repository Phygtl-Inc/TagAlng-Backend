"""
d3_resolve.py — D3: does the resolver merge two mentions of the same subject, and refuse to
merge two different ones? (contract v2 §B2)

    cd services/lana-worker/simulations/subject_eval
    python d3_resolve.py --env ../../../../.env.local.dev-backup

READ-ONLY, and no LLM. Real `latent_signals` embeddings from dev, test accounts excluded.
The ambiguous-band adjudication call is stubbed (credits are out) and every pair that would
have needed one is reported separately rather than guessed at.

WHY THIS RUNS ON REAL DATA FROM THE FIRST RUN
---------------------------------------------
Every checker I built this week passed its own fixtures and then failed the moment it met real
product output — six false positives in a day, because my invented cases were too clean. So this
one is scored against real entity text before it is trusted, and the fixtures are derived from
the data rather than imagined.

THE ALGORITHM, from §B2, as a stub
----------------------------------
  1. google_place_id present -> upsert, return. No embedding search. (not reachable here)
  2. else embed the mention
  3. ANN over existing subjects, limit 5
  4. top-1 cosine >= tau_merge -> MERGE
     top-1 cosine <= tau_new   -> no match, go to 5
     between                   -> ONE LLM adjudication (stubbed: reported, never guessed)
  5. no match -> count prior mentions by this user in this block with cosine >= tau_merge
     0 priors -> return None, create NOTHING
     >=1      -> create a provisional subject

Step 5 is the only defence against unbounded subject rows, and it is the one part of B2 that is
pure logic — so it is testable exactly, with no thresholds involved.

THE TWO FREE CLASSES, AND THE ONE THAT ISN'T FREE
-------------------------------------------------
  known-same     byte-identical entity_text. Same subject by construction. No labels needed.
  known-different  random pairs of different strings. Mostly different; noisy but usable.
  VARIANTS       "Mike's Plumbing" vs "Mike" vs "the plumber on Narcoossee". The case that
                 decides whether the resolver fragments a neighbourhood's plumber into three.
                 CANNOT be derived without judgment — this is what the 400-pair labelling is
                 for. Candidates are emitted here so the labelling starts from real pairs.
"""

from __future__ import annotations

import argparse
import ast
import json
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

TAU_MERGE = 0.86
TAU_NEW = 0.62

MERGE, NEW, ADJUDICATE, NOTHING = "merge", "new", "adjudicate", "nothing"


def _vec(v):
    return ast.literal_eval(v) if isinstance(v, str) else v


def cos(a, b) -> float:
    a, b = _vec(a), _vec(b)
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return d / (na * nb)


def decide(sim: float, *, tau_merge: float = TAU_MERGE, tau_new: float = TAU_NEW) -> str:
    """§B2 step 4, verbatim. The only place thresholds are applied."""
    if sim >= tau_merge:
        return MERGE
    if sim <= tau_new:
        return NEW
    return ADJUDICATE


def mint_decision(priors_above_tau: int) -> str:
    """§B2 step 5 — pure logic, no thresholds of its own.

    'A subject is minted on the SECOND independent mention. One mention is a signal, not a
    thing. This is the only defence against unbounded product rows.'"""
    return NEW if priors_above_tau >= 1 else NOTHING


def fetch(env: str) -> list[dict]:
    cfg = dotenv_values(env)
    url, key = cfg.get("SUPABASE_URL") or "", cfg.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        raise SystemExit(f"no credentials in {env}")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    print(f"[D3] {url}  (read-only, no LLM)")
    with httpx.Client(timeout=90) as c:
        test = {u["id"] for u in c.get(
            f"{url}/rest/v1/users?select=id&email=like.*phygtl*", headers=h).json()}
        rows: list[dict] = []
        while True:
            hh = dict(h); hh["Range"] = f"{len(rows)}-{len(rows)+999}"
            got = c.get(f"{url}/rest/v1/latent_signals"
                        f"?select=entity_text,entity_type,user_id,block_id,embedding"
                        f"&embedding=not.is.null", headers=hh).json()
            rows.extend(got)
            if len(got) < 1000:
                break
    kept = [r for r in rows if r["user_id"] not in test and (r.get("entity_text") or "").strip()]
    print(f"  {len(rows)} rows with embeddings -> {len(kept)} after excluding test accounts")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description="D3 resolution accuracy (read-only, no LLM)")
    ap.add_argument("--env", default=str(_HERE.parents[3] / ".env.local"))
    ap.add_argument("--negatives", type=int, default=8000)
    ap.add_argument("--out", default=str(_HERE / "out" / "d3_resolve.md"))
    ap.add_argument("--variants-out", default=str(_HERE / "out" / "d3_variant_candidates.json"))
    args = ap.parse_args()

    rows = fetch(args.env)
    if len(rows) < 50:
        print("  too few non-test rows"); return 2

    by_text: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_text[r["entity_text"].strip().lower()].append(r)
    uniq = [g[0] for g in by_text.values()]
    print(f"  {len(uniq)} distinct entity strings")

    # ── known-same: identical text, different rows ─────────────────────────────────────
    same: list[float] = []
    for g in by_text.values():
        for i in range(len(g)):
            for j in range(i + 1, min(len(g), i + 4)):
                same.append(cos(g[i]["embedding"], g[j]["embedding"]))
    # ── known-different: random distinct strings ───────────────────────────────────────
    rng = random.Random("d3-resolve")
    diff = [cos(a["embedding"], b["embedding"])
            for a, b in (rng.sample(uniq, 2) for _ in range(args.negatives))] if len(uniq) > 1 else []

    def tally(sims):
        t = {MERGE: 0, ADJUDICATE: 0, NEW: 0}
        for s in sims:
            t[decide(s)] += 1
        return t

    ts, td = tally(same), tally(diff)
    n_s, n_d = max(1, len(same)), max(1, len(diff))
    # Accuracy counting an ambiguous pair as UNRESOLVED, never as correct — it costs an LLM
    # call and this run cannot make one.
    correct = ts[MERGE] + td[NEW]
    scored = (ts[MERGE] + ts[NEW]) + (td[MERGE] + td[NEW])
    acc = correct / max(1, scored)
    lo, hi = wilson_ci(correct, max(1, scored))

    # ── step 5, pure logic ─────────────────────────────────────────────────────────────
    step5 = [(0, mint_decision(0)), (1, mint_decision(1)), (3, mint_decision(3))]

    # ── variant candidates for the labelling pool ──────────────────────────────────────
    # Pairs that share a distinctive token but are not identical: the shape that decides
    # whether one plumber becomes three. Emitted, never auto-labelled.
    toks: dict[str, list[str]] = defaultdict(list)
    for t in by_text:
        for w in t.split():
            if len(w) > 3:
                toks[w].append(t)
    cands = []
    for w, texts in toks.items():
        u = sorted(set(texts))
        if 2 <= len(u) <= 6:
            for i in range(len(u)):
                for j in range(i + 1, len(u)):
                    s = cos(by_text[u[i]][0]["embedding"], by_text[u[j]][0]["embedding"])
                    cands.append({"a": u[i], "b": u[j], "shared_token": w,
                                  "cosine": round(s, 4), "resolver": decide(s)})
    cands.sort(key=lambda c: -c["cosine"])
    Path(args.variants_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.variants_out).write_text(json.dumps(cands[:400], indent=1), encoding="utf-8")

    L = ["# D3 — resolver accuracy (stub, real data)", "",
         f"- real `latent_signals` embeddings, test accounts excluded · **no LLM** · read-only",
         f"- {len(rows)} rows -> {len(uniq)} distinct strings · τ_merge={TAU_MERGE} τ_new={TAU_NEW}",
         "", "## Decisions", "",
         "| population | n | merge | adjudicate | new |", "|---|---|---|---|---|",
         f"| known-same (identical text) | {len(same)} | **{ts[MERGE]/n_s:.0%}** | "
         f"{ts[ADJUDICATE]/n_s:.0%} | {ts[NEW]/n_s:.0%} |",
         f"| known-different (random) | {len(diff)} | {td[MERGE]/n_d:.2%} | "
         f"{td[ADJUDICATE]/n_d:.0%} | **{td[NEW]/n_d:.0%}** |", "",
         f"**Accuracy on decided pairs: {acc:.1%} (95% CI {lo:.0%}–{hi:.0%})** — "
         f"gate is 0.90 pass / 0.80 fail.", "",
         "_Ambiguous pairs are counted as UNRESOLVED, never as correct: each costs one LLM "
         "adjudication and this run could not make one._", "",
         "## Step 5 — mint on the second mention", "",
         "_Pure logic, no thresholds. The only defence against unbounded subject rows._", "",
         "| prior mentions | decision |", "|---|---|"]
    L += [f"| {p} | `{d}` |" for p, d in step5]
    L += ["", "## What this does NOT measure", "",
          "The class that decides whether a neighbourhood's plumber becomes three rows: **same "
          "subject, different wording**. It cannot be derived without judgment, which is what "
          f"the 400-pair labelling is for. **{len(cands)} candidate variant pairs** were mined "
          f"from real text and written to `{Path(args.variants_out).name}` so that labelling "
          "starts from real pairs rather than invented ones.", ""]
    if cands:
        L += ["### Highest-similarity variant candidates", "",
              "| a | b | cosine | resolver would |", "|---|---|---|---|"]
        L += [f"| {c['a']} | {c['b']} | {c['cosine']} | `{c['resolver']}` |" for c in cands[:12]]
        L.append("")
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")

    print(f"\n  known-same     merge {ts[MERGE]/n_s:.0%}  adjudicate {ts[ADJUDICATE]/n_s:.0%}  new {ts[NEW]/n_s:.0%}")
    print(f"  known-different merge {td[MERGE]/n_d:.2%}  adjudicate {td[ADJUDICATE]/n_d:.0%}  new {td[NEW]/n_d:.0%}")
    print(f"  accuracy on decided pairs  {acc:.1%}  (CI {lo:.0%}-{hi:.0%})   gate 0.90/0.80")
    print(f"  variant candidates mined   {len(cands)}  -> {Path(args.variants_out).name}")
    print(f"  report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
