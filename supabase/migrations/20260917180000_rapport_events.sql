-- ============================================================================
-- 20260917180000_rapport_events.sql
--
-- Repo path: supabase/migrations/20260917180000_rapport_events.sql
-- Target:    Supabase kmetmatfxdkrialwrnzj (tagalng-prod)
-- Spec:      LANA_MATURITY_MODEL_v2.md §7 (technical architecture), §4 (Axis A),
--            §5 (Axis B), §8 (analytics), §9 (open decisions)
-- Follows:   20260917170000_swarm_teardown (PR #126)
--
-- WHY
--   The maturity model has two ORTHOGONAL axes, and neither may be a column:
--
--     Axis A · product maturity   — what the user can do and has got. A RATCHET.
--                                   "you don't un-meet someone" (§4). Dormancy is
--                                   an overlay, never a downgrade.
--     Axis B · relationship depth — what Lana may ask and how she may speak.
--                                   BIDIRECTIONAL: earned per turn, and it decays
--                                   (§5 regression events).
--
--   §7 is explicit that stage must not be stored — "it drifts and can't be
--   replayed". Both axes are projections over an append-only event log. This
--   migration is that log.
--
--   The pattern is not new here. public.relationship_tier_events already does
--   exactly this for peer<->peer tiers; this mirrors it for user<->Lana.
--
-- WHAT THIS DELIBERATELY DOES NOT DO
--   * No get_relationship_depth(). The Axis-B fold needs a decay rate, and the
--     decay rate is §9 open decision 3 — unratified. Shipping an invented
--     constant would make a guess look like a derived read. SPEC_X1_MEMORY.md
--     D05 and SPEC_X3_HONESTY.md R08 say the same thing from the test side:
--     "Never synthesise a depth level." This migration does not either.
--   * No writer. The worker emits these rows; that is a separate PR.
--   * No backfill. There is no historical source for a transition event.
--     Inferring FIRST_VALUE retroactively out of 931 lana_messages would be
--     fabrication, and §8's own caveat ("at 29 users this is instrumentation,
--     not statistics") removes any reason to try.
--
-- SAFETY
--   * Purely additive: one table, four indexes, one function. Nothing existing is
--     altered, dropped, or rewritten.
--   * RLS is enabled WITH POLICIES in the same file. Enabling RLS and leaving zero
--     policies is deny-all-by-accident — the state 13 other tables are already in
--     (audited in PR #119 §3, four flagged as needing a recorded intent, one of
--     them circle_affiliations). This table does not join that list.
--   * Every FK names its ON DELETE behaviour explicitly. PR #126 found nine
--     ON DELETE NO ACTION edges into public.users, two of which silently abort the
--     entire anonymous-user sweep (places.created_by, events.host_id). This adds
--     none — see the per-column reasoning in §1 and the SWEEP probe in
--     docs/prs/PR15_rapport_events.md §5.
--   * Idempotent: every statement is `if not exists` / `or replace` guarded, and
--     the CHECK constraints go through `do $$ ... $$` existence guards because
--     `add constraint` has no `if not exists` form. Verified by running the whole
--     file twice inside one transaction.
--
-- ROLLBACK: docs/prs/PR15_rapport_events.md §7.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. The log
-- ----------------------------------------------------------------------------

create table if not exists public.rapport_events (
  id           uuid primary key default gen_random_uuid(),

  -- ON DELETE CASCADE, deliberately. The anonymous-user sweep
  -- (cleanup_stale_anonymous_users, and cleanup_swarm_run in PR #126) deletes
  -- from auth.users and relies on the cascade reaching every dependent row. A
  -- NO ACTION edge here would raise 23503 and abort the sweep for EVERY user,
  -- not just the one holding a rapport event — the exact defect PR #126 fixed.
  -- Matches rapport_gaps.user_id, circle_affiliations.user_id and
  -- relationship_tier_events.{user_low,user_high,viewer_user_id}.
  user_id      uuid not null references public.users (id) on delete cascade,

  -- ON DELETE SET NULL. A rapport event is a fact about the USER, not about the
  -- session; purging a session must not erase maturity history and must not
  -- block. (lana_sessions.user_id already cascades from users, so the
  -- user-delete path reaches this row through user_id regardless.)
  -- Matches rapport_gaps.opened_from_message_id.
  session_id   uuid references public.lana_sessions (id) on delete set null,

  -- ON DELETE SET NULL, same reasoning. lana_messages cascades from
  -- lana_sessions, so a session purge nulls this without touching the event.
  turn_id      uuid references public.lana_messages (id) on delete set null,

  -- The orthogonality, enforced. 'product' = Axis A, 'relationship' = Axis B.
  axis         text not null,

  -- §7's vocabulary. Constrained PER AXIS below: a product event on the
  -- relationship axis (or the reverse) is a category error, not a value.
  event_type   text not null,

  -- R0..R3, Axis B only. The depth tier the turn operated at (§6:
  -- "sensitivity(question) <= relationship_depth").
  sensitivity  smallint,

  -- Signed weight. Axis B is bidirectional — VOLUNTEERED is the strongest
  -- positive, ABANDONED a strong negative (§5). Axis A is a ratchet, so a
  -- negative delta there is rejected below.
  delta        real,

  -- Claim id, gap id, quote ref — whatever makes the event auditable later.
  -- NOT NULL DEFAULT '{}' so a fold never has to null-guard.
  evidence     jsonb not null default '{}'::jsonb,

  created_at   timestamptz not null default now()
);

comment on table public.rapport_events is
  'Append-only maturity log (LANA_MATURITY_MODEL_v2 §7). Two orthogonal axes: product '
  'maturity ratchets forward only; relationship depth moves both ways. Stage is NEVER '
  'stored — get_product_stage() folds this log. Service role writes only.';

comment on column public.rapport_events.axis is
  'product = Axis A (capability/outcomes, monotonic) | relationship = Axis B (disclosure/trust, bidirectional).';
comment on column public.rapport_events.event_type is
  'Axis A: IDENTIFIED FIRST_VALUE GROUNDED CONNECTED CONTRIBUTED. '
  'Axis B: VOLUNTEERED ANSWERED RECIPROCATED EDITED RETURNED SKIPPED DISMISSED ABANDONED.';
comment on column public.rapport_events.sensitivity is
  'R0-R3 depth tier the turn operated at. Axis B only — null on product events.';
comment on column public.rapport_events.delta is
  'Signed weight. Axis A is a ratchet: delta > 0 or null. Axis B may be negative (§5 regression events).';
comment on column public.rapport_events.evidence is
  'Provenance: {claim_id, gap_id, quote_ref, ...}. Defaults to {} so folds never null-guard.';


-- ----------------------------------------------------------------------------
-- 2. The constraints that make the two axes actually orthogonal
-- ----------------------------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.rapport_events'::regclass
      and conname = 'rapport_events_axis_check'
  ) then
    alter table public.rapport_events
      add constraint rapport_events_axis_check
      check (axis in ('product', 'relationship'));
  end if;

  -- The vocabulary is partitioned by axis. Without this, 'ABANDONED' could be
  -- written on the product axis and get_product_stage() would silently ignore
  -- it — a lost regression signal that reads as a clean funnel.
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.rapport_events'::regclass
      and conname = 'rapport_events_event_type_check'
  ) then
    alter table public.rapport_events
      add constraint rapport_events_event_type_check
      check (
        (axis = 'product' and event_type in (
            'IDENTIFIED', 'FIRST_VALUE', 'GROUNDED', 'CONNECTED', 'CONTRIBUTED'))
        or
        (axis = 'relationship' and event_type in (
            'VOLUNTEERED', 'ANSWERED', 'RECIPROCATED', 'EDITED', 'RETURNED',
            'SKIPPED', 'DISMISSED', 'ABANDONED'))
      );
  end if;

  -- §4: "Regression: none. Product maturity is a ratchet." A negative delta on
  -- Axis A is not a value, it is a bug in the writer. Reject it at the boundary.
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.rapport_events'::regclass
      and conname = 'rapport_events_product_ratchet_check'
  ) then
    alter table public.rapport_events
      add constraint rapport_events_product_ratchet_check
      check (axis <> 'product' or delta is null or delta > 0);
  end if;

  -- sensitivity is an Axis-B concept. It has no meaning on a product transition.
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.rapport_events'::regclass
      and conname = 'rapport_events_sensitivity_check'
  ) then
    alter table public.rapport_events
      add constraint rapport_events_sensitivity_check
      check (
        (sensitivity is null or sensitivity between 0 and 3)
        and (axis <> 'product' or sensitivity is null)
      );
  end if;
