-- ============================================================================
-- 20260917160000_simulations_harness.sql
--
-- Repo path: supabase/migrations/20260917160000_simulations_harness.sql
-- Target:    Supabase kmetmatfxdkrialwrnzj (tagalng-prod)
-- Follows:   20260917150000_rls_hardening (PR #119) — MUST land first.
--
-- WHY
--   `public.simulations` was built for the nightly judge harness: one row per
--   persona x seed transcript, scored 0..1 by an LLM judge, triaged by a human
--   in /admin/sims. The zero-bug program needs it to hold something different:
--   one row per (run_id, section_id, persona_id, arm) carrying a list of
--   individually-verdicted assertions.
--
--   Per LANA_ZERO_BUG_PROGRAM_FINAL.md §5 the reporting unit is
--     (section_id, persona_id, arm)
--   with `assertions_json` = [{id, verdict, observed, expected}], plus `score`,
--   `verdict`, `git_sha`, `run_id`.
--
-- THE BLOCKER THIS MIGRATION EXISTS TO REMOVE
--   `simulations.run_id` is `text not null UNIQUE` (constraint
--   `simulations_run_id_key`, verified on prod). A run_id is therefore capable
--   of holding exactly ONE row. The program writes 9 personas x 12 sections x
--   up to 3 arms under a single run_id — every insert after the first would
--   fail with a unique violation. The uniqueness is on the wrong grain: it
--   should identify a *result*, not a *run*.
--
-- SAFETY
--   * Non-destructive: no DROP TABLE, no DROP COLUMN, no data rewritten.
--   * The dropped constraint is a uniqueness *relaxation*. It cannot orphan or
--     invalidate an existing row. Prod holds 0 rows in `simulations` (verified),
--     so the composite unique index is built over an empty table.
--   * Idempotent: every statement is `if not exists` / `if exists` guarded.
--     Verified by running the whole file twice inside one transaction.
--   * The three judge-era NOT NULL columns (`seed_label`, `bucket`,
--     `transcript_json`) are left NOT NULL and given defaults instead, so the
--     existing judge writer is untouched while the assertion writer may omit
--     them. No writer breaks either way.
--   * RLS/grants are inherited from 20260917150000_rls_hardening. This
--     migration adds no policy and no grant. Writes stay service_role-only.
--
-- ROLLBACK: see PR_simulations_harness.md §6.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Regrain the uniqueness: run_id is not a result identity
-- ----------------------------------------------------------------------------

alter table public.simulations
  drop constraint if exists simulations_run_id_key;

-- New columns must exist before the composite unique index references them.


-- ----------------------------------------------------------------------------
-- 2. The assertion-harness columns
-- ----------------------------------------------------------------------------

-- The program section under test: 'P0'..'P8', 'X1'..'X3'.
alter table public.simulations
  add column if not exists section_id text;

-- The cross-cutting arm from personas.json#cross_cutting_arms.
-- 'E-VOICE' is the baseline arm, so it is the default rather than NULL —
-- NULL would sort outside the composite unique index on some paths.
alter table public.simulations
  add column if not exists arm text not null default 'E-VOICE';

-- [{id, verdict, observed, expected, delta_id?}] — one element per assertion.
alter table public.simulations
  add column if not exists assertions_json jsonb;

-- score = passed / (passed + failed). `blocked-by-known-delta` and `error` are
-- excluded from the denominator (program §4, registry §0). NULL when the
-- denominator is 0 — a section where everything was blocked has no score, and
-- 0.0 would misreport that as total failure.
alter table public.simulations
  add column if not exists score real;

-- Roll-up verdict for the row. Vocabulary is fixed by
-- personas.json#verdict_vocabulary.
alter table public.simulations
  add column if not exists verdict text;

-- The score formula discards two of the four verdicts, so they have to be
-- carried separately or the run is unauditable: a 1.0 over two assertions with
-- forty-nine blocked is not a green section. Program §4 requires both counts be
-- "reported separately as two counts"; these are those counts.
alter table public.simulations
  add column if not exists passed_count integer;
alter table public.simulations
  add column if not exists failed_count integer;
alter table public.simulations
  add column if not exists blocked_count integer;
alter table public.simulations
  add column if not exists error_count integer;

-- Every KNOWN_DELTA_REGISTRY id this row tripped, e.g. '{D-04,D-10}'.
-- Registry §4.5 mandates frequency reporting ("D-04 x 47, D-10 x 22") and that
-- is not answerable by scanning jsonb across a night's rows.
alter table public.simulations
  add column if not exists delta_ids text[] not null default '{}';

-- Which spec document version produced these assertion ids. A verdict is only
-- interpretable against the spec revision that defined the assertion.
alter table public.simulations
  add column if not exists spec_version text;

-- Wall-clock bounds of this (section, persona, arm) walk. F05's blast-radius
-- query needs a run window, and `created_at` only marks when the row landed.
alter table public.simulations
  add column if not exists started_at timestamptz;
alter table public.simulations
  add column if not exists finished_at timestamptz;


-- ----------------------------------------------------------------------------
-- 3. Constrain the verdict vocabulary
-- ----------------------------------------------------------------------------

-- NOT VALID: the check is enforced on new rows without rescanning the table.
-- Prod has 0 rows so this is cosmetic there, but dev (rjlcyvwogmfmngemhbmn)
-- holds 787 judge-era rows with `verdict` NULL, and an unqualified ADD
-- CONSTRAINT would have to scan them. NULL passes the check either way; the
-- NOT VALID keeps the migration O(1) on both projects.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.simulations'::regclass
      and conname = 'simulations_verdict_vocab'
  ) then
    alter table public.simulations
      add constraint simulations_verdict_vocab
      check (verdict is null or verdict in ('pass', 'fail', 'blocked-by-known-delta', 'error'))
      not valid;
  end if;
