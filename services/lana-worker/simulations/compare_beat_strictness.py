"""
compare_beat_strictness.py

Answers Tommaso's question ("should five_beat_landed be lenient or strict?") without
running a real A/B test — there's no user-facing traffic to split, so instead this
re-scores already-collected transcripts under both rubric variants and reports every
case where the verdict diverges. Gives a concrete diff to look at instead of an opinion.

Usage:
    cd services/lana-worker/simulations
    python compare_beat_strictness.py                      # latest run log in scratch/
    python compare_beat_strictness.py scratch/run_...json   # a specific run log

Only re-scores out_of_scope_rejection transcripts (the only bucket five_beat_landed
applies to). Does NOT re-run simulations or write to Supabase — it judges the
transcripts already stored in the run log twice (once per strictness mode) using
evaluation.score_five_beat_only().
"""

import glob
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[3] / ".env.local", override=True)

from evaluation import score_five_beat_only  # noqa: E402


def _latest_run_log() -> str:
    files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "scratch", "run_*.json")))
    if not files:
        raise SystemExit("No run logs found in scratch/ — run runner.py first.")
    return files[-1]


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else _latest_run_log()
    print(f"[compare] loading {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    oos_results = [r for r in data["results"] if r["bucket"] == "out_of_scope_rejection"]
    if not oos_results:
        raise SystemExit("No out_of_scope_rejection transcripts in this run log.")

    print(f"[compare] {len(oos_results)} out_of_scope_rejection transcripts — scoring strict + lenient\n")

    rows = []
    for r in oos_results:
        transcript = r["transcript"]
        label = f"{r['persona_id']} x {r['seed_label']}"
        strict = score_five_beat_only(transcript, strictness="strict")
        lenient = score_five_beat_only(transcript, strictness="lenient")
        diverged = strict.verdict != lenient.verdict
        rows.append((label, strict, lenient, diverged))
        flag = "  <-- DIVERGES" if diverged else ""
        print(f"[{label}] strict={strict.verdict} ({strict.score:.2f})  lenient={lenient.verdict} ({lenient.score:.2f}){flag}")

    n = len(rows)
    n_diverged = sum(1 for *_, d in rows if d)
    strict_pass = sum(1 for _, s, _, _ in rows if s.verdict == "PASS")
    lenient_pass = sum(1 for _, _, l, _ in rows if l.verdict == "PASS")

    print("\n[compare] summary")
    print(f"  strict PASS rate:  {strict_pass}/{n}")
    print(f"  lenient PASS rate: {lenient_pass}/{n}")
    print(f"  verdict diverged:  {n_diverged}/{n}")

    if n_diverged:
        print("\n[compare] divergent cases (reasoning)")
        for label, s, l, d in rows:
            if not d:
                continue
            print(f"\n  {label}")
            print(f"    strict  [{s.verdict}] {s.reasoning}")
            print(f"    lenient [{l.verdict}] {l.reasoning}")


if __name__ == "__main__":
    main()
