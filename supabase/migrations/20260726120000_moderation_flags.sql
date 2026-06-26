-- Moderation flags — inappropriate / abusive messages Lana refused.
--
-- DISTINCT from feature_requests: that table is legit-but-unsupported demand we may build
-- and notify on. This table is content Lana declined on safety grounds (NSFW/sexual,
-- harassment/abuse, hate, illegal). It is NEVER surfaced as product demand and carries no
-- "we'll add it" promise. Captured so trust & safety can review patterns and spot repeat
-- offenders. An append log — one row per refused message.
--
-- Service-role only — the worker writes; no anon/authenticated access (sensitive content).
create table if not exists public.moderation_flags (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete set null,
  block_id text references public.blocks(id) on delete set null,
  message text not null,
  kind text,
  severity text,
  created_at timestamptz not null default now()
);

create index if not exists moderation_flags_user_idx on public.moderation_flags (user_id);
create index if not exists moderation_flags_created_idx on public.moderation_flags (created_at desc);

alter table public.moderation_flags enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
