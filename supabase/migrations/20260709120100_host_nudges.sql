-- Supply engine №2 · demand-triggered host nudge ledger.
--
-- When >= 3 moms on a block saved the same unmet need (listening meet_seek signals),
-- Lana nudges the best candidate host: "Three moms near you want a weekday park morning.
-- Want to host it? I'll handle invites, RSVPs, and reminders." This table records every
-- nudge sent so we can enforce the cap (one nudge per host per 7 days) and audit which
-- demand pockets we acted on. Written by the worker's service role
-- (app/host_nudge.py · scripts/emit_host_nudges.py).

create table if not exists public.host_nudges (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  block_id text not null references public.blocks (id) on delete cascade,
  need_key text not null,
  need_label text not null,
  signal_count int not null check (signal_count > 0),
  created_at timestamptz not null default now()
);

comment on table public.host_nudges is
  'One row per demand-triggered "want to host it?" nudge Lana sent. need_key is the '
  'normalized grouping key of the unmet need; signal_count how many neighbors wanted it '
  'when the nudge fired. Last-nudged lookups drive the one-per-host-per-7-days cap.';

-- Cap lookup: most recent nudge per host.
create index if not exists host_nudges_user_recent_idx
  on public.host_nudges (user_id, created_at desc);

-- Ops/audit: what fired on a block lately.
create index if not exists host_nudges_block_recent_idx
  on public.host_nudges (block_id, created_at desc);

alter table public.host_nudges enable row level security;

-- Members may see nudges addressed to them; only the service role writes.
drop policy if exists host_nudges_select_own on public.host_nudges;
create policy host_nudges_select_own on public.host_nudges
  for select to authenticated
  using (user_id = auth.uid());
