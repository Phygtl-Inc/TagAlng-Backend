-- Pending signal asks (tip/swap/help "looking/sharing" requests) across a guest ->
-- existing-account login.
--
-- Mirrors pending_meet_seeks. When a guest makes an ask Lana must post to their block
-- ("can you recommend me a babysitter?" → tip_seek), the verify gate stashes it in the
-- session (signal_pending) so a fresh SIGNUP saves it post-verify in the same session.
-- But logging into an account they ALREADY have swaps the JWT and the frontend forces a
-- brand-new session — orphaning the guest session and silently losing the ask.
--
-- We stash the ask keyed by the DESTINATION account at email-entry time, then the
-- account's next Lana session recovers and saves it. One row per user (latest ask wins).
-- Service-role only — the worker writes/reads/clears it; no anon/authenticated access.
create table if not exists public.pending_signal_asks (
  user_id uuid primary key references public.users(id) on delete cascade,
  ask jsonb not null,
  created_at timestamptz not null default now()
);

alter table public.pending_signal_asks enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
