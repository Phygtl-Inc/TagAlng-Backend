# PR13 · `simulations` harness extension — the zero-bug program's results sink

**Target repo:** `Phygtl-Inc/TagAlng-Backend`
**Migration:** `supabase/migrations/20260917160000_simulations_harness.sql`
**Environment:** Supabase `kmetmatfxdkrialwrnzj` (**tagalng-prod**)
**Author:** harness build · 2026-07-30
**Status:** specification — verified against prod inside `begin; … rollback;`, **nothing applied, no migration run**
**Stacks on:** **PR #119** (`20260917150000_rls_hardening`) — must land first. See §5.
**Program refs:** `LANA_ZERO_BUG_PROGRAM_FINAL.md` §4, §5 · `LANA_AUTONOMY_GAPS.md` gap 2 · `HANDOVER_CLAUDE_CODE.md` T2

---

## 1. What this is for

`LANA_AUTONOMY_GAPS.md` gap 2, verbatim: *"Without this the swarm has nowhere to put a verdict and
every run's output lives only in a chat transcript."* This migration is that sink.

The overnight swarm walks 9 personas × 12 sections × up to 3 arms and emits a verdict per
assertion. `public.simulations` is the table that has to hold the result. It was not built for
this shape and one existing constraint makes the shape impossible.

---

## 2. The blocker: `run_id` is unique

```
simulations_run_id_key  UNIQUE (run_id)
```

Verified on prod. A `run_id` can therefore hold **exactly one row**.

The program's reporting unit (§4, restated identically in `SPEC_P0_SIGNUP.md` §SCORE and
`SPEC_P1_LANGUAGE.md` §SCORE) is:

> one row in `simulations` per (`section_id`, `persona_id`, `arm`)

all sharing one `run_id`. A single night is 9 personas × 1 section × 2 arms = 18 rows minimum
under one `run_id`. **The first insert would succeed and the next seventeen would fail with a
unique violation.**

The uniqueness is on the wrong grain: it identifies a *run* when it needs to identify a *result*.
This migration regrains it.

| | before | after |
|---|---|---|
| uniqueness | `UNIQUE (run_id)` | `unique index (run_id, section_id, persona_id, arm) where section_id is not null` |
| rows per run | 1 | unbounded |
| judge-era rows | unaffected | unaffected (`section_id is null` → outside the partial index) |

The partial predicate matters: judge-era rows have no `section_id`, and without `where section_id
is not null` every one of them would collide on `(run_id, null, persona_id, 'E-VOICE')`.

---

## 3. Columns added

The four named in the handover, plus four groups the program's own definitions require. `persona_id`
and `git_sha` were already present and are **not** re-added.

| column | type | why |
|---|---|---|
| `section_id` | `text` | `P0`..`P8`, `X1`..`X3`. Named in the handover. |
| `assertions_json` | `jsonb` | `[{id, verdict, observed, expected, delta_id?}]`. Named in the handover. |
| `score` | `real` | `passed / (passed + failed)`. Named in the handover. |
| `verdict` | `text` | Roll-up. Named in the handover. |
| `arm` | `text not null default 'E-VOICE'` | The reporting unit is per-**arm** (§4). Without it, `E-CLICK` and `E-VOICE` for one persona are the same row. Defaults to the baseline arm rather than NULL so it can sit inside a unique index. |
| `passed_count` `failed_count` `blocked_count` `error_count` | `integer` | §4 requires blocked and error be *"reported separately as two counts"*. They are excluded from the score denominator, so a `1.0` over two assertions with forty-nine blocked is indistinguishable from a real green **unless the counts are carried**. `SPEC_P1_LANGUAGE.md` §SCORE goes further: *"A run reporting zero blocked assertions is itself suspect."* That check needs `blocked_count` to be queryable. |
| `delta_ids` | `text[] not null default '{}'` | `KNOWN_DELTA_REGISTRY.md` §4.5 mandates frequency reporting — *"D-04 × 47, D-10 × 22, D-12 × 9"* — and calls that count *"the argument for what to fix next."* Not answerable by scanning `jsonb` across a night. GIN-indexed. |
| `spec_version` | `text` | An assertion id (`A07`, `M04`) only means something against the spec revision that defined it. Without this, re-reading last week's run is guesswork. |
| `started_at` `finished_at` | `timestamptz` | P0 `F05` (blast radius) and P1 `C06` both query `where created_at between :run_start and :run_end`. `created_at` records when the row landed, not the window the walk covered. |

### Constraints

- `simulations_verdict_vocab` — `verdict in ('pass','fail','blocked-by-known-delta','error')`, matching
  `personas.json#verdict_vocabulary` exactly. Verified to reject `'blocked'`, the obvious
  truncation of `blocked-by-known-delta`; silently accepting it would drop the row out of every
  registry-frequency query.
- `simulations_score_range` — `score between 0 and 1`.

Both are `NOT VALID`: enforced on new rows, no table scan. Prod has 0 rows so this is cosmetic
there, but **dev `rjlcyvwogmfmngemhbmn` holds 787 judge-era rows** and an unqualified
`ADD CONSTRAINT` would scan them all.

### `score` is nullable on purpose

