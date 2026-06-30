# Lana Simulation Pipeline
*Tim Chen · last updated 2026-06-30*

---

## What this is

An eval pipeline that drives mock-user LLM agents through real Lana conversations, scores each transcript with a judge LLM, and writes results to Supabase for nightly review and HITL curation. Based on `LANA_SIMULATIONS_v1.md`.

Two downstream consumers per run:
- **HITL review** — Tim confirms or flips judge verdicts in `/admin/sims`
- **SFT fine-tuning** — high-scoring transcripts are stored in messages-array format, exportable as JSONL for future model training

---

## Folder layout

```
services/lana-worker/simulations/
├── personas.json           ← 6 mock-user personas (P1–P6)
├── scenarios.json          ← 4 buckets × seeds, LLM improvises from each seed
├── simulation.py           ← drives Lana API, produces transcript dict
├── evaluation.py           ← judge LLM scores transcript, writes to Supabase
├── runner.py               ← CLI entry point, iterates persona × seed matrix
├── gate_check.py           ← CI regression check, called by sim-gate.yml
├── scratch/                ← gitignored, local working notes
└── SIMULATION_PIPELINE.md  ← this file

.github/workflows/
├── sim-nightly.yml         ← cron: runs full matrix at 02:00 EDT every night
└── sim-gate.yml            ← PR gate: blocks deploy on axis regression or HARD_FAIL increase

apps/admin/app/sims/
└── page.tsx                ← /admin/sims review UI (built)
```

---

## File-by-file: what each does

### `personas.json`
Array of 6 personas. Each persona has:
- `id` (P1–P6), `name`, `tech_comfort` (low/medium/high)
- `profile`: `nickname`, `home_block_id` (H3 hex), `email` (provisioning hint for Asjid)
- `identity_claims`: array of claims pre-seeded into Supabase before each run — wipe-then-reseed ensures clean state. Valid buckets: `heritage`, `stage`, `vicinity`, `faith`, `activity`, `interest`, `general`
- `character`: prose description of how this person speaks and behaves — injected directly into the mock-user system prompt

| ID | Persona | Key claims | Stress test |
|----|---------|-----------|-------------|
| P1 | Lake Nona Sports Mom (Sarah) | activity=youth_soccer, stage=school_age, heritage=american, vicinity=lake_nona | happy path, fast disengager |
| P2 | Lake Nona Faith Mom (Fatima) | stage=young_child, heritage=pakistani, faith=muslim | faith/values filtering |
| P3 | Orlando Food Mom (Rosa) | stage=school_age, heritage=mexican, interest=home_cooking, general=spanish_speaker | mid-convo language switch |
| P4 | Tampa Beauty Mom (Camila) | stage=toddler, heritage=brazilian, interest=beauty_wellness, general=portuguese_speaker | specific neighbor requests |
| P5 | Crossover Bilingual Mom (Marisol) | stage=school_age, heritage=colombian, general=spanish_english_bilingual, activity=community_organizer | Spanglish code-switching |
| P6 | Skeptical Mom (Diane) | zero claims | cold-start / rapport building |

### `scenarios.json`
4 buckets, each with a `description` (injected into both mock-user and judge prompts), `pass_criteria`, and `seeds`. Each seed has `label`, `opening_line`, `must_not`.

| Bucket | Seeds | What it tests |
|--------|-------|---------------|
| `in_scope_success` | 4 | Clean happy-path tool execution in ≤2 turns |
| `out_of_scope_rejection` | 4 | All 5 beats of the refusal pattern land |
| `ambiguous_clarity` | 5 | Lana counter-asks before routing, no tool on turn 1 |
| `edge_cases` | 4 | Zero-claims cold start, ASR garble, language switch, silence |

Total: **17 seeds × 6 personas = 102 runs** per nightly cycle.

