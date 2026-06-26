-- Pending meet seeks across a guest -> existing-account login.
--
-- Mirrors pending_event_drafts. When a guest builds a "looking for a meet/playgroup" seek in
-- chat, taps "Start listening for me", and then verifies by logging into an account they
-- ALREADY have, the JWT swaps to that account and the frontend forces a brand-new session
-- (force_new=true). That orphans the guest session and its ready look_seek_pending, so the
-- seek the guest just confirmed is silently lost (it is never saved, never shows in radar).
--
-- We stash the seek draft keyed by the DESTINATION account at email-entry time, then the
-- account's next Lana session recovers and saves it. One row per user (latest build wins).
-- Service-role only — the worker writes/reads/clears it; no anon/authenticated access.
--
-- Note: a fresh SIGNUP does not need this — signup links the email onto the same anonymous
-- user, so the session context (and look_seek_pending) survives and the in-session
-- post-verify save fires. This table only rescues the existing-account LOGIN case.
create table if not exists public.pending_meet_seeks (
  user_id uuid primary key references public.users(id) on delete cascade,
  seek jsonb not null,
  created_at timestamptz not null default now()
);

alter table public.pending_meet_seeks enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.
