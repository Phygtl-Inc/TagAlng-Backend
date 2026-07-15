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
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 output on Windows so Unicode in LLM responses doesn't crash prints
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Load .env.local from repo root so env vars don't need to be set manually in the shell
from dotenv import load_dotenv
load_dotenv(Path(__file__).parents[3] / ".env.local", override=True)

import simulation
import evaluation
import qa_analyze

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
    qa_transcripts: list[dict] = []  # QA-rubric buckets only — for qa_analyze
    qa_records_by_bucket: dict[str, list[tuple[dict, dict]]] = {}  # (transcript, result) pairs, for batch verdicts

    for i, (persona, bucket, seed) in enumerate(matrix, 1):
        print(f"\n[runner] {i}/{len(matrix)}")
        try:
            transcript = simulation.run(persona, bucket, seed)
            # score_qa() only handles the find_coverage/host_confirm/edge_trust-style QA
            # buckets and returns None otherwise, so this falls back to the original judge
            # for every existing bucket unchanged.
            result = evaluation.score_qa(transcript) or evaluation.score(transcript)
            results.append(result)
            if evaluation._qa_rubric_for_bucket(bucket.bucket) is not None:
                qa_transcripts.append(transcript)
                qa_records_by_bucket.setdefault(bucket.bucket, []).append((transcript, result))
        except Exception as exc:
            entry = {
                "persona_id": persona.id,
                "bucket": bucket.bucket,
                "seed_label": seed.label,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            failures.append(entry)
            print(f"  [runner] FAILED: {exc}")
            print(traceback.format_exc())

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

    if qa_transcripts:
        print(f"\n[runner] qa_analyze — {len(qa_transcripts)} find/host/edge-style transcript(s)")
        qa_stats = qa_analyze.analyze_transcripts(qa_transcripts)
        print(f"  verify_walls={qa_stats['verify_walls']}  zip_dead_ends={qa_stats['zip_dead_ends']}"
              f"  ny_bleed={qa_stats['ny_bleed']}  empty_replies={qa_stats['empty_replies']}")
        if qa_stats["lang_mismatch"]:
            print(f"  lang_mismatch: {[m['id'] for m in qa_stats['lang_mismatch']]}")
        if qa_stats["kid_name_echo"]:
            print(f"  kid_name_echo: {[m['id'] for m in qa_stats['kid_name_echo']]}")
        if qa_stats["flags"]:
            print(f"  flags: {[(f['id'], f['kind']) for f in qa_stats['flags']]}")
        lat = qa_stats["latency_summary"]
        if lat["n"]:
            print(f"  latency: p50={lat['p50']}ms p90={lat['p90']}ms max={lat['max']}ms")

    # Batch verdict per QA bucket — cross-conversation synthesis (systemic_issues,
    # best_moments, one overall verdict), same shape as qa/run1's original judges.
    qa_batch_verdicts: dict[str, dict] = {}
    for bucket_name, records in qa_records_by_bucket.items():
        verdict = evaluation.score_qa_batch(bucket_name, records)
        if verdict:
            qa_batch_verdicts[bucket_name] = verdict.model_dump()

    # Write a local run log to scratch/ for debugging (gitignored)
    _write_run_log(run_ts, results, failures, qa_batch_verdicts)


def _write_run_log(
    run_ts: str,
    results: list[dict],
    failures: list[dict],
    qa_batch_verdicts: dict[str, dict] | None = None,
) -> None:
    scratch_dir = Path(__file__).parent / "scratch"
    scratch_dir.mkdir(exist_ok=True)
    log_path = scratch_dir / f"run_{run_ts.replace(':', '-')}.json"
    log_path.write_text(
        json.dumps(
            {"results": results, "failures": failures, "qa_batch_verdicts": qa_batch_verdicts or {}},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\n[runner] run log → {log_path}")


if __name__ == "__main__":
    main()
