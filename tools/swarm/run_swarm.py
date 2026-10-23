#!/usr/bin/env python3
"""Zero-bug swarm runner.

    python run_swarm.py --section P1 --run-id 2026-07-31-a
    python run_swarm.py --section P1 --personas PER-02,PER-03 --arms E-VOICE,E-CLICK
    python run_swarm.py --section P1 --dry-run          # preflight + fixtures only, no writes

Exit codes are meaningful because a scheduler consumes them:
    0  ran, and no assertion failed
    1  ran, and at least one assertion failed
    2  preflight aborted — nothing was written
    3  a hard rail was violated — investigate before running again
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from swarm.config import Config, ConfigError  # noqa: E402
from swarm.identity import AnonymousAuth, Db  # noqa: E402
from swarm.preflight import run_preflight  # noqa: E402
from swarm.registry import Registry  # noqa: E402
from swarm.runner import SectionRunner, summarize  # noqa: E402
from swarm.worker import RailViolation, WorkerClient  # noqa: E402

EXIT_OK, EXIT_FAILURES, EXIT_ABORTED, EXIT_RAIL = 0, 1, 2, 3


def _merged_prs() -> set[int]:
    """PR numbers already merged, for the registry staleness audit.

    Optional: without a token we cannot check, and we say so rather than assume
    nothing has merged — assuming would let a stale TEMPORARY entry keep
    swallowing regressions, which is the exact failure §4.6 warns about.
    """
    raw = os.environ.get("SWARM_MERGED_PRS", "").strip()
    if raw:
        return {int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()}
    return set()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one zero-bug program section against the worker.")
    ap.add_argument("--section", required=True, help="P0..P8, X1..X3")
    ap.add_argument("--run-id", required=True, help="teardown is keyed on this; make it unique per night")
    ap.add_argument("--personas", default="", help="comma-separated persona ids (default: all 9)")
    ap.add_argument("--arms", default="E-VOICE", help="comma-separated: E-VOICE,E-CLICK,E-FALLBACK")
    ap.add_argument("--fixtures", default=os.environ.get("SWARM_FIXTURES_DIR", "tests"))
    ap.add_argument("--max-workers", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="preflight and fixture load only; no writes")
    ap.add_argument("--out", default="", help="write the run report to this path as JSON")
    args = ap.parse_args()

    try:
        cfg = Config.from_env(args.run_id, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ABORTED

    fixtures = Path(args.fixtures)
    personas_path = fixtures / "personas.json"
    registry_path = fixtures / "KNOWN_DELTA_REGISTRY.md"
    for p in (personas_path, registry_path):
        if not p.exists():
            print(
                f"missing fixture: {p}\nThe fixtures live in the design repo "
                f"('[R&D] TagAlng/tests'). Point --fixtures or SWARM_FIXTURES_DIR at it.",
                file=sys.stderr,
            )
            return EXIT_ABORTED

    personas_doc = json.loads(personas_path.read_text(encoding="utf-8"))
    registry = Registry.load(registry_path)

    all_personas = personas_doc["personas"]
    wanted = {p.strip() for p in args.personas.split(",") if p.strip()}
    personas = [p for p in all_personas if not wanted or p["persona_id"] in wanted]
    if wanted and len(personas) != len(wanted):
        missing = wanted - {p["persona_id"] for p in personas}
        print(f"unknown persona id(s): {sorted(missing)}", file=sys.stderr)
        return EXIT_ABORTED
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    db = Db(cfg)
    auth = AnonymousAuth(cfg)

    with WorkerClient(cfg) as worker:
        pf = run_preflight(cfg, worker, db, registry, merged_prs=_merged_prs())

        print(f"\n=== preflight · run_id={cfg.run_id} · section={args.section} ===")
        for g in pf.gates:
            mark = {"ok": "  ok  ", "degrade": " degr ", "ABORT": "ABORT "}[g.status]
            print(f"[{mark}] {g.gate_id:<10} {g.detail}")
        if pf.active_deltas:
            print(f"\nactive deltas (assertions covered by these are blocked, not failed): "
                  f"{', '.join(pf.active_deltas)}")

        if pf.aborting:
            sys.stdout.flush()  # keep the gate table above the abort summary
            print("\nRUN ABORTED. Nothing was written.", file=sys.stderr)
            for g in pf.aborting:
                print(f"  {g.gate_id}: {g.detail}", file=sys.stderr)
            _dump(args.out, {"aborted": True, "preflight": pf.as_json()})
            return EXIT_ABORTED

        if args.dry_run:
            print(f"\ndry run: {len(personas)} persona(s) x {len(arms)} arm(s) would walk "
                  f"section {args.section}. No writes performed.")
            _dump(args.out, {"dry_run": True, "preflight": pf.as_json()})
            return EXIT_OK

        sr = SectionRunner(
            cfg, worker=worker, auth=auth, db=db, registry=registry, preflight=pf, personas_doc=personas_doc
        )
        try:
            walks = sr.run_section(args.section, personas=personas, arms=arms, max_workers=args.max_workers)
        except RailViolation as exc:
            print(f"\nHARD RAIL VIOLATED — run halted: {exc}", file=sys.stderr)
            _dump(args.out, {"rail_violation": str(exc), "preflight": pf.as_json()})
            return EXIT_RAIL

    report = summarize(walks)
    report["preflight"] = pf.as_json()
    report["run_id"] = cfg.run_id
    report["section"] = args.section

    print(f"\n=== {args.section} · run {cfg.run_id} ===")
    print(f"walks {report['walks']} · passed {report['passed']} · failed {report['failed']} "
          f"· blocked {report['blocked']} · error {report['errored']}")
    if report["mean_score"] is not None:
        print(f"mean score {report['mean_score']:.3f}")
    if report["delta_frequency"]:
        print("deltas: " + ", ".join(f"{k} x {v}" for k, v in report["delta_frequency"].items()))
    if report.get("suspect"):
        print(f"\n⚠ SUSPECT RUN: {report['suspect']}")
    print()
    for row in report["per_walk"]:
        s = "  n/a" if row["score"] is None else f"{row['score']:.2f}"
        print(f"  {row['section']} {row['persona']:<8} {row['arm']:<11} {row['verdict']:<24} "
              f"score={s}  p/f/b/e={row['p/f/b/e']}")

    print(f"\nTEARDOWN: select public.cleanup_swarm_run('{cfg.run_id}');")

    _dump(args.out, report)
    return EXIT_FAILURES if report["failed"] else EXIT_OK


def _dump(path: str, payload: dict) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"report written to {path}")


if __name__ == "__main__":
    raise SystemExit(main())
