-- Directed recommendation asks — who Lana actually reached, so the copy can say so.
--
-- Until now a tip_seek was undirected: one row in local_signals, matched against tips that
-- ALREADY existed, and nobody contacted. That is why the receipt ("I asked your neighbors
-- nearby… I'll text you") was false twice over — no ask went out, and there is no SMS.
--
-- This table is the record of a real, narrow outreach: the ≤3 neighbors Lana judged able
-- to answer THIS ask. It exists for three jobs, and every one of them is a predicate on
-- this table rather than a new subsystem:
--
--   1. never ask the same person twice for the same ask   -> unique (signal_id, recipient)
--   2. never make one helpful neighbor the block's answering service -> cooldown window
--   3. stop asking someone who never answers             -> unanswered-streak lookback
--
-- Rotation falls out of (2) for free: this week's best-scoring neighbor is excluded next
-- week, so the next-best gets a turn instead of one person absorbing every ask.
--
-- Access is service-role only (RLS on, no policies — the deny-by-default pattern already
-- used by pending_signal_asks). Nothing on the read path needs these rows: the asker is
-- told a COUNT, never who was asked, and the recipient learns of it by email. Keeping the
-- roster unreadable is the privacy property that lets Lana ask on someone's behalf at all.

create table if not exists public.tip_ask_recipients (
  id uuid primary key default gen_random_uuid(),
  signal_id uuid not null references public.local_signals(id) on delete cascade,
  asker_user_id uuid not null references public.users(id) on delete cascade,
  recipient_user_id uuid not null references public.users(id) on delete cascade,
  -- Lana's own one-line justification for picking them, in her words, kept so a future
  -- "why did you ask me?" can be answered truthfully instead of reconstructed.
  reason text,
  -- queued -> sent (email away) | failed (send errored) | answered (they posted a tip)
  status text not null default 'queued'
    check (status in ('queued', 'sent', 'failed', 'answered')),
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  answered_at timestamptz,
  -- Job (1). Also the index that serves every per-ask lookup, signal_id leading.
  unique (signal_id, recipient_user_id)
);

-- Jobs (2) and (3): "was this person asked recently / did they answer the last few times".
-- Composite and descending because the query is always one recipient, newest first.
create index if not exists tip_ask_recipients_recipient_recent_idx
  on public.tip_ask_recipients (recipient_user_id, created_at desc);

-- FK index for the asker side (cascade deletes + "what have I asked" reads).
create index if not exists tip_ask_recipients_asker_idx
  on public.tip_ask_recipients (asker_user_id);

alter table public.tip_ask_recipients enable row level security;
-- No policies on purpose: RLS denies anon/authenticated by default; service_role bypasses.

comment on table public.tip_ask_recipients is
  'The neighbors Lana actually emailed about one tip_seek. Service-role only: the asker is '
  'told how many were asked, never which people.';

-- ---------------------------------------------------------------------------
-- Opt-out. One tap in the email, permanent, checked before every other rule.
-- ---------------------------------------------------------------------------
-- Separate table rather than a status value, because a mute is a property of the PERSON
-- and outlives any single ask. Deliberately not a preferences blob: a one-row-per-user
-- table is the cheapest thing that can be checked in the candidate filter, and the only
-- honest way to offer "don't ask me about this" in the mail we send.

create table if not exists public.tip_ask_mutes (
  user_id uuid primary key references public.users(id) on delete cascade,
  created_at timestamptz not null default now()
);

alter table public.tip_ask_mutes enable row level security;

-- The one exception to service-role-only in this migration: a person must be able to
-- silence their own asks, and must never be able to silence anyone else's.
create policy tip_ask_mutes_self_insert on public.tip_ask_mutes
  for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy tip_ask_mutes_self_select on public.tip_ask_mutes
  for select to authenticated
  using ((select auth.uid()) = user_id);

create policy tip_ask_mutes_self_delete on public.tip_ask_mutes
  for delete to authenticated
  using ((select auth.uid()) = user_id);

comment on table public.tip_ask_mutes is
  'Users who asked never to be emailed about a neighbor''s recommendation ask.';
