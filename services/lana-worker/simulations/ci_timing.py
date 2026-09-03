"""
ci_timing.py — measure per-case duration of the sim-gate matrix, and model the PR-gate subset.

The PR `Sim gate` runs the must-have buckets (runner.py --pr); the nightly runs the full 6×32 = 192
matrix. This tool times each case so the calibrated estimates below can be replaced with measured
numbers, and it can run just the reduced PR manifest.

    python ci_timing.py --estimate        # no API, no worker — print the calibrated model + totals
    python ci_timing.py --tier pr         # run ONLY the 16-case PR manifest, time each case (live)
    python ci_timing.py --tier full       # time the full 192 matrix (the current gate)
    python ci_timing.py --tier pr --limit 3   # first 3 cases only (quick live smoke)

Live runs need the same env as runner.py (LANA_BASE_URL + OPENAI_API_KEY + SUPABASE_* + SIM_PASSWORD).
Writes out/ci_timing.md + out/ci_timing.csv. Never writes to Supabase beyond what evaluation.score
already does.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parents[3] / ".env.local", override=True)

import evaluation
import simulation

OUT_DIR = Path(__file__).parent / "out"

# --- Calibrated model (anchored to real turn counts + the 2.5 h/192-run total): (sec_per_case, mean_turns) ---
# mean_turns is REAL (321 past runs) except find_coverage/host_confirm (estimated — no runs yet).
EST: dict[str, tuple[float, float]] = {
    "out_of_scope_rejection": (36, 3.4),
    "host_confirm":           (41, 4.0),   # estimated
    "edge_trust":             (45, 4.5),   # real Lana latency suggests this may run ~2x heavier
    "in_scope_success":       (45, 4.6),
    "find_coverage":          (49, 5.0),   # estimated
    "edge_cases":             (52, 5.5),
    "ambiguous_clarity":      (56, 6.0),
}

# --- The PR-gate manifest: 16 cases, 1 persona/seed, chosen to stress each seed. ---
# (persona_id, bucket, seed_label). Nightly keeps the full 6×32 matrix.
PR_MANIFEST: list[tuple[str, str, str]] = [
    # P0 — safety / privacy / refusals: FULL seed coverage
    ("P1", "out_of_scope_rejection", "uber to airport"),
    ("P6", "out_of_scope_rejection", "do my taxes"),
    ("P2", "out_of_scope_rejection", "pay my bills"),
    ("P3", "out_of_scope_rejection", "medical advice"),
    ("P6", "edge_trust", "safety question gets a real answer"),
    ("P1", "edge_trust", "privacy pushback respected"),
    ("P3", "edge_trust", "language mirrored"),          # bilingual persona
    ("P4", "edge_trust", "no internal debug strings leak"),
    ("P2", "edge_trust", "kid pii echo"),               # persona with young kids
    # P1 — hard-fail gate buckets / core function: sampled
    ("P1", "ambiguous_clarity", "pizza ambiguous"),
    ("P6", "ambiguous_clarity", "host a meeting ambiguous"),
    ("P1", "in_scope_success", "create meet happy path"),
    ("P3", "in_scope_success", "find neighbors happy path"),
    # P2 — coverage/quality canaries: 1 case each
    ("P1", "find_coverage", "refinement narrows results"),
    ("P1", "host_confirm", "yes advances the draft"),
    ("P5", "edge_cases", "mid-convo locale switch"),    # Spanglish persona
]


def _estimate() -> None:
    print("Calibrated model (no API). Anchored to real turn counts + the 2.5 h/192-run total.\n")
    personas = simulation.load_personas()
    buckets = simulation.load_buckets()
    npersona = len(personas)
    print(f"{'bucket':24} {'sec/case':>9} {'full cases':>11} {'full time':>10}")
    total_full = 0.0
    for b in buckets:
        sec, _ = EST.get(b.bucket, (45, 4.6))
        cases = npersona * len(b.seeds)
        t = sec * cases
        total_full += t
        print(f"{b.bucket:24} {sec:>7.0f}s {cases:>11} {t/60:>8.1f}m")
    print(f"{'TOTAL (full matrix)':24} {'':>9} {'':>11} {total_full/60:>8.1f}m  (~{total_full/3600:.1f} h)")

    pr_t = sum(EST.get(bk, (45, 0))[0] for _, bk, _ in PR_MANIFEST)
    print(f"\nPR manifest: {len(PR_MANIFEST)} cases → ~{pr_t/60:.1f} min sequential "
          f"(target ≤15). With 6-way persona parallelism the full matrix is ~{total_full/6/60:.0f} min.")


def _resolve_matrix(tier: str):
    personas = {p.id: p for p in simulation.load_personas()}
    buckets = {b.bucket: b for b in simulation.load_buckets()}
    cases = []
    if tier == "full":
        for p in personas.values():
            for b in buckets.values():
                for s in b.seeds:
                    cases.append((p, b, s))
    else:  # pr
        for pid, bname, slabel in PR_MANIFEST:
            b = buckets.get(bname)
            if not b:
                print(f"  [warn] bucket {bname!r} not found, skipping"); continue
            s = next((x for x in b.seeds if x.label == slabel), None)
            p = personas.get(pid)
            if not (s and p):
                print(f"  [warn] {pid}×{bname}/{slabel!r} not found, skipping"); continue
            cases.append((p, b, s))
    return cases


def _run(tier: str, limit: int | None) -> None:
    cases = _resolve_matrix(tier)
    if limit:
        cases = cases[:limit]
    print(f"[ci_timing] tier={tier} — timing {len(cases)} cases\n")
    rows = []
    wall0 = time.perf_counter()
    for i, (persona, bucket, seed) in enumerate(cases, 1):
        t0 = time.perf_counter()
        try:
            transcript = simulation.run(persona, bucket, seed)
            t_sim = time.perf_counter() - t0
            t1 = time.perf_counter()
            evaluation.score_qa(transcript) or evaluation.score(transcript)
            t_eval = time.perf_counter() - t1
            turns = transcript.get("turn_count", len(transcript.get("turns", [])))
            total = t_sim + t_eval
            rows.append({"persona": persona.id, "bucket": bucket.bucket, "seed": seed.label,
                         "turns": turns, "sim_s": round(t_sim, 1), "eval_s": round(t_eval, 1),
                         "total_s": round(total, 1)})
            print(f"  {i}/{len(cases)} {persona.id}×{bucket.bucket}/{seed.label}: "
                  f"{total:.1f}s (sim {t_sim:.1f} + eval {t_eval:.1f}, {turns} turns)")
        except Exception as e:  # keep timing the rest
            rows.append({"persona": persona.id, "bucket": bucket.bucket, "seed": seed.label,
                         "turns": 0, "sim_s": 0, "eval_s": 0, "total_s": round(time.perf_counter() - t0, 1),
                         "error": str(e)})
            print(f"  {i}/{len(cases)} {persona.id}×{bucket.bucket}/{seed.label}: ERROR {e}")
    wall = time.perf_counter() - wall0
    _write(rows, tier, wall)


def _write(rows: list[dict], tier: str, wall: float) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    # csv
    with open(OUT_DIR / "ci_timing.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["persona", "bucket", "seed", "turns", "sim_s", "eval_s", "total_s", "error"])
        w.writeheader()
        for r in rows:
            w.writerow({**{k: "" for k in w.fieldnames}, **r})
    # per-bucket aggregate
    by_b: dict[str, list[float]] = {}
    for r in rows:
        by_b.setdefault(r["bucket"], []).append(r["total_s"])
    L = [f"# ci_timing — tier `{tier}`", "",
         f"- cases: **{len(rows)}**  ·  wall-clock: **{wall/60:.1f} min**  ·  "
         f"sum of case times: {sum(r['total_s'] for r in rows)/60:.1f} min", "",
         "| bucket | cases | avg s/case | bucket total |", "|---|---|---|---|"]
    for b, ts in sorted(by_b.items(), key=lambda kv: -sum(kv[1])):
        L.append(f"| {b} | {len(ts)} | {sum(ts)/len(ts):.1f} | {sum(ts)/60:.1f} min |")
    L += ["", "## per-case", "", "| persona | bucket | seed | turns | sim_s | eval_s | total_s |",
          "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: -r["total_s"]):
        L.append(f"| {r['persona']} | {r['bucket']} | {r['seed']} | {r['turns']} | "
                 f"{r['sim_s']} | {r['eval_s']} | {r['total_s']}{' ⚠️'+r['error'] if r.get('error') else ''} |")
    (OUT_DIR / "ci_timing.md").write_text("\n".join(L), encoding="utf-8")
    print(f"\n[ci_timing] wall-clock {wall/60:.1f} min · report → {OUT_DIR/'ci_timing.md'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Time the sim-gate matrix / PR subset.")
    ap.add_argument("--tier", choices=["full", "pr"], default="full")
    ap.add_argument("--estimate", action="store_true", help="no API — print the calibrated model")
    ap.add_argument("--limit", type=int, help="cap number of cases (quick live smoke)")
    args = ap.parse_args()
    if args.estimate:
        _estimate()
        return 0
    _run(args.tier, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
