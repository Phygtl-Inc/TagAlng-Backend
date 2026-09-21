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
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
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
    parser.add_argument(
        "--pr", action="store_true",
        help="PR-gate mode: only the must-have SEEDS (per-seed `pr_gate` in scenarios.json) "
             "across the PR persona subset — safety/refusals, PII/privacy, core function, "
             "plus the one hallucination-catching seed. Everything else runs in the nightly. "
             "sim-gate.yml uses this; sim-nightly.yml runs the full matrix.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Parallel persona workers. Default: one per distinct persona in the matrix (≤6). "
             "Use 1 to force the old fully-sequential behavior. Parallelism is ACROSS personas "
             "only — a persona's own seeds always run sequentially (claims-seeding is per-user "
             "and would race otherwise).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Matrix builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PR-gate slimming
# ---------------------------------------------------------------------------
# The full matrix is 6 personas x 32 seeds = 192 runs (~472 turns measured). Every turn is an
# LLM call for the mock user AND for Lana, and every run is 1-2 judge calls on top, so the PR
# gate was the most expensive thing in CI by a wide margin.
#
# WHAT WAS CUT, AND ON WHAT BASIS. Seeds were ranked by defect yield (HARD_FAILs found per run)
# against turn cost, measured over run_2026-08-14T14-45-21Z. But yield alone is the WRONG
# criterion on its own: the privacy/PII/safety seeds found zero defects precisely because they
# are the ones that must never regress. Cutting a smoke detector because there is no fire is how
# a launch-blocking bug ships. So the rule is:
#
#   KEEP on PR  — anything whose failure is severe and hard to reverse (privacy, PII leak,
#                 safety refusals), regardless of current yield; plus the highest-yield
#                 defect-catchers.
#   NIGHTLY     — quality/nuance seeds with low yield and high turn cost.
#
# One seed is kept purely for coverage: ambiguous_clarity/"host a meeting ambiguous" is the ONLY
# seed in the suite where no_hallucination has ever fired (invented venues, reproduced on 2
# personas). Dropping the bucket wholesale would have removed the PR gate's only hallucination
# signal, which is why the cut is per-SEED, not per-bucket.
#
# Cutting seeds rather than buckets also keeps gate_check.py's baseline bucket matching intact —
# a bucket-level cut would have made every PR incomparable to its baseline.
#
# Measured effect: 192 -> 33 runs, ~472 -> ~150 turns (roughly a 68% cut).

# Three personas for the PR gate, chosen for CONTRAST rather than for past failure counts
# (picking the personas that failed most would overfit the gate to defects we already know):
#   P1 — established/dense-area happy path
#   P5 — the only bilingual persona; language handling is where the judge's one confirmed
#        false positive appeared (unprompted ES switch), so it earns a PR slot
#   P6 — new-to-app skeptic: the cold-start / empty-area path, and the most defect-dense persona
# The nightly still runs all six.
PR_PERSONAS = {"P1", "P5", "P6"}


def _pr_seed_allowlist() -> dict[str, set[str]]:
    """{bucket: {seed_label, ...}} for seeds flagged `"pr_gate": true` in scenarios.json.

    Read straight from the JSON rather than off the parsed Seed model: the pydantic model
    ignores unknown keys, so the flag would be silently dropped. A bucket absent from this map
    has no per-seed flags and keeps all of its seeds.
    """
    raw = json.loads(simulation.SCENARIOS_PATH.read_text(encoding="utf-8"))
    allow: dict[str, set[str]] = {}
    for b in raw.get("buckets", []):
        labels = {s["label"] for s in b.get("seeds", []) if s.get("pr_gate")}
        if labels:
            allow[b["bucket"]] = labels
    return allow


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

    if args.pr:
        before = len(matrix)
        allow = _pr_seed_allowlist()
        matrix = [
            (p, b, s) for (p, b, s) in matrix
            if b.pr_gate
            # A pr_gate bucket with NO per-seed flags keeps all its seeds — so a newly added
            # bucket is gated by default rather than silently skipped.
            and (b.bucket not in allow or s.label in allow[b.bucket])
            and (args.persona or p.id in PR_PERSONAS)
        ]
        print(f"[runner] --pr: {len(matrix)} cases (from {before}) — "
              f"{len(PR_PERSONAS)} personas × the must-have seeds. "
              f"Everything else runs in the nightly.")

    if not matrix:
        print("No runs matched the filters. Check --persona / --bucket / --seed values.")
        print(f"  Valid persona IDs: {[p.id for p in personas]}")
        print(f"  Valid buckets: {[b.bucket for b in buckets]}")
        sys.exit(1)

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[runner] {run_ts} — {len(matrix)} runs queued")
    for p, b, s in matrix:
        print(f"  {p.id} × {b.bucket}/{s.label}")

    # Group the matrix by persona. Parallelism is ACROSS personas only: a persona's own seeds
    # run sequentially in one worker, because _seed_claims() does a DELETE+reseed on that
    # persona's user_identity_claims at the start of every run — two concurrent same-persona runs
    # would corrupt each other's context. Different personas are different accounts/rows, so they
    # never collide.
    groups: dict[str, list[tuple]] = {}
    for persona, bucket, seed in matrix:
        groups.setdefault(persona.id, []).append((persona, bucket, seed))

    concurrency = args.concurrency if args.concurrency and args.concurrency > 0 else len(groups)
    concurrency = max(1, min(concurrency, len(groups)))

    if args.dry_run:
        print(f"\n[runner] dry-run — {len(groups)} persona group(s), would run "
              f"{concurrency}-way parallel across personas (each persona's seeds sequential)")
        for pid, cases in groups.items():
            print(f"  {pid}: {len(cases)} case(s)")
        print("[runner] dry-run — exiting without calling any API")
        return

    print(f"[runner] running {concurrency}-way parallel across {len(groups)} persona(s) "
          f"(each persona's seeds sequential)")
    _print_lock = threading.Lock()

    def _run_group(cases: list[tuple]) -> tuple[list[dict], list[dict], dict[str, list[tuple[dict, dict]]]]:
        """Process one persona's seeds sequentially. Returns local (results, failures, qa_records)
        so nothing mutable is shared across threads — merged in the main thread afterwards."""
        g_results: list[dict] = []
        g_failures: list[dict] = []
        g_qa: dict[str, list[tuple[dict, dict]]] = {}
        for persona, bucket, seed in cases:
            try:
                transcript = simulation.run(persona, bucket, seed)
                # HARNESS-INVALID runs are never scored. The mock user went out of character and
                # stayed there after a retry, so the conversation is partly the simulator talking to
                # itself. Scoring it would fold harness noise into Lana's failure rate and could make
                # a corrupted transcript SFT-eligible. Reported as a failure so it stays visible —
                # a silently dropped run is indistinguishable from one that passed.
                if not transcript.get("harness_valid", True):
                    g_failures.append({
                        "persona_id": persona.id, "bucket": bucket.bucket, "seed_label": seed.label,
                        "error": f"HARNESS_INVALID: {transcript.get('invalid_reason')}",
                        "traceback": "",
                    })
                    with _print_lock:
                        print(f"  [runner] INVALID (not scored): {persona.id} × "
                              f"{bucket.bucket}/{seed.label} — {transcript.get('invalid_reason')}")
                    continue
                # score_qa() only handles the find/host/edge-style QA buckets and returns None
                # otherwise, so this falls back to the original judge for every other bucket.
                result = evaluation.score_qa(transcript) or evaluation.score(transcript)
                g_results.append(result)
                if evaluation._qa_rubric_for_bucket(bucket.bucket) is not None:
                    g_qa.setdefault(bucket.bucket, []).append((transcript, result))
            except Exception as exc:
                g_failures.append({
                    "persona_id": persona.id, "bucket": bucket.bucket, "seed_label": seed.label,
                    "error": str(exc), "traceback": traceback.format_exc(),
                })
                with _print_lock:
                    print(f"  [runner] FAILED: {persona.id} × {bucket.bucket}/{seed.label}: {exc}")
                    print(traceback.format_exc())
        return g_results, g_failures, g_qa

    results: list[dict] = []
    failures: list[dict] = []
    qa_transcripts: list[dict] = []  # QA-rubric buckets only — for qa_analyze
    qa_records_by_bucket: dict[str, list[tuple[dict, dict]]] = {}  # (transcript, result) pairs

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for g_results, g_failures, g_qa in pool.map(_run_group, groups.values()):
            results.extend(g_results)
            failures.extend(g_failures)
            for bname, recs in g_qa.items():
                qa_records_by_bucket.setdefault(bname, []).extend(recs)
                qa_transcripts.extend(t for t, _ in recs)

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
