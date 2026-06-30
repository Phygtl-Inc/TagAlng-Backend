"""
runner.py
Entry point for the Lana simulation pipeline.

Full matrix run:
  python runner.py

Single persona, all seeds:
  python runner.py --persona P1

Single seed:
  python runner.py --persona P1 --bucket in_scope_success --seed "create meet happy path"

All seeds in a bucket across all personas:
  python runner.py --bucket out_of_scope_rejection

Dry run (load and validate data, print the matrix, don't call any API):
  python runner.py --dry-run
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import simulation
import evaluation

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Lana simulations.")
    parser.add_argument("--persona", help="Persona ID to run (e.g. P1). Default: all.")
    parser.add_argument("--bucket", help="Bucket name to run (e.g. in_scope_success). Default: all.")
    parser.add_argument("--seed", help="Seed label to run (e.g. 'create meet happy path'). Default: all.")
    parser.add_argument("--dry-run", action="store_true", help="Print the run matrix without calling any API.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Matrix builder
# ---------------------------------------------------------------------------

def _build_matrix(
    personas: list[simulation.Persona],
    buckets: list[simulation.Bucket],
    persona_filter: str | None,
    bucket_filter: str | None,
    seed_filter: str | None,
) -> list[tuple[simulation.Persona, simulation.Bucket, simulation.Seed]]:
    matrix = []
    for persona in personas:
        if persona_filter and persona.id.upper() != persona_filter.upper():
            continue
        for bucket in buckets:
            if bucket_filter and bucket.bucket != bucket_filter:
                continue
            for seed in bucket.seeds:
                if seed_filter and seed.label != seed_filter:
                    continue
                matrix.append((persona, bucket, seed))
    return matrix


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    personas = simulation.load_personas()
    buckets = simulation.load_buckets()

    matrix = _build_matrix(
        personas, buckets,
        args.persona, args.bucket, args.seed,
    )

    if not matrix:
        print("No runs matched the filters. Check --persona / --bucket / --seed values.")
        print(f"  Valid persona IDs: {[p.id for p in personas]}")
        print(f"  Valid buckets: {[b.bucket for b in buckets]}")
        sys.exit(1)

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[runner] {run_ts} — {len(matrix)} runs queued")
    for p, b, s in matrix:
        print(f"  {p.id} × {b.bucket}/{s.label}")

    if args.dry_run:
        print("\n[runner] dry-run — exiting without calling any API")
        return

    results: list[dict] = []
    failures: list[dict] = []

    for i, (persona, bucket, seed) in enumerate(matrix, 1):
        print(f"\n[runner] {i}/{len(matrix)}")
        try:
            transcript = simulation.run(persona, bucket, seed)
            result = evaluation.score(transcript)
            results.append(result)
        except Exception as exc:
            entry = {
                "persona_id": persona.id,
                "bucket": bucket.bucket,
                "seed_label": seed.label,
                "error": str(exc),
            }
            failures.append(entry)
            print(f"  [runner] FAILED: {exc}")

    # --- Summary ---
    print(f"\n[runner] complete — {len(results)} passed, {len(failures)} failed")

    if results:
        scores = [r["weighted_score"] for r in results]
        print(f"  weighted scores: min={min(scores):.3f}  avg={sum(scores)/len(scores):.3f}  max={max(scores):.3f}")

        hard_fails = [
            r for r in results
            if any(a["verdict"] == "HARD_FAIL" for a in r["scores_json"])
        ]
        if hard_fails:
            print(f"  HARD FAILs ({len(hard_fails)}):")
            for r in hard_fails:
                axes = [a["axis"] for a in r["scores_json"] if a["verdict"] == "HARD_FAIL"]
                print(f"    {r['persona_id']} × {r['bucket']}/{r['seed_label']} — {axes}")

    if failures:
        print(f"\n  Errored runs ({len(failures)}):")
        for f in failures:
            print(f"    {f['persona_id']} × {f['bucket']}/{f['seed_label']}: {f['error']}")

    # Write a local run log to scratch/ for debugging (gitignored)
    _write_run_log(run_ts, results, failures)


def _write_run_log(run_ts: str, results: list[dict], failures: list[dict]) -> None:
    scratch_dir = Path(__file__).parent / "scratch"
    scratch_dir.mkdir(exist_ok=True)
    log_path = scratch_dir / f"run_{run_ts.replace(':', '-')}.json"
    log_path.write_text(
        json.dumps({"results": results, "failures": failures}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n[runner] run log → {log_path}")


if __name__ == "__main__":
    main()
