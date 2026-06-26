-- Out-of-scope / unsupported feature requests captured from Lana chat.
--
-- When a user asks Lana to do something TagAlng has no feature for (deliver food, book a
-- taxi, do taxes, etc.), the discovery router classifies it as system.out_of_scope. Lana
-- declines gracefully AND logs the ask here, so the team can (a) see real demand and
-- (b) notify the user later if/when the capability ships ("I've added it and we'll let you
-- know" — the promise is only keepable because user_id is captured).
--
-- One row per ask (an append log, not a per-user upsert) — the same user may surface many
-- distinct unmet needs, and the timeline of demand is itself the signal. Service-role only:
-- the worker writes; no anon/authenticated access.
create table if not exists public.feature_requests (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete set null,
  block_id text references public.blocks(id) on delete set null,
  request_text text not null,
  category text,
  created_at timestamptz not null default now(),
  notified_at timestamptz
);

create index if not exists feature_requests_user_idx on public.feature_requests (user_id);
create index if not exists feature_requests_created_idx on public.feature_requests (created_at desc);

alter table public.feature_requests enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
