-- Lana value recommendations: impression log for heterogeneous candidates.
-- Records what Lana surfaced so we can learn from shown/dismissed/accepted/converted outcomes.

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

create policy "recommendation_impressions_select_own"
  on public.recommendation_impressions for select
  to authenticated
  using (user_id = auth.uid());

create policy "recommendation_impressions_no_client_write"
  on public.recommendation_impressions for all
  to authenticated
  using (false)
  with check (false);