end
$$;


-- ----------------------------------------------------------------------------
-- 3. Indexes — the two folds, plus the two reads §8 asks to instrument first
-- ----------------------------------------------------------------------------

-- Both folds read "this user, this axis, in order".
create index if not exists rapport_events_user_axis_idx
  on public.rapport_events (user_id, axis, created_at desc);

-- §8: "Instrument first: FIRST_VALUE (the activation event) and ABANDONED (we
-- are building an anti-annoyance system with no annoyance telemetry)." Both are
-- cohort queries across users by event_type, which the index above cannot serve.
create index if not exists rapport_events_type_time_idx
  on public.rapport_events (event_type, created_at desc);

-- Turn-level provenance: "which events did this session produce".
create index if not exists rapport_events_session_idx
  on public.rapport_events (session_id, created_at)
  where session_id is not null;

-- The ratchet enforced at WRITE time rather than trusted at read time. A product
-- transition is a one-time stage crossing; a replayed FIRST_VALUE is a duplicate,
-- not a second activation, and `on conflict do nothing` makes the worker's emit
-- idempotent for free.
--   Deliberate scope note: this means the log cannot count "hosted 4 events".
--   That is correct. Repeat contribution belongs in §7's value ledger, not in the
--   transition log — conflating the two is what made the v1 model a checklist.
create unique index if not exists rapport_events_product_once_idx
  on public.rapport_events (user_id, event_type)
  where axis = 'product';


