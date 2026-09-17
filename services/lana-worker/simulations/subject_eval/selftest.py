"""
selftest.py — proves the D5 harness measures something.

    cd services/lana-worker/simulations/subject_eval
    python selftest.py

Offline, deterministic, no key. Same discipline as the rest of the suite: every metric is fed
an input whose answer is known by construction, and the degenerate cases are asserted to fail
rather than to quietly return a number.

The specific risk here is different from the other harnesses. There is no product to be unfair
to — the selector is our own reference implementation — so the danger is not a false finding
about Lana, it is a metric that looks meaningful and is not. Two ways that happens:

  * the selector is non-deterministic, so the sweep measures noise;
  * `reached` and `correct` move together by construction, so `confidently_wrong` can never fire
    and the headline caveat is decorative.

Both are asserted against below.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import population  # noqa: E402
import run_eval  # noqa: E402
from elicit import NoElicit, ReferenceElicit  # noqa: E402
from ports import MISSING, SubjectMatrix  # noqa: E402

_PASSED = 0
_FAILED: list[str] = []


def expect(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
    else:
        _FAILED.append(msg)
        print(f"  FAIL: {msg}")


def section(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 64 - len(t)))


print("=" * 70)
print("subject_eval selftest — D5 elicitation")
print("=" * 70)

section("1. the problem model's own ground truth is right")
m = SubjectMatrix(
    id="hand", candidates=["A", "B"], predicates=["p", "q"],
    scores=[[1.0, 0.0], [0.0, 1.0]],
    true_weights=[0.9, 0.1],
)
expect(m.true_best() == 0, "true_best picked the wrong candidate on a hand-built matrix")
expect(abs(m.separation() - 0.8) < 1e-9, f"separation should be 0.8, got {m.separation()}")
m2 = SubjectMatrix(id="tie", candidates=["A", "B"], predicates=["p", "q"],
                   scores=[[0.5, 0.5], [0.5, 0.5]], true_weights=[0.5, 0.5])
expect(m2.separation() == 0.0, "two identical candidates must have zero separation")

section("2. the selector is deterministic — or the sweep measures noise")
probs = population.sweep(n_per_cell=2)
a = run_eval.run(probs, particles=200, sharpness=6.0)
b = run_eval.run(probs, particles=200, sharpness=6.0)
expect([r.result.winner for r in a] == [r.result.winner for r in b],
       "same problems gave different winners on a re-run — the selector is not seeded")
expect([r.result.confidence for r in a] == [r.result.confidence for r in b],
       "confidences moved between identical runs")
expect(population.make_problem(7).scores == population.make_problem(7).scores,
       "the problem generator is not deterministic")

section("3. the 2-question cap is never exceeded")
expect(all(r.result.n_questions <= 2 for r in a),
       "the selector asked more than the hard cap of 2")
sel3 = ReferenceElicit(particles=200, cap=3)
r3 = sel3.run(population.make_problem(1, separation="mixed"))
expect(r3.n_questions <= 3, "cap argument not honoured")

section("4. degenerate inputs error rather than returning a number")
one_cand = SubjectMatrix(id="one", candidates=["A"], predicates=["p", "q"],
                         scores=[[0.9, 0.1]], true_weights=[0.5, 0.5])
expect(ReferenceElicit(particles=50).run(one_cand).error is not None,
       "a single candidate should error, not silently 'win'")
one_pred = SubjectMatrix(id="onep", candidates=["A", "B"], predicates=["p"],
                         scores=[[0.9], [0.1]], true_weights=[1.0])
expect(ReferenceElicit(particles=50).run(one_pred).error is not None,
       "a single predicate leaves no pair to ask about and should error")

section("5. an all-blank matrix must never reach the bar")
# Every cell MISSING means the selector knows nothing about any candidate. Reaching 0.85
# confidence there would mean the belief is being driven by the prior, not by evidence.
blank = SubjectMatrix(
    id="blank", candidates=["A", "B", "C"], predicates=["p", "q", "r"],
    scores=[[MISSING] * 3 for _ in range(3)], true_weights=[0.5, 0.3, 0.2],
)
rb = ReferenceElicit(particles=300).run(blank)
expect(not rb.reached,
       f"reached {rb.confidence} confidence on a matrix with no information in it")

section("6. `confidently_wrong` can actually fire")
# If reached and correct moved together by construction, the headline caveat would be
# decorative. Sweep until the cell is non-empty — it must be reachable on SOME input.
rows = run_eval.run(population.sweep(n_per_cell=8), particles=300, sharpness=10.0)
cw = [r for r in rows if r.confidently_wrong]
expect(len(cw) > 0,
       "no problem in the sweep was confidently wrong — either the metric cannot fire, "
       "or reach and correctness are coupled and the caveat is decorative")
print(f"  confidently wrong fires on {len(cw)}/{len(rows)} problems")

section("7. questions help where separation is clear, and the baseline is the control")
clear = [r for r in rows if "_clear_" in r.matrix.id]
bunched = [r for r in rows if "_bunched_" in r.matrix.id]
mc, mb = run_eval.metrics(clear), run_eval.metrics(bunched)
expect(mc["winner_correct"] > mb["winner_correct"],  # type: ignore[operator]
       "the selector is no better on clearly-separated candidates than on bunched ones — "
       "it is not responding to the signal it is supposed to use")
expect(mc["reach_rate"] > mb["reach_rate"],  # type: ignore[operator]
       "reach rate does not respond to separation")
print(f"  clear: correct {mc['winner_correct']:.0%} reach {mc['reach_rate']:.0%}  |  "  # type: ignore[str-format]
      f"bunched: correct {mb['winner_correct']:.0%} reach {mb['reach_rate']:.0%}")  # type: ignore[str-format]

section("8. the no-questions baseline is a real control, not a strawman")
base = NoElicit()
hits = sum(base.run(r.matrix).winner == r.matrix.true_best() for r in rows)
expect(hits > len(rows) * 0.3,
       f"the baseline only scores {hits}/{len(rows)} — too weak to be a meaningful control")
expect(hits < len(rows),
       "the baseline is perfect, so nothing the selector does can ever show a lift")

section("9. wilson_ci behaves")
lo, hi = run_eval.wilson_ci(48, 100)
expect(lo < 0.48 < hi, "CI does not bracket the observed proportion")
expect(run_eval.wilson_ci(0, 0) == (0.0, 0.0), "empty sample divides by zero")
for k, n in ((0, 50), (50, 50)):
    lo, hi = run_eval.wilson_ci(k, n)
    expect(0.0 <= lo <= hi <= 1.0, f"wilson_ci({k},{n}) escaped [0,1]")

print("\n" + "=" * 70)
print(f"{_PASSED} assertions passed, {len(_FAILED)} failed")
if _FAILED:
    print("\nFAILURES:")
    for f in _FAILED:
        print(f"  - {f}")
print("=" * 70)
raise SystemExit(1 if _FAILED else 0)
