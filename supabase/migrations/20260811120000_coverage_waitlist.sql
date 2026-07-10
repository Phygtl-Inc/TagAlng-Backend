-- Out-of-coverage waitlist — capture demand from ZIPs we have no block for yet.
--
-- QA (2026-07-08): 12/12 metro moms hit a dead end — a valid ZIP with no block got
-- "try 32827 for Lake Nona" (an Orlando ZIP suggested to a Manhattan mom), and 7/12
-- times the next turn re-asked for the ZIP she'd just given. The honest reply is now
-- "we're not on your block yet — want me to text you the day we arrive?", and an accept
-- lands here so the promise is keepable.
--
-- One row per (user, zip): tapping "Join the waitlist" twice must not double-book, but
-- the same user may genuinely wait on two ZIPs (home + parents' place). looking_for
-- keeps what she was searching when she hit the wall ("walk buddy") — the day we open
-- her block, the first thing Lana offers should be the thing she wanted. notify gates
-- the launch-day text; founding_interest marks users who want to help seed the block.
-- Service-role only: the worker writes; no anon/authenticated access.
create table if not exists public.coverage_waitlist (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete set null,
  zip text not null,
  looking_for text,
  notify boolean not null default true,
  founding_interest boolean not null default false,
  created_at timestamptz not null default now()
);

-- Dedupe target for the worker's upsert (NULL user_id rows never conflict — fine, we
-- always pass the guest/user id when we have one).
create unique index if not exists coverage_waitlist_user_zip_uidx
  on public.coverage_waitlist (user_id, zip);
create index if not exists coverage_waitlist_zip_idx on public.coverage_waitlist (zip);
create index if not exists coverage_waitlist_created_idx on public.coverage_waitlist (created_at desc);

alter table public.coverage_waitlist enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
