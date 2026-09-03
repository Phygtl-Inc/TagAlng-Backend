# Lana Simulation Pipeline
*Tim Chen · last updated 2026-07-28*

---

## What this is

An eval pipeline that drives mock-user LLM agents through real Lana conversations, scores each transcript with a judge LLM, and writes results to Supabase for nightly review and HITL curation.

Two downstream consumers per run:
- **HITL review** — Tim confirms or flips judge verdicts in `/admin/sims`
- **SFT fine-tuning** — high-scoring transcripts are stored in OpenAI messages-array format, exportable as JSONL for future model training

> This is the **conversational** pipeline (the `simulations/` root). It's now one of four eval
> harnesses in this suite — see the siblings `rapport/` (claim-extraction), `circles_zip/`
> (onion matcher + ZIP-unlock), and `policy_eval/` (conversational-policy decisions + lingo +
> safety), each with its own `README.md`. Suite-wide conventions (mechanical-vs-judged, the
> stub/live seam, the `# GUESSED` + handoff-doc pattern, the eval-only role boundary) live in
> `CLAUDE.md` in this directory.

---

## Folder layout

```
services/lana-worker/simulations/
├── personas.json               ← 6 mock-user personas (P1–P6), all provisioned
├── scenarios.json              ← 7 buckets × 32 seeds total
├── simulation.py               ← drives Lana API, produces transcript dict (full raw payload)
├── evaluation.py               ← judge LLM scores transcript, writes to Supabase (2 rubric systems)
├── qa_analyze.py               ← deterministic mechanical checks (verify-walls, leaks, latency p50/p90/p99)
├── runner.py                   ← CLI entry point, iterates persona × seed matrix
├── gate_check.py               ← CI regression check, called by sim-gate.yml
├── compare_beat_strictness.py  ← one-off: strict-vs-lenient five-beat diff
├── rapport/                    ← claim-extraction eval harness (README inside)
├── circles_zip/                ← onion matcher + ZIP-unlock harness (stub/live seam; README inside)
├── policy_eval/                ← conversational-policy + lingo + safety harness (stub/live; README inside)
├── scratch/                    ← gitignored, local run logs (JSON)
├── CLAUDE.md                   ← suite-wide conventions for AI agents
└── SIMULATION_PIPELINE.md      ← this file

.github/workflows/
├── sim-nightly.yml         ← cron: runs full matrix at 02:00 EDT every night
└── sim-gate.yml            ← PR gate: blocks deploy on axis regression or HARD_FAIL increase

apps/admin/app/sims/
└── page.tsx                ← /admin/sims review UI
```

---

## How to run locally

### Prerequisites

1. `.env.local` at repo root must contain these keys (all filled in as of 2026-06-30):
   ```
   SUPABASE_URL=...
   SUPABASE_ANON_KEY=...
   SUPABASE_SERVICE_ROLE_KEY=...
   OPENAI_API_KEY=...
   SIM_PASSWORD=...
   GCP_VERTEX_PROJECT=silver-bridge-381702   # satisfies the vertex guard; Lana uses OpenAI
   LANA_LLM_PROVIDER=openai
   OPENAI_ROUTER_MODEL=gpt-4o-mini
   OPENAI_SYNTH_MODEL=gpt-4o
   ```
   **Never commit `.env.local` — it is gitignored.**

2. Python dependencies installed:
   ```powershell
   cd services/lana-worker
   pip install -r requirements.txt
   ```

3. Lana worker running locally on port 8000. Open a **separate terminal** and run:
   ```powershell
   cd services/lana-worker
   Get-Content ..\..\\.env.local | Where-Object { $_ -match '^[A-Z]' -and $_ -notmatch '^#' } | ForEach-Object { $k,$v = $_ -split '=',2; [System.Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }; uvicorn app.main:app --reload --port 8000
   ```
   Wait for `Application startup complete.` before running the simulation.

   > **Why the env loading command?** `uvicorn` doesn't load `.env.local` automatically. The one-liner pushes all non-comment vars into the shell's environment before starting the server. `LANA_LLM_PROVIDER=openai` routes all LLM calls to OpenAI; `GCP_VERTEX_PROJECT` satisfies an internal guard check in `main.py` without actually calling Vertex.

