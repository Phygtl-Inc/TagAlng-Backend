# D11 — is τ_merge/τ_new calibrated? (measured, not assumed)

204 candidate variant pairs (`d3_resolve.py`'s hard class), labelled same/different by a model pass and audited at 93% human agreement (30-pair blind stratified sample, 2026-09-18). This is the first time the resolver's actual merge/new decisions have been scored against real ground truth rather than just tallied.

## Current thresholds: τ_merge=0.86, τ_new=0.62

| decision | n | correct | precision (95% CI) |
|---|---|---|---|
| MERGE | 25 | 21 truly same | 84% (65%-94%) |
| NEW (mint) | 63 | 60 truly different | 95% (87%-98%) |
| ADJUDICATE | 116 | 32 same / 84 different | n/a — deferred to one LLM call each, by design |

## τ_merge sweep

| τ | n at/above | correct | wrong | precision |
|---|---|---|---|---|
| 0.7 | 95 | 50 | 45 | 53% |
| 0.72 | 84 | 45 | 39 | 54% |
| 0.74 | 69 | 42 | 27 | 61% |
| 0.76 | 62 | 40 | 22 | 65% |
| 0.78 | 55 | 38 | 17 | 69% |
| 0.8 | 47 | 34 | 13 | 72% |
| 0.82 | 36 | 26 | 10 | 72% |
| 0.84 | 32 | 24 | 8 | 75% |
| 0.86 **(current)** | 25 | 21 | 4 | 84% |
| 0.88 | 22 | 19 | 3 | 86% |
| 0.9 | 20 | 19 | 1 | 95% |
| 0.92 | 18 | 17 | 1 | 94% |
| 0.94 | 8 | 8 | 0 | 100% |
| 0.96 | 5 | 5 | 0 | 100% |
| 0.98 | 1 | 1 | 0 | 100% |

## τ_new sweep

| τ | n at/below | correct | wrong | precision |
|---|---|---|---|---|
| 0.4 | 1 | 1 | 0 | 100% |
| 0.42 | 3 | 3 | 0 | 100% |
| 0.44 | 4 | 4 | 0 | 100% |
| 0.46 | 4 | 4 | 0 | 100% |
| 0.48 | 4 | 4 | 0 | 100% |
| 0.5 | 5 | 5 | 0 | 100% |
| 0.52 | 10 | 10 | 0 | 100% |
| 0.54 | 18 | 18 | 0 | 100% |
| 0.56 | 29 | 29 | 0 | 100% |
| 0.58 | 36 | 36 | 0 | 100% |
| 0.6 | 52 | 51 | 1 | 98% |
| 0.62 **(current)** | 63 | 60 | 3 | 95% |
| 0.64 | 71 | 68 | 3 | 96% |
| 0.66 | 88 | 84 | 4 | 95% |
| 0.68 | 98 | 94 | 4 | 96% |
| 0.7 | 109 | 103 | 6 | 94% |

## Reading

**τ_merge=0.86 scores 84% precision (CI 65%-94%, n=25) on the hard class** — roughly 1 in 6 pairs it would MERGE on this pool are actually different subjects. That is meaningfully below the 90% D3 gate target, and below the 99.9% D3's own headline number reports (which measures only the easy identical/random classes — this is the first time the actual merge decision has been scored against ground truth on the hard class at all).

Reaching 95% precision on this pool needs τ_merge≈0.96 — ten points higher than current. That is NOT a recommendation to move it: raising τ_merge that far would push most of the pairs currently in the 0.86-0.96 range into ADJUDICATE, which already carries 57% of this hard class at the current threshold (D3's finding) — the honest tradeoff is fewer wrong auto-merges against a real LLM-cost increase, and that is a product/cost call, not something this measurement should decide alone. It does NOT contradict Asjid's separate, correct point that LOWERING τ_merge is unsafe (the husband/wife pair at 0.8575 would merge) — both can be true: 0.86 is too low to trust blindly, and going lower would be worse.

**τ_new=0.62 looks solid**: 95% precision (CI 87%-98%, n=63), and the sweep shows 100% precision holds all the way down to τ≈0.42 — there is real headroom here if a lower τ_new is ever wanted for other reasons (e.g. to shrink the adjudicate band), unlike τ_merge which has no headroom to move down at all.

Sample sizes are small (n=25 merge, n=63 new) — the merge CI alone spans 65%-94%, wide enough that this should be read as 'worth watching and re-measuring once more labelled pairs exist,' not as a settled number.

**Per-class thresholds (decision #6, people vs businesses) are not testable on this corpus** — `latent_signals` holds Layer-3 entity mentions only (activities/interests), with no person/business distinction in the fetched data. Needs the real `subject` table (unshipped) or a differently-scoped labelling pass to answer.
