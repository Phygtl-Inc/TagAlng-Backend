# CLAUDE.md — Lana sim/eval suite

Guidance for AI agents working in `services/lana-worker/simulations/`. This is Tim's
simulation & evaluation suite. Read this before changing anything here.

## Owner & role boundary (important)

This directory is **eval/simulation only**. When a run surfaces a bug in Lana's *product*
behavior (routing, extraction, tone, the policy), **report it — do not fix `app/` code.**
Backend/product fixes belong to Asjid (backend), Yunchao/Aki (AI/prompts), Tommaso (product).
The deliverable for a found product bug is a clear write-up (often a root-level `*_HANDOFF.md`),
not an edit to Lana's logic. Fixing eval *rubrics/harnesses* here is in scope; fixing Lana is not.

## The four pipelines here

| Path | What it evals | Target |
|---|---|---|
| `./` (runner/simulation/evaluation) | full conversations — decision + tone via a judge | **real Lana API** |
| `rapport/` | the claim-extraction pass (`vertex_extract`) + PII redaction | real extractor, no DB |
| `circles_zip/` | onion matcher + ZIP-unlock state machine | stub ↔ real (SIM_BACKEND) |
| `policy_eval/` | conversational-policy decisions + lingo + safety | stub ↔ live adapter |

Each has its own `README.md` (this dir uses `SIMULATION_PIPELINE.md`). `circles_zip/` and
`policy_eval/` target features that don't fully exist in code yet, so they use a swappable seam.

## Conventions that recur across the suite

- **Mechanical vs. judged.** Ground truth *known by construction* (a banned word, a planted PII
  string, an unavailable capability, a hand-labeled facet) is checked in **code** — deterministic,
  non-circular, cheap. Only genuinely subjective questions ("right action?", "warm not sycophantic?",
  "well-scoped follow-up?") go to an **LLM judge**. Don't make something mechanical if its ground
  truth needs judgment (false positives poison a gate); don't burn a judge call on something checkable.
- **Prove the mechanical checks fire.** A green run is worthless if the checks are vacuous. The
  harnesses ship a `selftest.py` that feeds each check a planted violation and asserts it HARD_FAILs.
  If you add a mechanical check, add a selftest case for it.
- **Fail closed.** A dropped judge axis, a backend crash, an unscored safety axis → NOT a silent
  PASS. See `policy_eval/judge.py` (`UNSCORED`/`REVIEW`) and `run_eval.py` (error → HARD_FAIL).
- **Stub/live seam** (`circles_zip/`, `policy_eval/`): `SIM_BACKEND=stub|live|dry` via `backend.py`;
  `ports.py` holds the Protocol + data contract; `stub_*` is the spec-faithful reference; `live_*`
  is the real adapter. The `live` adapter must be **honest** about what it can't measure (flag it in
  the report, never let an un-measurable axis vacuously PASS).
- **Flag every assumption.** Anything the spec left open or the DB doesn't back gets an inline
  `# GUESSED` (or `# FLAGGED` for adapter lossiness) with a one-line reason, summarized in a
  root-level `*_HANDOFF.md` for the backend owner to confirm. `grep -rn "GUESSED\|FLAGGED"`.
- **Judge model is independent.** `JUDGE_MODEL = "gpt-4o"`, kept separate from whatever model Lana
  runs, so the judge never grades its own family by construction. Multi-judge is a **triage** signal
  (route disagreement to humans), *not* calibration — real calibration needs a human gold set.
- **Determinism.** No wall-clock in seeded logic — pass an explicit `as_of`/`now`. No unseeded
  randomness (vary by index/seed). This keeps runs reproducible and resumable.

## Running things

- Env comes from the repo-root `.env.local` (each entry point loads it). Keys: `LANA_BASE_URL`,
  `OPENAI_API_KEY`, `SUPABASE_URL`/`SUPABASE_ANON_KEY`/`SUPABASE_SERVICE_ROLE_KEY`, `SIM_PASSWORD`.
  Locally `LANA_LLM_PROVIDER=openai`, so harnesses exercise the OpenAI path, not prod's Vertex/Gemini.
- `circles_zip/` and `policy_eval/` use **bare intra-package imports** (`from ports import …`), so
  run their scripts from inside the subdir (or the entry point adds its own dir to `sys.path`).
- No-cost smoke tests exist where possible: `policy_eval/run_eval.py --dry-run`, every `selftest.py`,
  `rapport/run_eval.py --dry-run`, `circles_zip/sweep.py --quick`. Use them before touching a judge.
- The Supabase-writing paths degrade to no-ops when creds are absent — the suite runs offline but
  persists nothing.

## Gotchas

- **Never seed prod with synthetic users.** Test accounts / claims / populations are for dev only.
- `rapport/` was `git mv`'d from `tests/rapport/`; some in-code docstrings still show the old path —
  the READMEs are current.
- `scratch/` (local run logs) and each harness's `out/` are gitignored — don't commit run artifacts.
- **Git is the human's job here.** Don't auto-commit/push; when syncing with `main`, prefer `merge`
  over `rebase` (no history rewriting).
- `scenarios.json` bucket→rubric routing is by **exact bucket name** (`evaluation.QA_BUCKET_NAMES`),
  not prefix — `edge_trust` must not collide with `edge_cases`.
- **PR gate vs nightly:** buckets with `pr_gate: true` (must-haves — safety/refusals, PII/privacy,
  core function, routing) run on PRs via `runner.py --pr` (`sim-gate.yml`); the full matrix runs
  nightly (`sim-nightly.yml`). Runs are parallel across personas (~5.6×). If you change the PR
  bucket set, keep `gate_check.py`'s baseline-bucket matching intact or the gate will mis-compare.
