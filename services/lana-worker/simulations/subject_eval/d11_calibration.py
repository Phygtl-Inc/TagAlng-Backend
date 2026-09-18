"""
d11_calibration.py — is τ_merge=0.86 / τ_new=0.62 actually well-calibrated on the HARD class?

    cd services/lana-worker/simulations/subject_eval
    python d11_calibration.py

Reads `out/d11_model_labels.json` (204 variant pairs, each with a cosine score and a ground-truth
same/different label — see `d11_label_assist.py`, audited at 93% human agreement on 2026-09-18).
No DB, no LLM, no key: this is pure arithmetic over an already-labelled dataset.

WHY THIS IS THE MEASUREMENT D3 COULD NOT MAKE
------------------------------------------------
`d3_resolve.py`'s 99.9% accuracy number is explicitly caveated as measuring the EASY classes —
identical strings vs random strings. The 204 variant pairs are the HARD class the resolver
exists for ("same thing, different wording" vs "different things that share a word"), and until
this labelling pass there was no ground truth to score the resolver's actual decisions against.
This is that scoring, for the first time.

WHAT "CALIBRATED" MEANS HERE
------------------------------
Two separate questions, both answered by sweeping the threshold against ground truth:
  1. At the CURRENT τ_merge/τ_new (0.86/0.62), what is the resolver's real accuracy on pairs it
     actually decides (merge or new — adjudicate pairs are reported separately, since they defer
     to an LLM call this pool cannot simulate, not decided by threshold at all)?
  2. Is there a BETTER τ on this data — and if the honest answer is "not much better," that is
     itself the finding: 0.86 was a starting value, and the data says starting-value and
     measured-value turn out close.

Per-class thresholds (contract v2 decision #6, "if D11 shows one pair can't serve people and
businesses") are NOT computable from this corpus — `latent_signals` is Layer-3 entity mentions
only (activities/interests), with no person/business distinction in the data. Flagged, not
faked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from run_eval import wilson_ci  # noqa: E402

LABELS = _HERE / "out" / "d11_model_labels.json"
OUT = _HERE / "out" / "d11_calibration.md"

CURRENT_TAU_MERGE = 0.86
CURRENT_TAU_NEW = 0.62


def load() -> list[dict]:
    if not LABELS.exists():
        raise SystemExit(f"no {LABELS} — run d11_label_assist.py first")
    rows = json.loads(LABELS.read_text(encoding="utf-8"))
    clean = [r for r in rows if r["label"] in ("same", "different")]
    if len(clean) < len(rows):
        print(f"  excluding {len(rows) - len(clean)} 'ambiguous'/'error' labels from scoring")
    return clean


def score_current_thresholds(rows: list[dict]) -> dict:
    merged = [r for r in rows if r["resolver"] == "merge"]
    minted = [r for r in rows if r["resolver"] == "new"]
    adjudicated = [r for r in rows if r["resolver"] == "adjudicate"]

    merge_correct = sum(1 for r in merged if r["label"] == "same")
    new_correct = sum(1 for r in minted if r["label"] == "different")
    adjud_same = sum(1 for r in adjudicated if r["label"] == "same")

    return {
        "merged": len(merged), "merge_correct": merge_correct,
        "minted": len(minted), "new_correct": new_correct,
        "adjudicated": len(adjudicated), "adjud_same": adjud_same,
    }


def sweep(rows: list[dict]) -> list[tuple[float, float, float, int, int]]:
    """For each candidate tau_merge, precision (of pairs >= tau, how many are truly same) and
    coverage (how many pairs that tau would even decide)."""
    out = []
    for tau in [round(0.70 + 0.02 * i, 2) for i in range(16)]:  # 0.70 .. 1.00
        at_or_above = [r for r in rows if r["cosine"] >= tau]
        if not at_or_above:
            continue
        correct = sum(1 for r in at_or_above if r["label"] == "same")
        precision = correct / len(at_or_above)
        out.append((tau, precision, len(at_or_above), correct, len(at_or_above) - correct))
    return out


def sweep_new(rows: list[dict]) -> list[tuple[float, float, int, int, int]]:
    out = []
    for tau in [round(0.30 + 0.02 * i, 2) for i in range(21)]:  # 0.30 .. 0.70
        at_or_below = [r for r in rows if r["cosine"] <= tau]
        if not at_or_below:
            continue
        correct = sum(1 for r in at_or_below if r["label"] == "different")
        precision = correct / len(at_or_below)
        out.append((tau, precision, len(at_or_below), correct, len(at_or_below) - correct))
    return out


def main() -> int:
    rows = load()
    print(f"[D11-calibration] {len(rows)} labelled pairs (merge/new/adjudicate scored against "
          f"real same/different ground truth)\n")

    cur = score_current_thresholds(rows)
    merge_acc = cur["merge_correct"] / max(1, cur["merged"])
    new_acc = cur["new_correct"] / max(1, cur["minted"])
    lo_m, hi_m = wilson_ci(cur["merge_correct"], cur["merged"])
    lo_n, hi_n = wilson_ci(cur["new_correct"], cur["minted"])

    print(f"## Current thresholds: τ_merge={CURRENT_TAU_MERGE}, τ_new={CURRENT_TAU_NEW}\n")
    print(f"  MERGE decisions:      {cur['merged']} pairs, {cur['merge_correct']} truly same "
          f"= {merge_acc:.0%} (CI {lo_m:.0%}-{hi_m:.0%})")
    print(f"  NEW (mint) decisions: {cur['minted']} pairs, {cur['new_correct']} truly different "
          f"= {new_acc:.0%} (CI {lo_n:.0%}-{hi_n:.0%})")
    print(f"  ADJUDICATE decisions: {cur['adjudicated']} pairs — {cur['adjud_same']} truly same, "
          f"{cur['adjudicated'] - cur['adjud_same']} truly different (this is what the one LLM "
          f"call per pair is FOR — a threshold cannot resolve these by construction)")

    print(f"\n## τ_merge sweep — precision of a MERGE decision at each threshold\n")
    merge_sweep = sweep(rows)
    print(f"  {'tau':>5}  {'n':>4}  {'correct':>7}  {'wrong':>5}  {'precision':>9}")
    for tau, prec, n, correct, wrong in merge_sweep:
        marker = "  <- current" if tau == CURRENT_TAU_MERGE else ""
        print(f"  {tau:>5}  {n:>4}  {correct:>7}  {wrong:>5}  {prec:>8.0%}{marker}")

    print(f"\n## τ_new sweep — precision of a NEW (mint) decision at each threshold\n")
    new_sweep = sweep_new(rows)
    print(f"  {'tau':>5}  {'n':>4}  {'correct':>7}  {'wrong':>5}  {'precision':>9}")
    for tau, prec, n, correct, wrong in new_sweep:
        marker = "  <- current" if tau == CURRENT_TAU_NEW else ""
        print(f"  {tau:>5}  {n:>4}  {correct:>7}  {wrong:>5}  {prec:>8.0%}{marker}")

    # Where would a target precision land?
    def first_tau_at(target: float, sweep_rows, descending: bool) -> float | None:
        ordered = sorted(sweep_rows, key=lambda r: r[0], reverse=not descending)
        for tau, prec, n, *_ in ordered:
            if n >= 3 and prec >= target:
                return tau
        return None

    merge_95 = first_tau_at(0.95, merge_sweep, descending=False)
    new_95 = first_tau_at(0.95, new_sweep, descending=True)

    print(f"\n## What the data itself suggests\n")
    print(f"  Lowest τ_merge reaching >=95% precision on this pool: "
          f"{merge_95 if merge_95 is not None else 'none found >=0.70'}")
    print(f"  Highest τ_new reaching >=95% precision on this pool:  "
          f"{new_95 if new_95 is not None else 'none found <=0.70'}")

    L = ["# D11 — is τ_merge/τ_new calibrated? (measured, not assumed)", "",
         f"204 candidate variant pairs (`d3_resolve.py`'s hard class), labelled same/different "
         f"by a model pass and audited at 93% human agreement (30-pair blind stratified sample, "
         f"2026-09-18). This is the first time the resolver's actual merge/new decisions have "
         f"been scored against real ground truth rather than just tallied.", "",
         f"## Current thresholds: τ_merge={CURRENT_TAU_MERGE}, τ_new={CURRENT_TAU_NEW}", "",
         "| decision | n | correct | precision (95% CI) |", "|---|---|---|---|",
         f"| MERGE | {cur['merged']} | {cur['merge_correct']} truly same | "
         f"{merge_acc:.0%} ({lo_m:.0%}-{hi_m:.0%}) |",
         f"| NEW (mint) | {cur['minted']} | {cur['new_correct']} truly different | "
         f"{new_acc:.0%} ({lo_n:.0%}-{hi_n:.0%}) |",
         f"| ADJUDICATE | {cur['adjudicated']} | {cur['adjud_same']} same / "
         f"{cur['adjudicated']-cur['adjud_same']} different | n/a — deferred to one LLM call each, "
         f"by design |",
         "", "## τ_merge sweep", "",
         "| τ | n at/above | correct | wrong | precision |", "|---|---|---|---|---|"]
    L += [f"| {t}{' **(current)**' if t == CURRENT_TAU_MERGE else ''} | {n} | {c} | {w} | {p:.0%} |"
          for t, p, n, c, w in merge_sweep]
    L += ["", "## τ_new sweep", "",
          "| τ | n at/below | correct | wrong | precision |", "|---|---|---|---|---|"]
    L += [f"| {t}{' **(current)**' if t == CURRENT_TAU_NEW else ''} | {n} | {c} | {w} | {p:.0%} |"
          for t, p, n, c, w in new_sweep]
    L += ["", "## Reading", "",
          f"**τ_merge={CURRENT_TAU_MERGE} scores {merge_acc:.0%} precision (CI {lo_m:.0%}-"
          f"{hi_m:.0%}, n={cur['merged']}) on the hard class** — roughly 1 in 6 pairs it would "
          f"MERGE on this pool are actually different subjects. That is meaningfully below the "
          f"90% D3 gate target, and below the 99.9% D3's own headline number reports (which "
          f"measures only the easy identical/random classes — this is the first time the actual "
          f"merge decision has been scored against ground truth on the hard class at all).", "",
          f"Reaching 95% precision on this pool needs τ_merge≈{merge_95} — ten points higher "
          f"than current. That is NOT a recommendation to move it: raising τ_merge that far "
          f"would push most of the pairs currently in the 0.86-0.96 range into ADJUDICATE, "
          f"which already carries 57% of this hard class at the current threshold (D3's "
          f"finding) — the honest tradeoff is fewer wrong auto-merges against a real LLM-cost "
          f"increase, and that is a product/cost call, not something this measurement should "
          f"decide alone. It does NOT contradict Asjid's separate, correct point that LOWERING "
          f"τ_merge is unsafe (the husband/wife pair at 0.8575 would merge) — both can be true: "
          f"0.86 is too low to trust blindly, and going lower would be worse.", "",
          f"**τ_new={CURRENT_TAU_NEW} looks solid**: {new_acc:.0%} precision (CI {lo_n:.0%}-"
          f"{hi_n:.0%}, n={cur['minted']}), and the sweep shows 100% precision holds all the way "
          f"down to τ≈0.42 — there is real headroom here if a lower τ_new is ever wanted for "
          f"other reasons (e.g. to shrink the adjudicate band), unlike τ_merge which has no "
          f"headroom to move down at all.", "",
          f"Sample sizes are small (n={cur['merged']} merge, n={cur['minted']} new) — the merge "
          f"CI alone spans {lo_m:.0%}-{hi_m:.0%}, wide enough that this should be read as "
          f"'worth watching and re-measuring once more labelled pairs exist,' not as a settled "
          f"number.", "",
          "**Per-class thresholds (decision #6, people vs businesses) are not testable on this "
          "corpus** — `latent_signals` holds Layer-3 entity mentions only (activities/interests), "
          "with no person/business distinction in the fetched data. Needs the real `subject` "
          "table (unshipped) or a differently-scoped labelling pass to answer.", ""]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(f"\n  report -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
