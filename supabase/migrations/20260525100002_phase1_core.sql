-- TagAlng Phase 1: blocks, waitlist, cohorts mirror, audit

-- Block lifecycle: waitlist → racing → live → day_zero (thresholds enforced in RPC Phase 2+)
create type public.block_state as enum (
  'waitlist',
  'racing',
  'live',
  'day_zero'
);
create table public.blocks (
  id text primary key, -- H3 cell id (res 10/11)
  cluster_id text not null default 'lake-nona',
  state public.block_state not null default 'waitlist',
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
comment on table public.blocks is '5-minute-walk unit (H3). Vicinity is the block, not radius.';
-- Mirror of cohorts.yaml for SQL validation (seed from yaml in CI or manual)
create table public.cohorts (
  id text primary key,
  label text not null,
  parent_id text references public.cohorts (id),
  kind text not null default 'cohort' check (kind in ('cohort', 'sport_subtype')),
  created_at timestamptz not null default now()
);
create table public.waitlist_signups (
  id uuid primary key default gen_random_uuid(),
  phone text,
  city text,
  declared_cohorts text[] not null default '{}',
  sport_sub text,
  candidate_block_id text not null references public.blocks (id),
  inbound_ref text,
  inbound_cohorts text[] default '{}',
  recaptcha_verified boolean not null default false,
  created_at timestamptz not null default now(),
  constraint waitlist_cohorts_not_empty check (cardinality(declared_cohorts) >= 1)
);
create index waitlist_signups_block_id_idx on public.waitlist_signups (candidate_block_id);
create index waitlist_signups_created_at_idx on public.waitlist_signups (created_at desc);
-- Atlas: materialized count per block (updated by trigger)
create table public.block_waitlist_counts (
  block_id text primary key references public.blocks (id) on delete cascade,
  signup_count int not null default 0,
  updated_at timestamptz not null default now()
);
create table public.audit_log (
  id uuid primary key default gen_random_uuid(),
  actor_id uuid,
  action text not null,
  resource_type text,
  resource_id text,
  metadata jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index audit_log_created_at_idx on public.audit_log (created_at desc);
-- Analytics events (Phase 1 taxonomy — expand in Phase 2)
create table public.analytics_events (
  id uuid primary key default gen_random_uuid(),
  event_name text not null,
  user_id uuid,
  session_id text,
  properties jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index analytics_events_name_created_idx
  on public.analytics_events (event_name, created_at desc);
-- Seed cohorts (v1 — keep in sync with /cohorts.yaml)
insert into public.cohorts (id, label, parent_id, kind) values
  ('parents', 'Parents', null, 'cohort'),
  ('sports', 'Sports', null, 'cohort'),
  ('faith', 'Faith & community', null, 'cohort'),
  ('sober', 'Sober / recovery', null, 'cohort'),
  ('runner', 'Running & fitness', null, 'cohort'),
  ('newcomer', 'New to the area', null, 'cohort'),
  ('professional', 'Professional network', null, 'cohort'),
  ('creative', 'Creative & makers', null, 'cohort'),
  ('volunteer', 'Volunteer & civic', null, 'cohort'),
  ('basketball', 'Basketball', 'sports', 'sport_subtype'),
  ('soccer', 'Soccer', 'sports', 'sport_subtype'),
  ('tennis', 'Tennis', 'sports', 'sport_subtype'),
  ('pickleball', 'Pickleball', 'sports', 'sport_subtype'),
  ('running', 'Running group', 'sports', 'sport_subtype'),
  ('cycling', 'Cycling', 'sports', 'sport_subtype'),
  ('swimming', 'Swimming', 'sports', 'sport_subtype'),
  ('other', 'Other sport', 'sports', 'sport_subtype')
on conflict (id) do nothing;
-- Placeholder Lake Nona blocks — replace with dossier H3 ids when known
insert into public.blocks (id, cluster_id, state, display_name) values
  ('8a2a1072b59ffff', 'lake-nona', 'waitlist', 'Lake Nona — Block A (placeholder)'),
  ('8a2a1072b5affff', 'lake-nona', 'waitlist', 'Lake Nona — Block B (placeholder)')
on conflict (id) do nothing;
insert into public.block_waitlist_counts (block_id, signup_count)
select id, 0 from public.blocks
on conflict (block_id) do nothing;
-- updated_at helper
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;
create trigger blocks_updated_at
before update on public.blocks
for each row execute function public.set_updated_at();