-- ----------------------------------------------------------------------------
-- 4. RLS — enabled WITH policies, in the same breath
-- ----------------------------------------------------------------------------

alter table public.rapport_events enable row level security;

-- Mirrors the pattern already in prod on rapport_gaps, latent_signals and
-- relationship_tier_events: an owner-scoped SELECT plus a deny-all ALL policy
-- covering every write path. Permissive policies OR together, so SELECT resolves
-- to (false OR user_id = auth.uid()) and INSERT/UPDATE/DELETE resolve to (false).
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'rapport_events'
      and policyname = 'rapport_events_select_own'
  ) then
    create policy rapport_events_select_own on public.rapport_events
      for select to authenticated
      using (user_id = auth.uid());
  end if;

  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'rapport_events'
      and policyname = 'rapport_events_no_client_write'
  ) then
    create policy rapport_events_no_client_write on public.rapport_events
      for all to authenticated using (false) with check (false);
  end if;
end
$$;

-- Defence in depth, per PR #119: if RLS is ever toggled off, the table still is
-- not client-writable and anon still cannot read it.
revoke insert, update, delete, truncate on public.rapport_events from anon, authenticated;
revoke select on public.rapport_events from anon;


-- ----------------------------------------------------------------------------
-- 5. Axis A as a derived read — and only Axis A
-- ----------------------------------------------------------------------------

-- Monotonic BY CONSTRUCTION: the stage is the highest transition ever observed,
-- so events arriving out of order, replayed, or backdated cannot move it
-- downward. That IS the ratchet — a property of the fold, not something the
-- writer has to be careful about.
--
-- Dormancy is NOT modelled here. §4: "Dormancy is an overlay (P{n}-quiet after no
-- session in N days), never a downgrade." The overlay is a presentation concern
-- and N is undecided; deriving it here would bake a constant into the schema.
create or replace function public.get_product_stage(p_user_id uuid)
returns text
language sql
stable
security definer
set search_path = public
as $$
  select case
    when bool_or(event_type = 'CONTRIBUTED') then 'P5'
    when bool_or(event_type = 'CONNECTED')   then 'P4'
    when bool_or(event_type = 'GROUNDED')    then 'P3'
    when bool_or(event_type = 'FIRST_VALUE') then 'P2'
    when bool_or(event_type = 'IDENTIFIED')  then 'P1'
    else 'P0'
  end
  from public.rapport_events
  where user_id = p_user_id
    and axis = 'product';
$$;

comment on function public.get_product_stage(uuid) is
  'Axis A projection (LANA_MATURITY_MODEL_v2 §4): P0 Arrived .. P5 Weaving, folded from '
  'rapport_events. Monotonic — the highest transition ever observed. Never stored. '
  'Service role only (§9 open decision 4: the funnel is internal).';

-- §9 open decision 4 recommends "funnel internal, profile user-facing". Until that
-- is ratified the stage is not exposed to a client role.
revoke all on function public.get_product_stage(uuid) from public, anon, authenticated;
grant execute on function public.get_product_stage(uuid) to service_role;


-- ============================================================================
-- POST-CONDITIONS (assert after apply)
--
--   select to_regclass('public.rapport_events');
--   -- Expected: public.rapport_events
--   -- This is the SPEC_X1_MEMORY / SPEC_X3_HONESTY "RAPEVT" pre-flight probe.
--   -- It expects null today; after this lands it must be non-null.
--
--   select relrowsecurity from pg_class where oid = 'public.rapport_events'::regclass;
--   -- Expected: t
--
--   select policyname, cmd from pg_policies
--   where schemaname='public' and tablename='rapport_events' order by policyname;
--   -- Expected: rapport_events_no_client_write (ALL), rapport_events_select_own (SELECT)
--   -- Expected COUNT: 2. Zero would be the circle_affiliations mistake.
--
--   select conname, confdeltype from pg_constraint
--   where conrelid='public.rapport_events'::regclass and contype='f';
--   -- Expected: user_id 'c' (cascade), session_id 'n' (set null), turn_id 'n' (set null)
--   -- NONE may be 'a' (no action) — that is the PR #126 defect.
--
--   select public.get_product_stage('00000000-0000-0000-0000-000000000000'::uuid);
--   -- Expected: P0 — no events folds to the floor, not to null.
--
--   -- negative cases, all must raise:
--   --   axis='product',      event_type='ABANDONED' -> 23514 event_type_check
--   --   axis='product',      delta=-1               -> 23514 product_ratchet_check
--   --   axis='product',      sensitivity=2          -> 23514 sensitivity_check
--   --   axis='relationship', sensitivity=9          -> 23514 sensitivity_check
--   --   two FIRST_VALUE rows for one user           -> 23505 product_once_idx
-- ============================================================================