`passed / (passed + failed)` has a zero denominator when every assertion was blocked. `0.0` would
report that as total failure; `NULL` reports it as unscored. On the current prod build, block `C` of
P1 is *entirely* blocked by D-12 — that case is today's reality, not a hypothetical.

### Judge-era `NOT NULL`s get defaults, not relaxation

`seed_label`, `bucket`, `transcript_json` are `NOT NULL` and are judge concepts with no analogue in
a section walk. They stay `NOT NULL`; they gain defaults (`''`, `''`, `'[]'::jsonb`). The existing
judge writer is untouched, the assertion writer may omit them, and a gate-ABORT row can still be
recorded with no transcript. Verified: an insert naming none of the three succeeds.

---

## 4. Verification — run against prod, rolled back

Every statement below ran on `kmetmatfxdkrialwrnzj` inside `begin; … rollback;`. **Nothing was
applied.** A `create table` probe confirmed rollback semantics before any of it.

**Idempotency** — the whole file, twice, in one transaction: no error.

**Multi-row per `run_id`** (the point of the migration), inserting three rows and naming none of the
judge-era `NOT NULL` columns:

| probe | result |
|---|---|
| `rows_under_one_run_id` | `3` ← **impossible before this migration** |
| `uniq_constraints_left` | `(none)` — uniqueness now lives in the partial index |
| `new_indexes` | all four created |
| `delta_freq` | `D-04=2, D-12=1` ← registry §4.5 reporting works |
| `defaults_applied` | `[][][[]]` — the three defaults filled in |

**Negative cases** — both correctly rejected:

```
insert … ('probe','P1','PER-01','E-VOICE') twice
  → ERROR 23505 duplicate key value violates "simulations_result_identity_idx"
     DETAIL: Key (run_id, section_id, persona_id, arm)=(probe, P1, PER-01, E-VOICE) already exists.

insert … verdict = 'blocked'
  → ERROR 23514 violates check constraint "simulations_verdict_vocab"
```

---

## 5. Ordering — this stacks on #119, and it matters

`20260917150000_rls_hardening` (PR #119) is what enables RLS on `simulations` and revokes anon DML.
This migration adds **no policy and no grant**; it inherits them.

Applied in the wrong order, the table gains columns for verbatim user utterances while `anon` still
holds `SELECT, INSERT, UPDATE, DELETE, TRUNCATE`. Gate **G4** in the program is exactly this, and
`SPEC_P0_SIGNUP.md` §PRE-FLIGHT makes it an **ABORT** condition rather than a warning.

`admin_allowlist` has **0 rows** (re-verified today). #119's read policy is fail-closed, so until
someone is enrolled nobody can read the results through the intended path. **Enrolling an admin is
part of G4, not a follow-up.**

Migration slot `20260917160000` sits after the four already claimed by open PRs
(`…120000` #121, `…130000` #122, `…140000` #124, `…150000` #119) so there is no collision.

---

## 6. Rollback

```sql
begin;
  drop index if exists public.simulations_result_identity_idx;
  drop index if exists public.simulations_run_section_idx;
  drop index if exists public.simulations_verdict_idx;
  drop index if exists public.simulations_delta_ids_idx;

  alter table public.simulations drop constraint if exists simulations_verdict_vocab;
  alter table public.simulations drop constraint if exists simulations_score_range;

  alter table public.simulations alter column seed_label      drop default;
  alter table public.simulations alter column bucket          drop default;
  alter table public.simulations alter column transcript_json drop default;

  alter table public.simulations
    drop column if exists section_id,
    drop column if exists arm,
    drop column if exists assertions_json,
    drop column if exists score,
    drop column if exists verdict,
    drop column if exists passed_count,
    drop column if exists failed_count,
    drop column if exists blocked_count,
    drop column if exists error_count,
    drop column if exists delta_ids,
    drop column if exists spec_version,
    drop column if exists started_at,
    drop column if exists finished_at;

  -- only if no run has written two rows under one run_id yet
  alter table public.simulations add constraint simulations_run_id_key unique (run_id);
commit;
```

⚠️ The last statement fails once any real run exists — by design. After a single night, restoring
`UNIQUE (run_id)` is data loss, and the failure is the guard. Drop that line and leave `run_id`
non-unique.

---

## 7. What this does not do

- **No RLS or grant changes.** Owned by #119.
- **No teardown.** `cleanup_swarm_run` is PR14 (handover T3); nothing runs against prod until it
  exists and is proven on dev.
- **No writer.** The harness in `tools/swarm/` writes these rows as `service_role`.
- **Judge-era columns are untouched.** `scores_json`, `weighted_score`, `judge_summary`,
  `hitl_status`, `tim_verdict`, `tim_note`, `sft_eligible`, `sft_messages` keep their meaning. The
  two harnesses coexist in one table, distinguished by `section_id is null`.

---

## 8. Reviewer checklist

- [ ] #119 has landed and `admin_allowlist` has ≥ 1 row (**G4**)
- [ ] Migration slot `20260917160000` is still free at merge time
- [ ] `select count(*) from public.simulations` — confirm the partial-index predicate is right for whatever judge-era rows exist by then
- [ ] Post-conditions at the foot of the `.sql` re-run green after apply