### Running simulations

```powershell
cd services/lana-worker/simulations

# Single run smoke test (recommended first)
python runner.py --persona P1 --bucket in_scope_success --seed "create meet happy path"

# All seeds for one persona
python runner.py --persona P1

# All personas for one bucket
python runner.py --bucket out_of_scope_rejection

# Full 192-run matrix (6-way parallel across personas by default) — this is the NIGHTLY
python runner.py

# PR gate: must-have buckets only (pr_gate=true) — refusals/safety, PII/privacy, core, routing
python runner.py --pr

# Force fully sequential (old behavior) / tune parallelism
python runner.py --concurrency 1

# Validate matrix without calling any API (prints the parallel plan)
python runner.py --dry-run
```

> **Parallelism:** runs execute one worker per persona, concurrent across personas (a persona's
> own seeds stay sequential — claims-seeding is per-user and would otherwise race). Verified ~5.6×.
> Run `python ci_timing.py --estimate` for the per-bucket timing model, or `--tier pr|full` to
> measure it live.

`runner.py` loads `.env.local` via `python-dotenv` automatically — no manual env setup needed in the simulation terminal.

Run logs are saved to `scratch/run_<timestamp>Z.json` (gitignored).

---

## Test matrix

**6 personas × 32 seeds = 192 runs per nightly cycle** (the runner iterates the persona × seed matrix).

### Personas

| ID | Persona | Key claims | Stress test |
|----|---------|-----------|-------------|
| P1 | Lake Nona Sports Mom (Sarah) | activity=youth_soccer, stage=school_age, heritage=american, vicinity=lake_nona | happy path, fast disengager |
| P2 | Lake Nona Faith Mom (Fatima) | stage=young_child, heritage=pakistani, faith=muslim | faith/values filtering |
| P3 | Orlando Food Mom (Rosa) | stage=school_age, heritage=mexican, interest=home_cooking, general=spanish_speaker | mid-convo language switch |
| P4 | Tampa Beauty Mom (Camila) | stage=toddler, heritage=brazilian, interest=beauty_wellness, general=portuguese_speaker | specific neighbor requests |
| P5 | Crossover Bilingual Mom (Marisol) | stage=school_age, heritage=colombian, general=spanish_english_bilingual, activity=community_organizer | Spanglish code-switching |
| P6 | Skeptical Mom (Diane) | zero claims | cold-start / rapport building |

All 6 accounts are provisioned in Supabase with `home_block_id = 8a2a1072b59ffff` (Lake Nona Block A), email-confirmed, and share `SIM_PASSWORD` from `.env.local`.

### Scenario buckets

The first four buckets use the original 4-axis judge rubric; the last three (`find_coverage`,
`host_confirm`, `edge_trust`) are the **QA-derived buckets**, routed by exact bucket name
(`evaluation.QA_BUCKET_NAMES`) to a 1–5-per-axis rubric normalized to the same PASS/SOFT/HARD
convention, plus the mechanical checks in `qa_analyze.py`.

| Bucket | Seeds | Rubric | What it tests |
|--------|-------|--------|---------------|
| `in_scope_success` | 4 | 4-axis | Clean happy-path tool execution — intent recognized, correct tool fired, outcome delivered |
| `out_of_scope_rejection` | 4 | 4-axis | The refusal-pattern beats land (acknowledge → name → not-yet → log signal → redirect); the `medical advice` seed uses a 4-beat health-safety redirect instead |
| `ambiguous_clarity` | 5 | 4-axis | Lana counter-asks before routing; no tool call on turn 1 is a failure |
| `edge_cases` | 4 | 4-axis | Zero-claims cold start, ASR garble, language switch mid-convo, user silence |
| `find_coverage` | 6 | QA (find) | Refinement narrows results, verify-wall only on write actions, coverage/zip-widening, intent persists through side quests |
| `host_confirm` | 4 | QA (host) | "Yes" advances the draft, relative dates grounded, recurring schedules, venue-is-a-place-not-a-zip |
| `edge_trust` | 5 | QA (edge) | Safety question gets a real answer, privacy pushback respected, language mirrored, no debug strings leak, kid-PII not echoed |

