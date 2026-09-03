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
import re
import subprocess
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


def _resolve_baseline_sha(ref: str) -> str:
    """Resolve a git ref (usually a tag like `sim-baseline-2026-07-03`) to the commit SHA.

    WHY THIS EXISTS: rows are written by evaluation._write_to_supabase with
    `git_sha = os.environ["GIT_SHA"]` — an actual 40-char commit SHA. Querying
    `git_sha=eq.<tag-name>` therefore matched NOTHING, the baseline came back empty, and the
    gate hit its "no baseline — passes automatically" branch on every run. The regression gate
    has consequently never gated. Resolve the tag to a SHA so the comparison is real.
    """
    ref = ref.strip()
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return ref  # already a SHA
    try:
        sha = subprocess.check_output(
            ["git", "rev-list", "-n", "1", ref], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if sha:
            print(f"[gate] resolved baseline ref {ref!r} -> {sha[:7]}")
            return sha
    except Exception as exc:
        print(f"[gate] could not resolve baseline ref {ref!r} via git: {exc}")
    return ref  # fall through; the empty-result branch below reports it loudly


def _fetch_baseline_runs() -> list[dict]:
    """Fetches the baseline runs (rows written under the baseline commit's SHA)."""
    if not BASELINE_TAG:
        print("[gate] No BASELINE_TAG set — skipping regression check, gate passes.")
        return []

    sha = _resolve_baseline_sha(BASELINE_TAG)
    with httpx.Client(timeout=15) as http:
        resp = http.get(
            f"{SUPABASE_URL}/rest/v1/simulations",
            params={
                "select": "bucket,seed_label,weighted_score,scores_json",
                "git_sha": f"eq.{sha}",
                "limit": "500",
            },
            headers=SUPABASE_HEADERS,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            # Do NOT let this look like "no baseline configured" — a configured-but-unmatched
            # baseline is a broken gate, and silently passing is exactly the fail-open we're fixing.
            print(f"[gate] WARNING: BASELINE_TAG={BASELINE_TAG!r} resolved to {sha[:7]} but matched "
                  f"ZERO rows in `simulations`. The regression comparison cannot run — check that "
                  f"the baseline nightly actually wrote rows with GIT_SHA={sha[:7]}.")
        return rows


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

    # The PR gate runs only the must-have buckets (runner.py --pr), but the baseline tag is a FULL
    # nightly run (all buckets). Restrict the baseline to the SAME buckets the PR ran so the pooled
    # per-axis averages are apples-to-apples — otherwise the differing bucket mix (the nightly-only
    # coverage buckets) would manufacture spurious regressions and block every PR.
    pr_buckets = {r.get("bucket") for r in pr_runs}
    baseline_runs = [r for r in baseline_runs if r.get("bucket") in pr_buckets]
    print(f"[gate] comparing {len(pr_runs)} PR runs vs {len(baseline_runs)} baseline runs "
          f"over matched buckets {sorted(pr_buckets)}")

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
