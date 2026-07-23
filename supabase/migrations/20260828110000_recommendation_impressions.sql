-- Lana value recommendations: impression log for heterogeneous candidates.
-- Records what Lana surfaced so we can learn from shown/dismissed/accepted/converted outcomes.

-- Name-collision repair: 20260728_lana_latent_intent §4 also created a table called
-- recommendation_impressions (the latent-intent ranker ledger — different shape, keyed by
-- surfaced_at, no writers yet). On databases where that migration ran first, move it to its
-- new name (latent_suggestion_impressions, matching the edited 20260728 file) so this
-- migration can claim the name the live recommendations.py insert path expects.
do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name = 'recommendation_impressions'
      and column_name = 'surfaced_at'
  ) and not exists (
    select 1 from information_schema.tables
    where table_schema = 'public'
      and table_name = 'latent_suggestion_impressions'
  ) then
    alter table public.recommendation_impressions rename to latent_suggestion_impressions;
    alter index if exists public.recommendation_impressions_user_time_idx
      rename to latent_suggestion_impressions_user_time_idx;
    alter index if exists public.recommendation_impressions_suggestion_idx
      rename to latent_suggestion_impressions_suggestion_idx;
  end if;
end $$;

create table if not exists public.recommendation_impressions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  session_id uuid references public.lana_sessions (id) on delete set null,
  block_id text references public.blocks (id) on delete set null,
  recommendation_type text not null
    check (recommendation_type in ('neighbor', 'event', 'local_signal')),
  candidate_user_id uuid references public.users (id) on delete set null,
  event_id uuid references public.events (id) on delete set null,
  signal_id uuid,
  score real not null default 0,
  reason_codes text[] not null default '{}',
  suggested_action text not null,
  safe_reason text,
  query text,
  surface text not null default 'lana_chat',
  status text not null default 'shown'
    check (status in ('shown', 'viewed', 'dismissed', 'accepted', 'converted')),
  converted_action_id uuid,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint recommendation_impressions_candidate_present check (
    candidate_user_id is not null
    or event_id is not null
    or signal_id is not null
  )
);

comment on table public.recommendation_impressions is
  'Feedback loop for Lana-mediated recommendations across neighbors, events, and local signals.';

create index if not exists recommendation_impressions_user_created_idx
  on public.recommendation_impressions (user_id, created_at desc);

create index if not exists recommendation_impressions_session_idx
  on public.recommendation_impressions (session_id, created_at desc);

create index if not exists recommendation_impressions_type_idx
  on public.recommendation_impressions (recommendation_type, created_at desc);

alter table public.recommendation_impressions enable row level security;

-- drop-then-create so a rerun after a mid-file failure (it happened once) can't trip here.
drop policy if exists "recommendation_impressions_select_own" on public.recommendation_impressions;
create policy "recommendation_impressions_select_own"
  on public.recommendation_impressions for select
  to authenticated
  using (user_id = auth.uid());

drop policy if exists "recommendation_impressions_no_client_write" on public.recommendation_impressions;
create policy "recommendation_impressions_no_client_write"
  on public.recommendation_impressions for all
  to authenticated
  using (false)
  with check (false);
