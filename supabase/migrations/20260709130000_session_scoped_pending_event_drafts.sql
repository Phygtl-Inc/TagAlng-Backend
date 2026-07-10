-- Session-scoped pending event drafts.
--
-- QA (2026-07-08): concurrent sessions under one account corrupted each other's event
-- drafts. The in-flight draft itself lives in lana_sessions.context — already keyed by
-- session id — but pending_event_drafts (the stash that carries a ready-to-publish event
-- across a guest -> existing-account login) was keyed ONE ROW PER USER (user_id primary
-- key, upsert latest-wins). Two sessions stashing toward the same destination account
-- clobbered each other, and the pop deleted every row for the user at once.
--
-- Re-key the stash by the SOURCE session: one stash slot per session (upsert on
-- session_id), with user_id kept as the recovery lookup — the destination account's next
-- new session pops its newest stash only, leaving any other session's stash intact.

alter table public.pending_event_drafts
  add column if not exists id uuid not null default gen_random_uuid(),
  add column if not exists session_id uuid references public.lana_sessions(id) on delete cascade;

-- Backfill: attribute existing stashes to the user's most recent active session — the
-- best available signal, since the source session id was never recorded before this
-- migration. Rows for users with no active session keep session_id null; the pop-by-user
-- recovery path still drains them.
update public.pending_event_drafts p
set session_id = (
  select s.id
  from public.lana_sessions s
  where s.user_id = p.user_id
    and s.status = 'active'
  order by s.updated_at desc
  limit 1
)
where p.session_id is null;

-- Drop the one-row-per-user key; a surrogate id becomes the primary key.
alter table public.pending_event_drafts
  drop constraint pending_event_drafts_pkey;
alter table public.pending_event_drafts
  add primary key (id);

-- One stash slot per source session (nulls stay distinct for legacy rows), and a fast
-- newest-first lookup for the pop-by-user recovery.
create unique index if not exists pending_event_drafts_session_id_key
  on public.pending_event_drafts (session_id);
create index if not exists pending_event_drafts_user_id_created_idx
  on public.pending_event_drafts (user_id, created_at desc);

comment on table public.pending_event_drafts is
  'Ready-to-publish event stashes carried across a guest -> existing-account login. One row per SOURCE session (session_id unique); popped newest-first by destination user_id.';