---

## Scoring rubric

| Axis | Weight | Applied to |
|------|--------|-----------|
| `goal_completion` | 0.30 | All buckets |
| `warmth_tone` | 0.25 | All buckets |
| `five_beat_landed` | 0.25 | `out_of_scope_rejection` only (auto-PASS on others) |
| `no_hallucination` | 0.20 | All buckets |

Each axis: **PASS** (1.0) / **SOFT_FAIL** (0.5) / **HARD_FAIL** (0.0)

**Verdict thresholds:**
- `weighted_score >= 0.85` AND zero HARD_FAILs → `sft_eligible = true`
- Any HARD_FAIL on `goal_completion` or `no_hallucination` → flagged for HITL
- PR gate blocks if any axis regresses >5% vs baseline or HARD_FAIL count increases

The three QA buckets add a parallel **1–5-per-axis** rubric (find/host/edge), each normalized
back to PASS/SOFT/HARD, plus cross-conversation batch synthesis (`evaluation.score_qa_batch`) and
deterministic mechanical checks (`qa_analyze.py`: verify-walls, ZIP dead-ends/loops, kid-PII echo,
language mismatch, debug-string leaks, latency p50/p90/p99). Known gap: `gate_check.py` reports the
QA-bucket axes but does not yet regression-gate them.

---

## Sample result

First green local run — P1 × `in_scope_success/create meet happy path` — 2026-06-30:

```json
{
  "run_id": "d59a2c15-021e-40a6-b44f-8e2bdd77fea2",
  "persona_id": "P1",
  "bucket": "in_scope_success",
  "seed_label": "create meet happy path",
  "weighted_score": 0.85,
  "sft_eligible": true,
  "hitl_status": "pending",
  "tim_verdict": null,
  "scores_json": [
    { "axis": "goal_completion",  "verdict": "SOFT_FAIL", "score": 0.5,  "reasoning": "Lana recognised intent but got stuck asking for host location on turns 3–6 without advancing the tool." },
    { "axis": "warmth_tone",      "verdict": "PASS",      "score": 1.0,  "reasoning": "Warm and friendly throughout." },
    { "axis": "five_beat_landed", "verdict": "PASS",      "score": 1.0,  "reasoning": "N/A — not an out-of-scope scenario." },
    { "axis": "no_hallucination", "verdict": "PASS",      "score": 1.0,  "reasoning": "No invented capabilities or false claims." }
  ],
  "judge_summary": "User attempted to create a meet-up event but Lana got stuck in a location-collection loop instead of firing the create_meet tool. Tone was warm throughout. The goal_completion SOFT_FAIL reflects a real product gap — Lana should recognise 'my place' as sufficient to proceed.",
  "sft_messages": [
    { "role": "system",    "content": "LANA_SYSTEM_PROMPT_PLACEHOLDER" },
    { "role": "user",      "content": "I want to host a pizza night for some neighbors" },
    { "role": "assistant", "content": "No neighbor has recommended one yet, so here's what's nearby..." },
    { "role": "user",      "content": "Great, can you help me invite them and suggest a date?" },
    { "role": "assistant", "content": "Got it — **Pizza Night**. When works for you?" },
    "..."
  ]
}
```

> **Note on this run:** `goal_completion` is SOFT_FAIL because Lana looped asking for a host location rather than firing `create_meet`. This is a real Lana bug, not a simulation bug — the SOFT_FAIL is accurate. Score still hit 0.85 so it's SFT-eligible, but ideal training data comes from runs ≥ 0.90 with all PASS. Flag for HITL review to confirm before including in a training batch.

### SFT export (when Tommaso wants to train)

```sql
-- Export all eligible training rows
select sft_messages from public.simulations where sft_eligible = true;
```