### `simulation.py`
1. Queries Supabase for past runs on the same `bucket/seed_label` (last 3) — injects summaries into the mock-user system prompt so the LLM varies its approach on known-passing seeds and probes harder on known failures
2. Wipes and re-seeds the persona's identity claims in Supabase (clean state per run)
3. Gets a JWT for the mock user (`SIM_TOKEN_P{n}` env var, falls back to `SIM_BYPASS_TOKEN`)
4. Opens a Lana session via `POST /lana/sessions`
5. Loops up to `MAX_TURNS=8`: mock-user LLM generates a `UserTurn` (Pydantic structured output via `openai.beta.chat.completions.parse`) → POST to Lana → capture `routing.intent_class`, `routing.confidence`, `tool_called`, `ui_intent`
6. Stops on `user_turn.disengage=true` or `lana_resp.ready_to_complete=true`
7. Completes the session, returns transcript dict

### `evaluation.py`
1. Receives transcript dict from `simulation.py`
2. **Pass 1** (temperature=0.2): judge LLM scores 4 axes — `goal_completion` (0.30), `warmth_tone` (0.25), `five_beat_landed` (0.25, OOS bucket only), `no_hallucination` (0.20). Each axis: PASS / SOFT_FAIL / HARD_FAIL + reasoning
3. **Pass 2** (temperature=0.3): judge LLM writes a 3-sentence summary for future test case sampling
4. Computes `weighted_score` (0.0–1.0)
5. Sets `sft_eligible=true` if `weighted_score >= 0.85` and zero HARD_FAILs — builds `sft_messages` in OpenAI messages-array format with `LANA_SYSTEM_PROMPT_PLACEHOLDER` sentinel
6. Writes to Supabase `simulations` table (upsert on `run_id`)

### `runner.py`
CLI entry point. Iterates the full persona × seed matrix, calls `simulation.run()` then `evaluation.score()` per pair. Errors are isolated — one failed run doesn't abort the batch. Writes a run log to `scratch/run_<timestamp>.json`.

```bash
python runner.py                                                    # full 102-run matrix
python runner.py --persona P1                                       # all seeds for P1
python runner.py --bucket out_of_scope_rejection                    # all personas × 4 OOS seeds
python runner.py --persona P1 --bucket in_scope_success --seed "create meet happy path"
python runner.py --dry-run                                          # validate matrix, no API calls
```

### `gate_check.py`
Called by `sim-gate.yml` after `runner.py` completes on a PR branch. Fetches this PR's results and the baseline tag's results from Supabase, compares per-axis averages and HARD_FAIL counts. Posts a markdown table comment to the PR. Exits non-zero (blocks merge) if any axis regresses >5% or HARD_FAIL count increases on `out_of_scope_rejection` or `ambiguous_clarity` buckets. Auto-passes if no baseline tag is set yet.

### `apps/admin/app/sims/page.tsx`
Review UI at `/admin/sims`. Sidebar lists all runs sorted worst→best score, with filters for persona, bucket, hitl_status, SFT eligibility. Click a run to expand: left pane shows full transcript with intent routing metadata per turn, right pane shows per-axis judge reasoning and verdict buttons (Confirm / False positive / False negative / Skip). Verdict + optional note written back to Supabase, `hitl_status` flips to `reviewed`.

---

## What's confirmed from the codebase

### Lana API endpoints (`services/lana-worker/app/main.py`)

| Method | Path | Used by |
|--------|------|---------|
| `POST` | `/lana/sessions` | `simulation.py` — open session |
| `POST` | `/lana/sessions/{id}/messages` | `simulation.py` — send turn, get reply |
| `POST` | `/lana/sessions/{id}/complete` | `simulation.py` — end session |
| `GET`  | `/health` | smoke-check before a run |

`SendMessageResponse` shape (from `app/models.py`):
- `assistant_message: str`
- `routing: { intent_class, confidence, tool_called } | None`
- `ui_intent: str | None`
- `ready_to_complete: bool`

### Identity claim buckets (7 valid values)
Confirmed from `vertex_extract.py` and `profile_intake.py` LLM prompts:
`heritage` · `stage` · `vicinity` · `faith` · `activity` · `interest` · `general`

### Auth
Auth migrated from phone OTP to email OTP. `phone_verified` field kept for backward compat but fed by `email_confirmed_at`. Mock users need email accounts with `email_confirmed_at` set.

