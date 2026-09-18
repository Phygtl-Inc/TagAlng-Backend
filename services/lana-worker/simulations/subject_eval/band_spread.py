"""
band_spread.py — §4.4: on the recommendation path, what fraction of a top-10 result set falls
entirely inside ±0.05 cosine? (Pouya's note 2)

    cd services/lana-worker/simulations/subject_eval
    python band_spread.py --env ../../../../.env.local.dev-backup

WHY THIS IS NOT BUILT AS LITERALLY SPECIFIED, AND WHY THAT'S A FINDING OF ITS OWN
------------------------------------------------------------------------------------
§4.4 as written: "Embed ~30 realistic asks against the existing tip_share subjects." Checked
both halves before writing a line of this file, 2026-09-17:

  1. `local_signals` (where `attester_authority()`'s behavioural tier looks for tip_share
     embeddings) has **zero rows with a non-null embedding, of ANY intent** — not just
     tip_share. `select id from local_signals where embedding is not null` returns 0 of the
     ~1000-row sample checked. This means `attester_authority()`'s ENTIRE behavioural
     component (§A1, up to 0.40 of the 1.0 ceiling) is currently unreachable for every user in
     dev, not just for someone gaming it — worth its own line to Asjid/Pouya, separate from
     this measurement.
  2. Embedding 30 new ask strings needs `vertex_embed()` (the same model the stored vectors
     use — comparing an OpenAI embedding against a Vertex one would be comparing different
     vector spaces, not a cosine gap). `vertex_embed()` raises `DefaultCredentialsError` in
     this environment: no Application Default Credentials configured locally.

Both would need to be true for §4.4 as scoped to run at all. Rather than skip the question,
this reuses the exact real-data pattern D3 already established: `latent_signals` has 4,163
embedded rows (confirmed live), so EXISTING entity texts stand in as "asks" — pick one, find
its real top-10 nearest OTHER entities by cosine among the already-stored vectors, measure the
spread. Zero new embedding calls. Same caveat D3 already carries and already got accepted with:
these are Layer-3 entity mentions ("mom friends", "cooking class"), not the named places/
people/products `subject` will hold — indicative, not final, exactly as D3's own report says.
"""

from __future__ import annotations

import argparse
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
from d3_resolve import cos  # noqa: E402 — reuse, don't reimplement
from run_eval import wilson_ci  # noqa: E402

BAND = 0.05
TOP_K = 10


def fetch(env: str) -> list[dict]:
    cfg = dotenv_values(env)
    url, key = cfg.get("SUPABASE_URL") or "", cfg.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        raise SystemExit(f"no credentials in {env}")
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    print(f"[band-spread] {url} (read-only, no LLM, no new embedding calls)")
    with httpx.Client(timeout=90) as c:
        test = {u["id"] for u in c.get(
            f"{url}/rest/v1/users?select=id&email=like.*phygtl*", headers=h).json()}
        rows: list[dict] = []
        while True:
            hh = dict(h); hh["Range"] = f"{len(rows)}-{len(rows)+999}"
            got = c.get(f"{url}/rest/v1/latent_signals"
                        f"?select=entity_text,user_id,embedding&embedding=not.is.null",
                        headers=hh).json()
            rows.extend(got)
            if len(got) < 1000:
                break
    kept = [r for r in rows if r["user_id"] not in test and (r.get("entity_text") or "").strip()]
    print(f"  {len(rows)} rows with embeddings -> {len(kept)} after excluding test accounts")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description="§4.4 band-spread measurement (proxy corpus)")
    ap.add_argument("--env", default=str(_HERE.parents[3] / ".env.local"))
    ap.add_argument("--asks", type=int, default=30)
    ap.add_argument("--out", default=str(_HERE / "out" / "band_spread.md"))
    args = ap.parse_args()

    rows = fetch(args.env)
    if len(rows) < args.asks + TOP_K:
        print("  too few embedded rows"); return 2

    by_text: dict[str, dict] = {}
    for r in rows:
        by_text.setdefault(r["entity_text"].strip().lower(), r)   # one row per distinct string
    uniq = list(by_text.values())
    print(f"  {len(uniq)} distinct entity strings")

    rng = random.Random("band-spread-4.4")
    asks = rng.sample(uniq, min(args.asks, len(uniq)))

    inside_band = 0
    spreads: list[float] = []
    rows_report: list[tuple[str, float, float, float]] = []   # text, top1, top10, spread
    for ask in asks:
        others = [r for r in uniq if r is not ask]
        sims = sorted((cos(ask["embedding"], o["embedding"]) for o in others), reverse=True)
        top10 = sims[:TOP_K]
        spread = top10[0] - top10[-1]
        spreads.append(spread)
        if spread <= BAND:
            inside_band += 1
        rows_report.append((ask["entity_text"], top10[0], top10[-1], spread))

    n = len(asks)
    lo, hi = wilson_ci(inside_band, n)
    mean_spread = sum(spreads) / n

    print(f"\n  {inside_band}/{n} ({inside_band/n:.0%}, CI {lo:.0%}-{hi:.0%}) of top-10 sets "
          f"fall entirely inside ±{BAND} cosine")
    print(f"  mean top-10 spread: {mean_spread:.3f}")

    L = ["# Band-spread measurement (§4.4, Pouya's note 2)", "",
         "**Not built as literally scoped — see module docstring.** `local_signals` has zero "
         "embedded rows of any intent (attester_authority's behavioural tier is currently "
         "unreachable for everyone, not a measurement finding but worth flagging on its own), "
         "and `vertex_embed()` has no local ADC to embed new ask strings with. This reuses "
         "`latent_signals`' 4,163 already-embedded rows as a PROXY: existing entity texts stand "
         "in as 'asks', real stored embeddings, zero new embedding calls. Same caveat D3 "
         "already carries — entity mentions, not the named places/people/products `subject` "
         "will hold. Indicative, not final.", "",
         f"**{inside_band}/{n} ({inside_band/n:.0%}, 95% CI {lo:.0%}-{hi:.0%}) of top-10 sets "
         f"fall entirely inside ±{BAND} cosine. Mean spread: {mean_spread:.3f}.**", "",
         "## What this means for §6.1", "",
         f"A high in-band share would mean the ±{BAND} secondary sort is decorative and "
         "whatever breaks the tie (authority, distance, recency) IS the ranking — making §6.1 "
         "(does authority outrank shared-community?) load-bearing rather than a tuning "
         "detail. A low share means the primary cosine order usually already separates "
         "candidates before any secondary rule gets a say.", "",
         "## Sample (10 tightest and 10 widest)", "",
         "| ask (proxy) | top-1 | top-10 | spread |", "|---|---|---|---|"]
    rows_report.sort(key=lambda r: r[3])
    sample_rows = rows_report[:10]
    if n > 20:
        L_sample_gap = True
    else:
        L_sample_gap = False
    for text, top1, top10v, spread in sample_rows:
        L.append(f"| {text} | {top1:.3f} | {top10v:.3f} | {spread:.3f} |")
    if L_sample_gap:
        L.append("| … | | | |")
    for text, top1, top10v, spread in rows_report[-10:]:
        L.append(f"| {text} | {top1:.3f} | {top10v:.3f} | {spread:.3f} |")
    L.append("")

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"  report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
