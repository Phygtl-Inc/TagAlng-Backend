# Rapport claim-extraction eval harness

Fixture harness for Lana's **rapport parser** — the per-turn identity-extraction pass that turns
a user message into structured identity claims. It tests the **real extraction path** end-to-end
(no mocks, no DB): `app.vertex_extract.incremental_claims_from_utterance` →
`parse_incremental_claims_data` → the same PII-redaction backstop every real write goes through
(`app.claims_persist.clean_claims_for_persist`).

Part of Tim's sim/eval suite under `services/lana-worker/simulations/` — sibling to `circles_zip/`
and `policy_eval/`; the conversational pipeline it complements is documented in
`../SIMULATION_PIPELINE.md`.

## Run it

```bash
cd services/lana-worker/simulations/rapport
python run_eval.py                # all 27 fixtures, mechanical axes only
python run_eval.py --id rapport_006   # one fixture
python run_eval.py --judge         # also run the gap-quality LLM judge (costs a call)
python run_eval.py --dry-run       # validate fixture shapes, no API calls
python run_eval.py --out /tmp/r.md # report artifact path (default: out/report.md)
```

Exit codes: **0** all axes passed · **1** an axis failed (or the run was vacuous) · **2** the
fixture file itself is invalid. `--dry-run` really validates now (see *Fixture validation* below),
so it is a genuine no-cost CI smoke test rather than a pretty-printer.

Every run writes **`out/report.md`** — axis-tagged failures plus per-fixture detail. That artifact
contains **pre-redaction** extractor output, i.e. the fixtures' planted PII, which is why
`services/lana-worker/simulations/*/out/` is gitignored. Keep it that way; don't paste it around.

The script puts `services/lana-worker` on `sys.path` itself and loads the repo-root `.env.local`,
so it runs from its own directory. It routes through `orchestrator/llm.py`, which honors
`LANA_LLM_PROVIDER` — locally that's `openai` (same as the rest of the sim suite), so this exercises
the OpenAI path, not the Vertex/Gemini path prod actually runs. That's expected and matches how the
rest of the harness runs locally.

## What it scores

Same mechanical-vs-judged split as the other harnesses: ground truth known by construction is
checked in code; only the one genuinely subjective axis uses an LLM judge.

| Axis | Type | What it checks |
|---|---|---|
| **facet precision / recall** | mechanical | returned claims and expectations are paired by a **one-to-one maximum bipartite matching** (Kuhn's augmenting paths), and *both* ratios read off that single assignment. Precision = returned claims that got paired; recall = expectations that got paired. Match = `bucket` + `label_has`/`label_has_any` substring + `confidence_min` (+ optional `disclosure`/`transient`/`vague`). **`concept` is NOT matched exactly** — the extractor's slug is a free choice (`karate_practice` vs `karate`), only its regex shape is fixed. |
| **anonymization** | mechanical | `must_not_contain` strings never appear — checked **after** `clean_claims_for_persist` runs, across each claim's `label`/`source_quote`/each `synonym`/`details`, the extracted `nickname`, **and** the raw `followup_question` (redaction is never applied to the followup in the real pipeline, so it's a distinct, unprotected leak vector). Fields are tested **separately** and **normalised** (case + punctuation flattened), so punctuation tricks can't hide a leak and a needle can't "match" by straddling two fields. 100% pass required; HARD_FAIL on any leak. |
| **kids_count correctness** | mechanical | `kids_count` is never a claim — checked separately against `expect_kids_count`. |
| **followup presence** | mechanical | whether `followup_question` is non-null matches `expect_followup`. The question *text* is AI-authored per turn and not asserted verbatim. |
| **followup not-a-repeat** | mechanical | only for fixtures with `recent_questions`: the followup must not be an **exact** repeat (modulo case/punctuation) of a question already asked. Near-duplicate detection needs judgment, so it stays out of this gate. |
| **gap quality** | LLM judge (`--judge` only) | a 3-point rating of the followup question: too-vague / just-right / too-narrow. The one subjective axis; skipped by default since it costs a call. **Advisory:** the judge is a total function (any error/timeout/bad JSON → `unscored`), `unscored` is excluded from the just-right denominator, and this axis **never** affects the exit code. |

**Fail-closed details.** A per-fixture extractor exception is recorded as an `ERROR` verdict, counted
as a hard failure, and the run **continues** (one flaky call can't wipe out the other 26 fixtures'
results). A zero denominator reports **`n/a`**, never a flattering `1.00` — and a run that scored
nothing at all (every call errored) fails as `VACUOUS RUN` rather than printing "all axes passed".

## Fixtures (`fixtures.yaml`)

27 hand-verified fixtures, each locking in one extraction behavior or edge case. Key fields:
`input` · `existing_labels` (dedup context) · `recent_questions` (already-asked context) ·
`expect_claims` (bucket/concept/`label_has`|`label_has_any`/confidence_min/…) · `expect_no_claims`
(greetings, vague, negations, forbidden categories → zero claims) · `expect_kids_count` ·
`expect_followup` · `must_not_contain` · `notes`. The header comment in `fixtures.yaml` is the
authoritative field guide.

### Fixture validation (what `--dry-run` enforces)

Load-time, over the **whole** file regardless of `--id`, exiting **2** on any problem:

- ids present and unique; `input` non-empty.
- `expect_no_claims: true` **and** a non-empty `expect_claims` → contradiction. (`expect_no_claims:
  false` *alongside* `expect_claims` is legal and deliberate — see `rapport_006`.)
- every `expect_claims` entry has a `bucket` (the harness indexes it as the primary match key).
- no **inert matcher**: an expectation with an empty `label_has` and no `label_has_any` degenerates
  to "any claim in this bucket" and scores a vacuous pass. `rapport_012` used to be exactly that.

**Why these axes are mechanical:** the ground truth is known by construction — the PII in
`must_not_contain` is self-declared in the fixture, and the expected facets are hand-labeled — so
precision/recall/anonymization can be checked without a judge (no circularity). Only "is this
follow-up question well-scoped?" needs judgment, so that's the lone LLM-judged axis.

## File layout

| File | What it is |
|---|---|
| `fixtures.yaml` | 27 fixtures + the authoritative field-guide header. |
| `run_eval.py` | CLI harness (`--id`, `--judge`, `--dry-run`, `--out`). Calls the real extractor + redaction; no DB. |
| `out/report.md` | Run artifact — axis-tagged failures + per-fixture detail. **Gitignored: holds pre-redaction PII.** |
| `README.md` | This file. |
