# D11 ground truth — frozen snapshot, 2026-09-18

Promoted out of `out/` (gitignored scratch) into version control because this is a real,
reusable labelled dataset — not disposable run output. It cost actual LLM calls plus a human
audit to produce, and the whole point of D11/F-RESOLVE calibration is having ground truth to
measure the resolver against, so it belongs in git the same way `fixtures.yaml` does.

## Files

- **`variant_pairs.json`** — 204 candidate "variant" pairs mined from real (non-test) dev
  `latent_signals` entity text by `d3_resolve.py`: pairs sharing a distinctive token but not
  identical, the hard class the resolver exists for (as opposed to the trivial identical-vs-
  random classes D3's headline 99.9% number measures). Each row: `a`, `b`, `shared_token`,
  `cosine`, `resolver` (what the current τ_merge=0.86/τ_new=0.62 thresholds would decide).
- **`labels.json`** — the same 204 pairs, each with a model-assigned ground-truth label
  (`same`/`different`/`ambiguous`) and a one-line reason, from `d11_label_assist.py`.
- **`calibration.md`** — `d11_calibration.py`'s report scoring the resolver's actual merge/new
  decisions against these labels. Headline: τ_merge=0.86 hits only 84% precision on this pool
  (CI 65-94%, n=25 — small sample), below the 90% D3 target; τ_new=0.62 hits 95%, solid. Full
  reasoning and the threshold sweep are in the file itself.

## Validated, not just asserted

A 30-pair blind stratified sample was hand-labelled independently and scored against the
model's labels: **93% agreement** (`python d11_label_assist.py --score <filled_csv>`), well
above the 85% trust bar this suite uses for treating model labels as ground truth. The two
disagreements are documented in the calibration script's git history / commit message, not
repeated here.

## This is a FROZEN snapshot, not a live artifact

Re-running `d3_resolve.py` against dev will mine a different candidate set (dev data grows).
Re-running `d11_label_assist.py` will call the LLM again and can produce different labels
(temperature=0, but not guaranteed byte-identical across model versions). **Don't silently
overwrite these files with a fresh run** — if you regenerate this dataset, audit the new sample
again before trusting it, and either replace this snapshot deliberately (with a note on why) or
keep both as separate dated snapshots. The whole reason this is versioned is so anyone can see
exactly what ground truth a given calibration claim was measured against.

## Regenerating from scratch

```
cd services/lana-worker/simulations/subject_eval
python d3_resolve.py --env ../../../../.env.local.dev-backup   # mines out/d3_variant_candidates.json
python d11_label_assist.py                                      # labels all pairs + writes a blind audit sample
# hand-label out/d11_audit_sample.csv, then:
python d11_label_assist.py --score out/d11_audit_sample.csv     # confirm >=85% agreement before trusting it
python d11_calibration.py                                       # scores resolver decisions against the labels
```
