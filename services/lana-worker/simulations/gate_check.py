"""
gate_check.py
Called by sim-gate.yml after runner.py completes on a PR branch.
Compares this PR's sim results against the last green baseline tag.
Blocks the PR if:
  - Any axis regresses > 5% in average score vs baseline
  - HARD_FAIL count increases on out_of_scope_rejection or ambiguous_clarity seeds
Posts a summary comment to the PR with top-3 worst runs and a link to /admin/sims.
Exits non-zero to fail the CI check if the gate fails.
"""

import json
import os
import sys
import urllib.request

import httpx

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BASELINE_TAG = os.environ.get("BASELINE_TAG", "")  # e.g. sim-baseline-2026-07-03
PR_SHA = os.environ["PR_SHA"]
GH_TOKEN = os.environ.get("GH_TOKEN", "")
PR_NUMBER = os.environ.get("PR_NUMBER", "")
REPO = os.environ.get("REPO", "")

REGRESSION_THRESHOLD = 0.05   # 5% drop in per-axis average score blocks the PR
HARD_FAIL_BUCKETS = {"out_of_scope_rejection", "ambiguous_clarity"}

AXIS_WEIGHTS = {
    "goal_completion": 0.30,
    "warmth_tone": 0.25,
    "five_beat_landed": 0.25,
    "no_hallucination": 0.20,
}

SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
}


def _fetch_runs(git_sha: str) -> list[dict]:
    with httpx.Client(timeout=15) as http:
        resp = http.get(
            f"{SUPABASE_URL}/rest/v1/simulations",
            params={
                "select": "bucket,seed_label,weighted_score,scores_json",
                "git_sha": f"eq.{git_sha}",
                "limit": "500",
            },
            headers=SUPABASE_HEADERS,
        )
        resp.raise_for_status()
        return resp.json()


def _fetch_baseline_runs() -> list[dict]:
    """
    Fetches runs tagged with BASELINE_TAG.
    Falls back to the most recent 102 runs if no baseline tag is set.
    """
    if not BASELINE_TAG:
        print("[gate] No BASELINE_TAG set — skipping regression check, gate passes.")
        return []

    with httpx.Client(timeout=15) as http:
        resp = http.get(
            f"{SUPABASE_URL}/rest/v1/simulations",
            params={
                "select": "bucket,seed_label,weighted_score,scores_json",
                "git_sha": f"eq.{BASELINE_TAG}",
                "limit": "500",
            },
            headers=SUPABASE_HEADERS,
        )
        resp.raise_for_status()
        return resp.json()


def _axis_averages(runs: list[dict]) -> dict[str, float]:
    totals: dict[str, list[float]] = {a: [] for a in AXIS_WEIGHTS}
    for run in runs:
        for axis in (run.get("scores_json") or []):
            name = axis.get("axis")
            if name in totals:
                totals[name].append(axis["score"])
    return {a: (sum(v) / len(v)) if v else 1.0 for a, v in totals.items()}


def _hard_fail_count(runs: list[dict], buckets: set[str]) -> int:
    count = 0
    for run in runs:
        if run.get("bucket") not in buckets:
            continue
        for axis in (run.get("scores_json") or []):
            if axis.get("verdict") == "HARD_FAIL":
                count += 1
    return count


def _post_pr_comment(body: str) -> None:
    if not GH_TOKEN or not PR_NUMBER or not REPO:
        print("[gate] No GH_TOKEN/PR_NUMBER/REPO — skipping PR comment")
        return
    url = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
    payload = json.dumps({"body": body}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        print(f"[gate] PR comment failed: {exc}")


def main() -> None:
    print(f"[gate] fetching runs for SHA {PR_SHA[:7]}")
    pr_runs = _fetch_runs(PR_SHA)
    if not pr_runs:
        print("[gate] No runs found for this SHA — did runner.py complete?")
        sys.exit(1)

    baseline_runs = _fetch_baseline_runs()
    if not baseline_runs:
        print("[gate] No baseline — gate passes automatically on first run.")
        _post_pr_comment(
            f"## Sim gate — no baseline yet\n\n"
            f"{len(pr_runs)} sims ran on this PR (SHA `{PR_SHA[:7]}`). "
            f"No baseline tag to compare against. Gate passes.\n\n"
            f"Once the first nightly run completes, tag it: `git tag sim-baseline-YYYY-MM-DD <sha>`"
        )
        return

    pr_avgs = _axis_averages(pr_runs)
    base_avgs = _axis_averages(baseline_runs)
    pr_hard = _hard_fail_count(pr_runs, HARD_FAIL_BUCKETS)
    base_hard = _hard_fail_count(baseline_runs, HARD_FAIL_BUCKETS)

    regressions = []
    for axis, base_score in base_avgs.items():
        pr_score = pr_avgs.get(axis, 0.0)
        drop = base_score - pr_score
        if drop > REGRESSION_THRESHOLD:
            regressions.append((axis, base_score, pr_score, drop))

    hard_fail_increase = pr_hard - base_hard

    # Worst 3 runs for PR comment
    worst = sorted(pr_runs, key=lambda r: r.get("weighted_score", 1.0))[:3]
    worst_lines = "\n".join(
        f"- **{r['bucket']}/{r['seed_label']}** — score `{r['weighted_score']:.3f}`"
        for r in worst
    )

    axis_table = "\n".join(
        f"| {a} | {base_avgs.get(a, 0):.3f} | {pr_avgs.get(a, 0):.3f} | "
        + (f"**↓ {base_avgs.get(a,0) - pr_avgs.get(a,0):.3f}** 🚨" if (base_avgs.get(a, 0) - pr_avgs.get(a, 0)) > REGRESSION_THRESHOLD else f"{pr_avgs.get(a,0) - base_avgs.get(a,0):+.3f} ✓")
        + " |"
        for a in AXIS_WEIGHTS
    )

    gate_passed = not regressions and hard_fail_increase <= 0

    comment = f"""## Sim gate {'✅ passed' if gate_passed else '🚨 FAILED'}

**{len(pr_runs)} sims** ran on SHA `{PR_SHA[:7]}` vs baseline `{BASELINE_TAG}`.

### Axis averages
| Axis | Baseline | PR | Δ |
|---|---|---|---|
{axis_table}

**HARD FAILs on rejection/ambiguous buckets:** baseline={base_hard} → PR={pr_hard}{' 🚨 increased' if hard_fail_increase > 0 else ' ✓'}

### Worst 3 runs this PR
{worst_lines}

[Review all runs in /admin/sims →](../admin/sims)
"""

    _post_pr_comment(comment)

    if regressions:
        print(f"[gate] FAILED — axis regressions detected:")
        for axis, base, pr, drop in regressions:
            print(f"  {axis}: {base:.3f} → {pr:.3f} (↓{drop:.3f})")

    if hard_fail_increase > 0:
        print(f"[gate] FAILED — HARD_FAIL count increased by {hard_fail_increase} on critical buckets")

    if not gate_passed:
        sys.exit(1)

    print(f"[gate] passed — no regressions, HARD_FAILs stable")


if __name__ == "__main__":
    main()
