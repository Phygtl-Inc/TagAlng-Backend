-- memory · user_constraints — durable availability/need constraints per user.
-- QA 2026-07-08: the "I can only do evenings after 6 or weekends" mom was shown
-- 10 AM coffee mornings; the constraint was never stored, so nothing could filter.
--
-- One ACTIVE row per (user, kind): restating a constraint replaces the prior one
-- (app-side upsert on user_id+kind, service role — see app/constraints.py).
-- The structured jsonb `value` is what the app-side event filter reads:
--   availability_time → {"windows":[{"days":"any"|"weekday"|"weekend",
--                        "start_minute":int|null,"end_minute":int|null}],"label":text}
--   kid_age           → {"min_years":int,"max_years":int,"label":text}
--
-- Why a dedicated table (and not an existing store):
--   * user_identity_claims is label-text only — no structured payload a filter can
--     read, and a constraint is not an identity thread to embed/peer-match on.
--   * latent_signals is an append-only observation firehose ("collect, don't
--     surface"), not a current-state store read on every turn.
-- Conventions mirror latent_signals / pending_event_drafts: service-role writes,
-- RLS select-own, no client writes.

create table if not exists public.user_constraints (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  kind text not null check (kind in ('availability_time', 'kid_age')),
  value jsonb not null default '{}'::jsonb,
  label text not null,                          -- human phrasing ("evenings after 6pm or weekends")
  source_quote text,                            -- the user line it was captured from
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_constraints_value_is_object check (jsonb_typeof(value) = 'object'),
  constraint user_constraints_user_kind_unique unique (user_id, kind)
);

comment on table public.user_constraints is
  'Durable availability/need constraints (one active row per user+kind). Captured '
  'deterministically from chat; applied as hard filters to every event result set.';

alter table public.user_constraints enable row level security;

create policy user_constraints_select_own on public.user_constraints
  for select to authenticated
  using (user_id = auth.uid());

create policy user_constraints_no_client_write on public.user_constraints
  for all to authenticated using (false) with check (false);
