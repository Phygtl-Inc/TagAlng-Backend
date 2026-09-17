"""
run_eval.py — D5 elicitation gate (contract v2 §C4).

    cd services/lana-worker/simulations/subject_eval
    python run_eval.py                 # the full sweep, ~675 problems
    python run_eval.py --quick         # 5 per cell, for a fast check
    python run_eval.py --sharpness-sweep   # how sensitive is the answer to the free parameter?

No API key, no server, no database. Deterministic given the seeds.

WHAT IT REPORTS

    reach_rate         % of asks where max P(winner) > 0.85 within the 2-question cap.
                       This is D5 as contract v2 redefined it.
    winner_correct     % where the selector picked the asker's ACTUAL best candidate.
    confidently_wrong  % that reached the bar AND picked wrong. The dangerous cell: a gate
                       that only watched reach_rate would score these as successes.
    vs no-questions    what you get by asking nothing and taking the best mean score. If the
                       selector cannot beat this, two questions bought nothing.

Every rate carries a Wilson interval. `reco_eval` reported 43% and then 37% on an identical
re-run; a rate without an interval invites the next ordinary swing to be read as a regression.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[0]))   # simulations/ -> provenance

import population  # noqa: E402
from elicit import NoElicit, ReferenceElicit  # noqa: E402
from ports import DEFAULT_CONFIDENCE, ElicitResult, SubjectMatrix  # noqa: E402

OUT_DIR = _HERE / "out"


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson interval. Same helper as reco_eval — every rate gets one."""
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Row:
    matrix: SubjectMatrix
    result: ElicitResult
    baseline_winner: int

    @property
    def correct(self) -> bool:
        return self.result.winner == self.matrix.true_best()

    @property
    def baseline_correct(self) -> bool:
        return self.baseline_winner == self.matrix.true_best()

    @property
    def confidently_wrong(self) -> bool:
        return self.result.reached and not self.correct


def run(problems: list[SubjectMatrix], *, particles: int, sharpness: float) -> list[Row]:
    sel = ReferenceElicit(particles=particles, sharpness=sharpness)
    base = NoElicit()
    rows: list[Row] = []
    for m in problems:
        r = sel.run(m)
        rows.append(Row(matrix=m, result=r, baseline_winner=base.run(m).winner))
    return rows


def metrics(rows: list[Row]) -> dict[str, object]:
    ok = [r for r in rows if r.result.error is None]
    n = len(ok) or 1
    reached = sum(r.result.reached for r in ok)
    correct = sum(r.correct for r in ok)
    base_correct = sum(r.baseline_correct for r in ok)
    cw = sum(r.confidently_wrong for r in ok)
    asked = sum(r.result.n_questions for r in ok)
    return {
        "n": len(ok),
        "errors": len(rows) - len(ok),
        "reach_rate": reached / n,
        "reach_ci": wilson_ci(reached, n),
        "winner_correct": correct / n,
        "correct_ci": wilson_ci(correct, n),
        "baseline_correct": base_correct / n,
        "confidently_wrong": cw / n,
        "cw_ci": wilson_ci(cw, n),
        "mean_questions": asked / n,
        "mean_separation": sum(r.matrix.separation() for r in ok) / n,
    }


def _pct(x: float) -> str:
    return f"{x:.0%}"


