"""
run_eval.py — entry point for the Conversational-Policy eval harness.

    cd services/lana-worker/simulations/policy_eval
    python run_eval.py --dry-run              # no API key, no server — smoke-test the pipeline
    python run_eval.py --backend stub --judge # reference policy + LLM judge (needs OPENAI_API_KEY)
    python run_eval.py --backend stub --judge --multi-judge   # 3-stance judge + disagreement flag
    python run_eval.py --backend live --judge # score REAL Lana (needs a running worker + account)
    python run_eval.py --id sf_crisis_distress --judge        # one scenario
    python run_eval.py --bucket lingo_tone                     # one bucket

Writes out/report.md (+ prints a summary). Mechanical axes always run; judged axes run only with
--judge. --gate makes the process exit 1 on any HARD_FAIL (for CI use).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow bare intra-package imports

import checks  # noqa: E402
import judge as judge_mod  # noqa: E402
from backend import get_backend  # noqa: E402
from ports import NextAction  # noqa: E402
from scenarios import ALL_SCENARIOS, BUCKETS, Scenario, by_id  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


@dataclass
class RunRecord:
    scenario: Scenario
    action: NextAction
    mechanical: list[checks.CheckResult]
    judged: judge_mod.JudgeResult | None = None
    error: str | None = None

    @property
    def mech_verdict(self) -> str:
        if self.error is not None:
            return "HARD_FAIL"  # a backend crash is a failure, never a silent PASS on an empty check list
        return checks.overall_verdict(self.mechanical) if self.mechanical else "PASS"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(scenarios: list[Scenario], *, backend_kind: str, do_judge: bool,
        n_judges: int, blind: bool = False) -> list[RunRecord]:
    backend = get_backend(backend_kind)
    judge_client = None
    if do_judge:
        from openai import OpenAI
        if not judge_mod.OPENAI_API_KEY:
            raise RuntimeError("--judge needs OPENAI_API_KEY")
        judge_client = OpenAI(api_key=judge_mod.OPENAI_API_KEY)

    records: list[RunRecord] = []
    for sc in scenarios:
        print(f"\n[policy-eval] {sc.id} ({sc.bucket})")
        try:
            action = backend.decide_turn(sc.context())
        except Exception as e:  # a backend failure shouldn't abort the whole run
            print(f"  [error] backend.decide_turn failed: {e}")
            records.append(RunRecord(scenario=sc, action=NextAction(kind="reply", utterance=""),
                                     mechanical=[], error=str(e)))
            continue
        print(f"  kind={action.kind} tool={action.tool} | {action.utterance[:80]!r}")
        mech = checks.run_mechanical_checks(action, sc, backend_kind=backend_kind)
        for r in mech:
            flag = "" if r.verdict == "PASS" else f"  <-- {r.verdict}"
            print(f"  [mech] {r.name}: {r.verdict}{flag}")
        judged = None
        # Live replays only user turns; the judge would otherwise grade against scripted assistant
        # history the live server never saw. So skip judged axes for multi-turn scenarios in live mode.
        judge_invalid_live = backend_kind == "live" and bool(sc.recent)
        if do_judge and sc.judge_axes and judge_invalid_live:
            print("  [judge] skipped — live multi-turn history is mis-attributed (see live_policy.py)")
        elif do_judge and sc.judge_axes:
            judged = judge_mod.score_scenario(sc, action, n_judges=n_judges, client=judge_client,
                                              backend_kind=backend_kind, blind=blind)
            for a in judged.axes:
                dis = " (DISAGREE)" if a.disagreement else ""
                print(f"  [judge] {a.axis}: {a.majority_verdict} ({a.mean_score}){dis}")
        records.append(RunRecord(scenario=sc, action=action, mechanical=mech, judged=judged))
    return records


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def render_report(records: list[RunRecord], *, backend_kind: str, judged: bool) -> str:
    L: list[str] = []
    L.append("# Conversational-Policy eval — report")
    L.append("")
    L.append(f"- backend: `{backend_kind}`  ·  judged axes: `{judged}`  ·  scenarios: {len(records)}")
    if backend_kind == "dry":
        L.append("- **dry run** — DryPolicy canned actions; proves pipeline plumbing, not policy quality.")
    if backend_kind == "stub":
        L.append("- **stub** — reference policy under the PART 1-7 constitution; judged axes are "
                 "partly circular (see judge.py). Mechanical axes are the non-circular signal.")
        exemplars = [r for r in records if r.scenario.exemplar_of_stub]
        if exemplars:
            L.append(f"- ⚠️ **{len(exemplars)} scenarios overlap the stub's few-shot exemplars** "
                     f"({', '.join(r.scenario.id for r in exemplars)}) — a stub verdict on these "
                     f"(esp. right_action / expected_kind) measures parroting, not independent "
                     f"quality. Flagged per-scenario below; fully valid against `live`.")
    if backend_kind == "live":
        L.append("- **live** — REAL runtime output. `why`/typed-chip-actions/`kind` are flagged as "
                 "not-emitted (see live_policy.py); capability-grounding is indicative only.")
    L.append("")

    # --- mechanical summary by bucket ---
    L.append("## Mechanical axes (known-by-construction ground truth)")
    L.append("")
    L.append("| bucket | scenarios | PASS | SOFT_FAIL | HARD_FAIL |")
    L.append("|---|---|---|---|---|")
    for b in BUCKETS:
        rs = [r for r in records if r.scenario.bucket == b]
        if not rs:
            continue
        p = sum(r.mech_verdict == "PASS" for r in rs)
        s = sum(r.mech_verdict == "SOFT_FAIL" for r in rs)
        h = sum(r.mech_verdict == "HARD_FAIL" for r in rs)
        L.append(f"| {b} | {len(rs)} | {p} | {s} | {h} |")
    L.append("")

    # --- per-axis mechanical failure tallies ---
    axis_fail: dict[str, int] = {}
    for r in records:
        for c in r.mechanical:
            if c.verdict != "PASS":
                axis_fail[c.name] = axis_fail.get(c.name, 0) + 1
    if axis_fail:
        L.append("### Mechanical failures by axis")
        L.append("")
        for name, n in sorted(axis_fail.items(), key=lambda kv: -kv[1]):
            L.append(f"- `{name}`: {n}")
        L.append("")

    # --- judged summary ---
    if judged:
        L.append("## Judged axes (LLM; multi-judge majority + disagreement)")
        L.append("")
        L.append("_UNSCORED = the judge dropped the axis (surfaced, never a silent PASS). "
                 "REVIEW = no verdict plurality across stances → route to human spot-audit._")
        L.append("")
        L.append("| axis | scored | PASS | SOFT_FAIL | HARD_FAIL | UNSCORED | REVIEW | disagreements |")
        L.append("|---|---|---|---|---|---|---|---|")
        agg: dict[str, dict[str, int]] = {}
        for r in records:
            if not r.judged:
                continue
            for a in r.judged.axes:
                d = agg.setdefault(a.axis, {})
                d["n"] = d.get("n", 0) + 1
                d[a.majority_verdict] = d.get(a.majority_verdict, 0) + 1
                d["dis"] = d.get("dis", 0) + int(a.disagreement)
        for axis, d in sorted(agg.items()):
            L.append(f"| {axis} | {d.get('n',0)} | {d.get('PASS',0)} | {d.get('SOFT_FAIL',0)} | "
                     f"{d.get('HARD_FAIL',0)} | {d.get('UNSCORED',0)} | {d.get('REVIEW',0)} | {d.get('dis',0)} |")
        L.append("")

    # --- detail: everything that wasn't a clean pass ---
    L.append("## Findings (non-PASS detail)")
    L.append("")
    any_finding = False
    for r in records:
        problems = [c for c in r.mechanical if c.verdict != "PASS"]
        jproblems = [a for a in (r.judged.axes if r.judged else []) if a.majority_verdict != "PASS"]
        if not problems and not jproblems and not r.error:
            continue
        any_finding = True
        tag = " — ⚠️ stub-exemplar (parroting risk)" if (backend_kind == "stub" and r.scenario.exemplar_of_stub) else ""
        L.append(f"### `{r.scenario.id}` ({r.scenario.bucket}){tag}")
        L.append(f"- utterance: {r.action.utterance[:200]!r}")
        if r.error:
            L.append(f"- **backend error**: {r.error}")
        for c in problems:
            L.append(f"- **{c.verdict}** `{c.name}` — {c.detail}")
        for a in jproblems:
            dis = " · JUDGES DISAGREE" if a.disagreement else ""
            L.append(f"- **{a.majority_verdict}** (judged) `{a.axis}`{dis} — {a.reasonings[0] if a.reasonings else ''}")
        L.append("")
    if not any_finding:
        L.append("_All scenarios passed every applicable axis._")
        L.append("")

    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Conversational-Policy eval harness")
    ap.add_argument("--backend", choices=["stub", "live", "dry"], default=None)
    ap.add_argument("--dry-run", action="store_true", help="alias for --backend dry, judge off")
    ap.add_argument("--judge", action="store_true", help="run the LLM-judged axes (needs OPENAI_API_KEY)")
    ap.add_argument("--multi-judge", action="store_true", help="3-stance judge + disagreement flag")
    ap.add_argument("--blind-judge", action="store_true",
                    help="omit judge_focus from the judge prompt (blind vs guided = the real calibration probe)")
    ap.add_argument("--id", help="run one scenario by id")
    ap.add_argument("--bucket", choices=BUCKETS, help="run one bucket")
    ap.add_argument("--gate", action="store_true", help="exit 1 on any HARD_FAIL (CI mode)")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "report.md"))
    args = ap.parse_args()

    backend_kind = "dry" if args.dry_run else (args.backend or "stub")
    do_judge = args.judge and not args.dry_run
    n_judges = 3 if args.multi_judge else 1

    if args.id:
        sc = by_id(args.id)
        if not sc:
            print(f"no scenario with id={args.id!r}")
            return 2
        scenarios = [sc]
    elif args.bucket:
        scenarios = [s for s in ALL_SCENARIOS if s.bucket == args.bucket]
    else:
        scenarios = ALL_SCENARIOS

    records = run(scenarios, backend_kind=backend_kind, do_judge=do_judge, n_judges=n_judges,
                  blind=args.blind_judge)

    report = render_report(records, backend_kind=backend_kind, judged=do_judge)
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)

    # console summary
    errors = sum(1 for r in records if r.error is not None)
    hard = sum(r.mech_verdict == "HARD_FAIL" for r in records)
    soft = sum(r.mech_verdict == "SOFT_FAIL" for r in records)
    jhard = sum(1 for r in records if r.judged and r.judged.any_hard_fail)
    junscored = sum(1 for r in records if r.judged and r.judged.any_unscored)
    jdis = sum(1 for r in records if r.judged and r.judged.any_disagreement)
    print("\n" + "=" * 60)
    print(f"[policy-eval] {len(records)} scenarios · backend={backend_kind}")
    print(f"  mechanical: {hard} HARD_FAIL ({errors} backend error), {soft} SOFT_FAIL")
    if do_judge:
        print(f"  judged: {jhard} HARD_FAIL, {junscored} UNSCORED (judge dropped an axis), "
              f"{jdis} disagreement/REVIEW")
    print(f"  report -> {args.out}")

    # Gate fails on any mechanical HARD_FAIL (incl. backend errors), any judged HARD_FAIL, and any
    # UNSCORED safety/privacy axis (fail-closed — never let a dropped safety axis pass silently).
    if args.gate and (hard > 0 or jhard > 0 or junscored > 0):
        print("[policy-eval] GATE FAIL")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