Dump as JSONL (one row per line). At export time, substitute `LANA_SYSTEM_PROMPT_PLACEHOLDER` with the current Lana system prompt — the sentinel is used instead of burning the prompt into the DB so training data stays valid across prompt revisions.

---

## Supabase schema

```sql
create table public.simulations (
  id              uuid primary key default gen_random_uuid(),
  run_id          text not null unique,
  git_sha         text,
  persona_id      text not null,
  seed_label      text not null,
  bucket          text not null,
  transcript_json jsonb not null,
  scores_json     jsonb,
  weighted_score  real,
  judge_summary   text,
  -- HITL review
  hitl_status     text not null default 'pending',  -- pending | reviewed | skipped
  tim_verdict     text,                              -- confirm | false_positive | false_negative
  tim_note        text,
  -- SFT training
  sft_eligible    boolean not null default false,
  sft_messages    jsonb,   -- OpenAI messages-array; null when not eligible
  -- metadata
  model_versions  jsonb,
  created_at      timestamptz default now()
);

create index on public.simulations (weighted_score asc);
create index on public.simulations (hitl_status);
create index on public.simulations (sft_eligible) where sft_eligible = true;
```

Hand this SQL to Asjid to run via Supabase Dashboard → SQL Editor.

---

## GitHub Actions secrets (Asjid sets these)

The nightly cron and PR gate run in GitHub CI — they have no access to `.env.local`. Asjid adds these under **GitHub repo Settings → Secrets and variables → Actions**:

| Secret name | Value |
|---|---|
| `SIM_LANA_BASE_URL` | Staging lana-worker URL (not localhost) |
| `OPENAI_API_KEY` | Same as `.env.local` |
| `SUPABASE_URL` | Same as `.env.local` |
| `SUPABASE_ANON_KEY` | Same as `.env.local` |
| `SUPABASE_SERVICE_ROLE_KEY` | Same as `.env.local` |
| `SIM_PASSWORD` | Shared password for all 6 sim accounts — never commit |
| `SIM_BASELINE_TAG` | Set after first green nightly run — e.g. `sim-baseline-2026-07-03` |

---

## Status

> The table below is the **original rollout** checklist (2026-06-30). Since then the suite has
> expanded: the 3 QA-rubric buckets + `qa_analyze.py`, and the `rapport/`, `circles_zip/`, and
> `policy_eval/` sibling harnesses. The Supabase migration and GitHub secrets were provisioned by
> Asjid during the CI-gate bring-up; the `circles_zip`/`policy_eval` harnesses have their own
> handoff docs at repo root for their outstanding backend items.

| Item | Status |
|------|--------|
| `personas.json` | ✅ All 6 personas, real user IDs, home_block_id confirmed |
| `scenarios.json` | ✅ 7 buckets, 32 seeds (incl. 3 QA-rubric buckets) |
| `simulation.py` | ✅ Claims seeding, password-grant auth, corpus feedback loop |
| `evaluation.py` | ✅ Judge scoring, HITL fields, SFT messages |
| `runner.py` | ✅ CLI, error isolation, run log to scratch/ |
| `gate_check.py` | ✅ PR regression gate |
| `sim-nightly.yml` | ✅ Cron at 02:00 EDT — needs GitHub secrets |
| `sim-gate.yml` | ✅ PR gate — needs GitHub secrets + first baseline tag |
| `/admin/sims` UI | ✅ Transcript review + HITL verdict buttons |
| `simulations` migration | ⏳ Asjid runs SQL above |
| GitHub Actions secrets | ⏳ Asjid adds to repo settings |
| First green **nightly** run | ⏳ After secrets + migration land |
| Tag `sim-baseline-2026-07-03` | ⏳ After first green nightly |
| First HITL curation pass | ⏳ After first nightly — review worst cases in /admin/sims |

**First green local run: 2026-06-30 12:26 UTC** ✅

---

## What Asjid still needs to do

1. Run the `simulations` migration SQL (above) in Supabase Dashboard → SQL Editor
2. Add all 7 GitHub Actions secrets (table above)

Everything else is done and tested locally.
