# CLAUDE.md — Lana sim/eval suite

Guidance for AI agents working in `services/lana-worker/simulations/`. This is Tim's
simulation & evaluation suite. Read this before changing anything here.

## Owner & role boundary (important)

This directory is **eval/simulation only**. When a run surfaces a bug in Lana's *product*
behavior (routing, extraction, tone, the policy), **report it — do not fix `app/` code.**
Backend/product fixes belong to Asjid (backend), Yunchao/Aki (AI/prompts), Tommaso (product).
The deliverable for a found product bug is a clear write-up (often a root-level `*_HANDOFF.md`),
not an edit to Lana's logic. Fixing eval *rubrics/harnesses* here is in scope; fixing Lana is not.

## The six pipelines here

| Path | What it evals | Target |
|---|---|---|
| `./` (runner/simulation/evaluation) | full conversations — decision + tone via a judge | **real Lana API** |
| `rapport/` | the claim-extraction pass (`vertex_extract`) + PII redaction | real extractor, no DB |
| `circles_zip/` | onion matcher + ZIP-unlock state machine | stub ↔ real (SIM_BACKEND) |
| `policy_eval/` | conversational-policy decisions + lingo + safety | stub ↔ live adapter |
| `reco_eval/` | recommendation capture — the questions Lana writes, and the answers the flow accepts | real generator in-process, no DB |
| `subject_eval/` | the subject-graph / recommender-authority spec (Part D + authority) — resolver, elicitation, F-SAFETY, extraction baselines, topic/authority invariants | mix: real dev-data reads, offline stubs where the product hasn't shipped, one real RPC (`attester_authority`) |

`subject_eval/` is the least uniform of the six on purpose: contract v2's subject graph
(`subject_context()`, `subject`/`attestation` tables) hasn't shipped, so its D7/D9/T2 checks run
against a stub built from the accepted spec text, swapped for the real thing the day it lands.
Everything that CAN run against real data already does — D3 (resolver), N1 (τ calibration), N2
(extractor baseline) and the band-spread measurement all read real (non-test) rows from dev.
T3 (anti-gaming) is the newest real-not-stub piece: `attester_authority()` shipped 2026-09-17, so
it mirrors the deployed SQL formula and optionally calls the live RPC once, safely, against a
guaranteed-empty user.

Each has its own `README.md` (this dir uses `SIMULATION_PIPELINE.md`; `LOCAL_STACK.md` covers
running any of them against a local Supabase stack). `circles_zip/` and
`policy_eval/` target features that don't fully exist in code yet, so they use a swappable seam.

`reco_eval/` is the only one whose subject is **nondeterministic enough that a single run means
nothing**: identical calls to the question generator return six questions, or three, or none.
Every number it reports is a rate over `--trials`, never a verdict. Its Arm B (what the flow
accepts as an answer) needs no key and no server and runs in seconds, which makes it the
cheapest real signal in the suite.

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

- **Prefer a LOCAL Supabase stack — see `LOCAL_STACK.md`.** The six sim accounts live in the
  SHARED dev project, and `create_session(force_new=True)` calls `abandon_other_active_sessions`
  (`app/db.py:340,363`), so two concurrent runs destroy each other (`400 session_not_active` on
  turn 2). Local also gives full control of world state, which is what lets `policy_eval
  --backend inproc` honour a scenario's pinned world instead of scoring it UNSCORED.
- Env comes from the repo-root `.env.local` (each entry point loads it). Keys: `SUPABASE_URL`,
  `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SIM_PASSWORD`, `OPENAI_API_KEY`, and
  `LANA_BASE_URL` — **optional.** It defaults to `http://localhost:8000`, which is also the
  default in backend's own `tests/test_L1_L2_intents.py:38`, so 8000 is the shared convention and
  a worker started there needs no env var at all. Set it ONLY when the worker is somewhere else —
  notably `scripts/run-lana-worker-local.sh`, which defaults to **8081** and is the one thing in
  the repo that disagrees. Starting the worker with that script and not setting this points the
  harness at a dead port, and every run fails on turn 1.
  Locally `LANA_LLM_PROVIDER=openai`, so harnesses exercise the OpenAI path, not prod's Vertex/Gemini.
- The **worker** does not read `.env.local` — `scripts/run-lana-worker-local.sh` sources
  `deploy/lana-worker.env` only. Pointing the suite at local means editing BOTH files, and if
  they disagree about `SUPABASE_URL` the run is meaningless (the worker rejects the harness's JWT).
- **Auth goes through `sim_auth.py`**, not a raw password grant: a Supabase `400` is the same
  for "wrong project", "SIM_PASSWORD missing/wrong" and "account never seeded", so it diagnoses
  which. With no `SIM_PASSWORD` but a service-role key it falls back to the admin
  `generate_link` -> `/auth/v1/verify` magic-link flow described in `seed_sim_accounts.sql`'s
  header (# GUESSED on the hashed-token field name; unverified end-to-end).
- `circles_zip/` and `policy_eval/` use **bare intra-package imports** (`from ports import …`), so
  run their scripts from inside the subdir (or the entry point adds its own dir to `sys.path`).
- No-cost smoke tests exist where possible: `policy_eval/run_eval.py --dry-run`, every `selftest.py`,
  `rapport/run_eval.py --dry-run`, `circles_zip/sweep.py --quick`. Use them before touching a judge.
- The Supabase-writing paths degrade to no-ops when creds are absent — the suite runs offline but
  persists nothing.

## Gotchas

- **Never seed prod with synthetic users.** Test accounts / claims / populations are for dev only.
- **Destructive paths are LOCAL-ONLY, enforced in code.** `local_guard.require_local()` gates
  `simulation._seed_claims` (it DELETEs a persona's claims), `circles_zip/live_seed.py`,
  `circles_zip/live_impl.transition_zip` (recount can stamp founding), and all of
  `local_world.py`. It classifies `SUPABASE_URL` as local/dev/unknown and **fails closed** —
  an unrecognised host is refused, not assumed local. It LAYERS on `SIM_ALLOW_WRITES=1`
  ("I meant to write") rather than replacing it ("...to *this* DB"); where both apply, both must
  pass. Override with `SIM_ALLOW_NONLOCAL_WRITES=1`, which prints a banner. If you add a write,
  gate it the same way and add a `selftest.py` case that proves it refuses.
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