---

## Supabase migration (hand SQL to Asjid)

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
  sft_messages    jsonb,   -- OpenAI messages-array format; null when not eligible
  -- metadata
  model_versions  jsonb,
  created_at      timestamptz default now()
);

create index on public.simulations (weighted_score asc);
create index on public.simulations (hitl_status);
create index on public.simulations (sft_eligible) where sft_eligible = true;
```

SFT export when Tommaso wants to train:
```sql
select sft_messages from public.simulations where sft_eligible = true;
```
Dump as JSONL — one row per line — hand to fine-tuning pipeline.

---

## GitHub Actions secrets (Asjid sets these)

The nightly cron and PR gate run in GitHub's CI environment — they have no access to `.env.local`. Asjid needs to copy the same values he provisioned into **GitHub repo Settings → Secrets and variables → Actions**:

| Secret name | Where it comes from |
|---|---|
| `SIM_LANA_BASE_URL` | Staging/prod lana-worker URL (not localhost) |
| `OPENAI_API_KEY` | Same key as in `.env.local` |
| `SUPABASE_URL` | Same as in `.env.local` |
| `SUPABASE_SERVICE_ROLE_KEY` | Same as in `.env.local` |
| `SIM_PASSWORD` | Shared password for all 6 sim accounts — do not commit |
| `SUPABASE_ANON_KEY` | Same as in `.env.local` — needed for password-grant login |
| `SIM_BASELINE_TAG` | Set after first green nightly run — e.g. `sim-baseline-2026-07-03` |

One-time copy. Same values as `.env.local`, different vault.

---

## What still needs to come from Asjid

1. ✅ **6 mock user accounts** — provisioned, email-confirmed, `home_block_id = 8a2a1072b59ffff`
2. ✅ **Auth** — password grant, shared `SIM_PASSWORD`, no bypass token needed
3. ✅ **`home_block_id`** — `8a2a1072b59ffff` (Lake Nona Block A), filled in `personas.json`
4. **Run the `simulations` migration** — SQL above, Asjid pushes via Supabase dashboard
5. **Add GitHub Actions secrets** — see table above; add `SIM_PASSWORD` and `SUPABASE_ANON_KEY`

Once 1–3 land: `python runner.py --dry-run` to validate, then drop `--dry-run` for the first live run.

---

## What's left to build

| Item | Status |
|------|--------|
| `personas.json` | ✅ Done — pending `home_block_id` + real emails from Asjid |
| `scenarios.json` | ✅ Done |
| `simulation.py` | ✅ Done — claims seeding stubbed until Asjid provisions accounts |
| `evaluation.py` | ✅ Done — includes HITL fields + SFT messages |
| `runner.py` | ✅ Done |
| `gate_check.py` | ✅ Done |
| `sim-nightly.yml` | ✅ Done — needs GitHub secrets set |
| `sim-gate.yml` | ✅ Done — needs GitHub secrets + first baseline tag |
| `/admin/sims` UI | ✅ Done |
| `simulations` migration | ⏳ Asjid runs it |
| First green run | ⏳ Blocked on Asjid's accounts + migration |
| Tag `sim-baseline-2026-07-03` | ⏳ After first green nightly run completes |
| Thu curation pass | ⏳ After first run — fix broken seed cases |

---

## Execution order once Asjid delivers

```
1. Asjid: run migration SQL, provision accounts, confirm home_block_id
2. Tim: fill home_block_id in personas.json, add SIM_TOKEN_P* to .env.local
3. Tim: python runner.py --dry-run   ← validates matrix loads cleanly
4. Tim: python runner.py --persona P1 --bucket in_scope_success --seed "create meet happy path"
          ← first single-run smoke test
5. Tim: python runner.py             ← full 102-run matrix
6. Tim: review results in /admin/sims, curate worst cases
7. Tim: git tag sim-baseline-2026-07-03 <sha>
8. Asjid: add GitHub Actions secrets → nightly fires automatically at 02:00 EDT
```
