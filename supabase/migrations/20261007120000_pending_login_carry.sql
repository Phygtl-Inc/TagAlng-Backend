-- Everything a guest had in flight survives a login into an existing account.
--
-- The JWT swap + forced new session drops the guest's context. Three flows were
-- stashed by hand (pending_event_drafts, pending_meet_seeks, pending_signal_asks);
-- every other pending key was silently lost. Prod 2026-08-06: a guest asked for
-- Italian restaurants, confirmed with a chip, saw "YOUR ASK: Italian restaurants",
-- logged in, and got "how can I help you today?" — tip_seek_pending had no stash.
--
-- One row per destination account, one-shot: written at email entry, read and
-- deleted by that account's next session (mirrors the three tables above). Which
-- keys travel is decided in app/login_carry.py, not here, so adding a flow does
-- not need a migration.

create table if not exists public.pending_login_carry (
  user_id    uuid primary key references public.users(id) on delete cascade,
  carry      jsonb not null,
  created_at timestamptz not null default now()
);

comment on table public.pending_login_carry is
  'Guest session context held for a login into an existing account. One-shot: '
  'popped by that account''s next session. Keys are chosen by '
  'app/login_carry.py::LOGIN_CARRY_KEYS.';

alter table public.pending_login_carry enable row level security;

-- Service-role only, exactly like the sibling stash tables: the row is written
-- for an account the CALLER IS NOT YET AUTHENTICATED AS (they are still the
-- guest), so no client-facing policy could express this safely.
drop policy if exists pending_login_carry_no_client on public.pending_login_carry;
create policy pending_login_carry_no_client on public.pending_login_carry
  for all to authenticated using (false) with check (false);

-- Housekeeping: an unclaimed stash is stale after a day (the login either
-- happened or was abandoned). Cheap partial scan on the one column.
create index if not exists pending_login_carry_created_idx
  on public.pending_login_carry (created_at);