def render(rows: list[Row], *, particles: int, sharpness: float) -> str:
    m = metrics(rows)
    lo, hi = m["reach_ci"]  # type: ignore[misc]
    clo, chi = m["correct_ci"]  # type: ignore[misc]
    wlo, whi = m["cw_ci"]  # type: ignore[misc]
    L: list[str] = []
    L.append("# Elicitation eval (D5) — report")
    L.append("")
    L.append(f"- §C4 reference selector · {particles} particles · 2-question cap · "
             f"confidence {DEFAULT_CONFIDENCE} · answer sharpness {sharpness}")
    L.append(f"- {m['n']} synthetic problems, deterministic. No LLM, no DB — "
             f"the selector has no model in the loop.")
    L.append("- The simulated asker answers from their true weights, so **every number here is "
             "an upper bound** on what a real, noisier person would give.")
    L.append("")
    L.append(f"**D5 — reach rate: {_pct(m['reach_rate'])}** "  # type: ignore[arg-type]
             f"(95% CI {_pct(lo)}–{_pct(hi)}) of asks reach P(winner) > "
             f"{DEFAULT_CONFIDENCE} within the 2-question cap.")
    L.append("")
    L.append("| metric | value | |")
    L.append("|---|---|---|")
    L.append(f"| reach rate (D5) | **{_pct(m['reach_rate'])}** | CI {_pct(lo)}–{_pct(hi)} |")  # type: ignore[arg-type]
    L.append(f"| winner correct | {_pct(m['winner_correct'])} | CI {_pct(clo)}–{_pct(chi)} |")  # type: ignore[arg-type]
    L.append(f"| **confidently wrong** | **{_pct(m['confidently_wrong'])}** | CI {_pct(wlo)}–{_pct(whi)} |")  # type: ignore[arg-type]
    L.append(f"| no-questions baseline | {_pct(m['baseline_correct'])} | correct with 0 questions |")  # type: ignore[arg-type]
    L.append(f"| mean questions asked | {m['mean_questions']:.2f} | cap is 2 |")  # type: ignore[str-format]
    L.append("")
    lift = float(m["winner_correct"]) - float(m["baseline_correct"])  # type: ignore[arg-type]
    L.append(f"_Asking up to two questions moves accuracy by **{lift:+.0%}** against asking "
             f"none. If that is near zero, the cost of C4 is not bought by its result._")
    L.append("")

    # --- by separation and sparsity: where it works and where it cannot ---
    L.append("## Where it works")
    L.append("")
    L.append("_`separation` is the utility gap between the asker's best candidate and the "
             "runner-up; `bunched` is the case where every candidate is worth the same to "
             "them — Pouya's coffee-shop regime, where the topic score cannot rank either._")
    L.append("")
    L.append("| separation | sparsity | n | reach | correct | confidently wrong | baseline |")
    L.append("|---|---|---|---|---|---|---|")
    for sep in ("clear", "mixed", "bunched"):
        for sp in (0.0, 0.3, 0.6):
            cell = [r for r in rows
                    if f"_{sep}_" in r.matrix.id and f"_s{int(sp * 100)}" in r.matrix.id]
            if not cell:
                continue
            c = metrics(cell)
            L.append(f"| {sep} | {int(sp * 100)}% | {c['n']} | "
                     f"{_pct(c['reach_rate'])} | {_pct(c['winner_correct'])} | "  # type: ignore[arg-type]
                     f"{_pct(c['confidently_wrong'])} | {_pct(c['baseline_correct'])} |")  # type: ignore[arg-type]
    L.append("")
    L.append("## By candidate count")
    L.append("")
    L.append("| candidates | n | reach | correct | confidently wrong |")
    L.append("|---|---|---|---|---|")
    for nc in (3, 5, 8):
        cell = [r for r in rows if f"_c{nc}_" in r.matrix.id]
        if not cell:
            continue
        c = metrics(cell)
        L.append(f"| {nc} | {c['n']} | {_pct(c['reach_rate'])} | "  # type: ignore[arg-type]
                 f"{_pct(c['winner_correct'])} | {_pct(c['confidently_wrong'])} |")  # type: ignore[arg-type]
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="D5 elicitation eval")
    ap.add_argument("--quick", action="store_true", help="5 problems per cell")
    ap.add_argument("--particles", type=int, default=500)
    ap.add_argument("--sharpness", type=float, default=6.0)
    ap.add_argument("--sharpness-sweep", action="store_true",
                    help="how sensitive is the result to the spec's unstated sigmoid temperature?")
    ap.add_argument("--out", default=str(OUT_DIR / "report.md"))
    args = ap.parse_args()

    problems = population.sweep(n_per_cell=5 if args.quick else 25)
    print(f"[subject-eval] {len(problems)} problems · {args.particles} particles")

    if args.sharpness_sweep:
        print("\nanswer sharpness sweep — the one parameter §C4 does not specify:\n")
        print(f"  {'sharpness':>10} {'reach':>8} {'correct':>9} {'conf-wrong':>11}")
        for s in (2.0, 4.0, 6.0, 10.0, 20.0):
            m = metrics(run(problems, particles=args.particles, sharpness=s))
            print(f"  {s:>10.1f} {m['reach_rate']:>8.0%} {m['winner_correct']:>9.0%} "  # type: ignore[str-format]
                  f"{m['confidently_wrong']:>11.0%}")  # type: ignore[str-format]
        return 0

    rows = run(problems, particles=args.particles, sharpness=args.sharpness)
    report = render(rows, particles=args.particles, sharpness=args.sharpness)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    m = metrics(rows)
    lo, hi = m["reach_ci"]  # type: ignore[misc]
    print("\n" + "=" * 64)
    print(f"  D5 reach rate      {m['reach_rate']:.0%}  (CI {lo:.0%}-{hi:.0%})")  # type: ignore[str-format]
    print(f"  winner correct     {m['winner_correct']:.0%}")  # type: ignore[str-format]
    print(f"  confidently wrong  {m['confidently_wrong']:.0%}   <-- reached the bar, picked wrong")  # type: ignore[str-format]
    print(f"  no-questions base  {m['baseline_correct']:.0%}")  # type: ignore[str-format]
    print(f"  mean questions     {m['mean_questions']:.2f}")  # type: ignore[str-format]
    print(f"  report -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
