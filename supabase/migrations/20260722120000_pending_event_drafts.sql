-- Pending event drafts across a guest -> existing-account login.
--
-- When a guest builds an event in chat and then verifies by logging into an account
-- they ALREADY have, the JWT swaps to that account and the frontend forces a brand-new
-- session (force_new=true). That orphans the guest session and its in-progress event
-- draft, so the finished event is silently lost.
--
-- We stash the host context (draft + the "ready to publish" flags) keyed by the
-- DESTINATION account at email-entry time, then the account's next Lana session recovers
-- and publishes it. One row per user (latest build wins). Service-role only — the worker
-- writes/reads/clears it; no anon/authenticated access.
create table if not exists public.pending_event_drafts (
  user_id uuid primary key references public.users(id) on delete cascade,
  host_ctx jsonb not null,
  created_at timestamptz not null default now()
);

alter table public.pending_event_drafts enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