end
$$;

-- score is a ratio; anything outside 0..1 is a harness bug, not a result.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.simulations'::regclass
      and conname = 'simulations_score_range'
  ) then
    alter table public.simulations
      add constraint simulations_score_range
      check (score is null or (score >= 0.0 and score <= 1.0))
      not valid;
  end if;
end
$$;


-- ----------------------------------------------------------------------------
-- 4. Judge-era NOT NULLs get defaults so the assertion writer can omit them
-- ----------------------------------------------------------------------------

-- `seed_label` and `bucket` are judge concepts with no analogue in a section
-- walk; `transcript_json` is written by the swarm but not on an early-abort
-- row (a G-gate ABORT still has to record *why* it aborted).
alter table public.simulations alter column seed_label      set default '';
alter table public.simulations alter column bucket          set default '';
alter table public.simulations alter column transcript_json set default '[]'::jsonb;


-- ----------------------------------------------------------------------------
-- 5. Indexes — the composite identity, and the three nightly report queries
-- ----------------------------------------------------------------------------

-- The real result identity. Replaces the dropped unique on run_id alone.
-- section_id is nullable for judge-era rows, so this is a partial index: it
-- constrains harness rows without forcing the old writer to supply a section.
create unique index if not exists simulations_result_identity_idx
  on public.simulations (run_id, section_id, persona_id, arm)
  where section_id is not null;

-- "how did section P4 do tonight" — the primary nightly read.
create index if not exists simulations_run_section_idx
  on public.simulations (run_id, section_id);

-- "which sections are red" across runs.
create index if not exists simulations_verdict_idx
  on public.simulations (verdict)
  where verdict is not null;

-- Registry §4.5 frequency reporting: unnest(delta_ids) group by 1.
create index if not exists simulations_delta_ids_idx
  on public.simulations using gin (delta_ids);


-- ----------------------------------------------------------------------------
-- 6. Documentation
-- ----------------------------------------------------------------------------

comment on column public.simulations.section_id is
  'Zero-bug program section: P0..P8, X1..X3. NULL on judge-era rows.';
comment on column public.simulations.arm is
  'Cross-cutting arm per personas.json#cross_cutting_arms: E-VOICE (baseline), E-CLICK, E-FALLBACK.';
comment on column public.simulations.assertions_json is
  'One element per assertion: {id, verdict, observed, expected, delta_id?}.';
comment on column public.simulations.score is
  'passed / (passed + failed). blocked-by-known-delta and error are excluded from the denominator. NULL when the denominator is 0.';
comment on column public.simulations.verdict is
  'Roll-up: pass | fail | blocked-by-known-delta | error.';
comment on column public.simulations.delta_ids is
  'KNOWN_DELTA_REGISTRY ids tripped by this row, for frequency reporting (registry §4.5).';
comment on column public.simulations.spec_version is
  'Version of the SPEC_<section>.md that defined these assertion ids.';


-- ============================================================================
-- POST-CONDITIONS (assert after apply)
--
--   -- the unique constraint is regrained, not merely dropped
--   select conname, pg_get_constraintdef(oid)
--   from pg_constraint where conrelid='public.simulations'::regclass and contype='u';
--   -- Expected: no row (uniqueness now lives in simulations_result_identity_idx)
--
--   select indexname from pg_indexes
--   where schemaname='public' and tablename='simulations' order by indexname;
--   -- Expected to include: simulations_result_identity_idx,
--   --   simulations_run_section_idx, simulations_verdict_idx,
--   --   simulations_delta_ids_idx
--
--   -- two rows under one run_id must now coexist, and a duplicate must not
--   begin;
--     insert into public.simulations (run_id, section_id, persona_id, arm, verdict)
--       values ('probe','P1','PER-01','E-VOICE','pass'),
--              ('probe','P1','PER-01','E-CLICK','pass'),
--              ('probe','P1','PER-02','E-VOICE','fail');
--     -- expect: 3 rows inserted
--     insert into public.simulations (run_id, section_id, persona_id, arm)
--       values ('probe','P1','PER-01','E-VOICE');
--     -- expect: ERROR duplicate key value violates simulations_result_identity_idx
--   rollback;
--
--   -- RLS survived (owned by 20260917150000, asserted here as a guard)
--   select relrowsecurity from pg_class where oid='public.simulations'::regclass;
--   -- Expected: t
-- ============================================================================
