-- "Tell her once. She keeps looking." — she did keep looking; nobody ever told her.
--
-- _match_local_signal has always run on EVERY insert, in both directions, and queued rows
-- into match_notifications at strength >= 0.75. But match_notifications had no consumer:
-- grep across the worker returns nothing. It was written by the matcher and read by no one
-- ("audit log; Twilio/push wired later" — the 20260630 migration). So a neighbor could post
-- the exact tip you asked for, the match row existed the same second, and you learned about
-- it only if you happened to open the radar yourself.
--
-- No scheduler is needed to close this. Every match is created INSIDE a live worker turn —
-- the neighbor's own chat turn, the one that inserted their tip. These two RPCs turn the
-- queue into a real outbox with a drain step:
--
--   drain_signal_match_notifications(signal_id) — called by the actor's own turn, right
--      after their insert. Returns who to tell (the OTHER side) and enough truthful text to
--      say why, flipping queued -> sent in the same statement so a retry can't double-send.
--   drain_stale_match_notifications(minutes)    — service-role sweeper for anything left
--      queued (a crashed turn, or a future insert path that isn't a Lana turn). Correct
--      without a scheduler; becomes a safety net if one is ever pointed at it.

-- ---------------------------------------------------------------------------
-- §1 drain_signal_match_notifications — the actor's turn tells the other side
-- ---------------------------------------------------------------------------
-- security definer: it must read the PEER's queued notification row and their signal text,
-- both RLS-hidden from the caller. Ownership of p_signal_id is checked first, so a caller
-- can only ever drain matches created by their own posting.
--
-- Direction, from _match_local_signal: for the newly inserted signal S, the peer's own
-- block-log row is the one with peer_signal_id = S (their my_signal_id is their older
-- signal). That row's notification is the one to send.
create or replace function public.drain_signal_match_notifications(
  p_signal_id uuid
)
returns table (
  notification_id uuid,
  recipient_user_id uuid,
  recipient_ask text,
  recipient_intent text,
  match_detail text,
  match_intent text,
  match_strength real,
  block_id text
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_signal_id is null then
    return;
  end if;
  if not exists (
    select 1 from public.local_signals
    where id = p_signal_id and user_id = v_me
  ) then
    return;  -- not the caller's signal: nothing to drain, and nothing leaked
  end if;

  -- The actor is right here in chat and their own reply already told them what matched,
  -- so their copy is retired as 'skipped' rather than left queued forever.
  update public.match_notifications n
  set status = 'skipped'
  from public.block_log_entries e
  where n.block_log_entry_id = e.id
    and n.status = 'queued'
    and e.my_signal_id = p_signal_id
    and n.user_id = v_me;

  return query
  with drained as (
    update public.match_notifications n
    set status = 'sent', sent_at = now()
    where n.id in (
      select n2.id
      from public.match_notifications n2
      join public.block_log_entries e2 on e2.id = n2.block_log_entry_id
      where n2.status = 'queued'
        and e2.peer_signal_id = p_signal_id
        and e2.for_user_id <> v_me
    )
    returning n.id, n.user_id, n.block_log_entry_id
  )
  select
    d.id,
    d.user_id,
    theirs.detail_text,
    theirs.intent,
    mine.detail_text,
    mine.intent,
    e.match_strength,
    e.block_id
  from drained d
  join public.block_log_entries e on e.id = d.block_log_entry_id
  left join public.local_signals theirs on theirs.id = e.my_signal_id
  left join public.local_signals mine on mine.id = e.peer_signal_id;
end;
$$;

revoke all on function public.drain_signal_match_notifications(uuid) from public, anon;
grant execute on function public.drain_signal_match_notifications(uuid) to authenticated;

comment on function public.drain_signal_match_notifications(uuid) is
  'Claim the queued match notifications owed to the OTHER side of a just-created match, '
  'marking them sent atomically. Called by the actor''s own turn — no scheduler needed.';

-- ---------------------------------------------------------------------------
-- §2 drain_stale_match_notifications — service-role sweeper
-- ---------------------------------------------------------------------------
-- Same claim, but across users and keyed on age instead of a signal id: whatever is still
-- queued after p_older_than_minutes had no turn to carry it. service_role only.
create or replace function public.drain_stale_match_notifications(
  p_older_than_minutes int default 10,
  p_limit int default 200
)
returns table (
  notification_id uuid,
  recipient_user_id uuid,
  recipient_ask text,
  recipient_intent text,
  match_detail text,
  match_intent text,
  match_strength real,
  block_id text
)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  return query
  with drained as (
    update public.match_notifications n
    set status = 'sent', sent_at = now()
    where n.id in (
      select n2.id
      from public.match_notifications n2
      where n2.status = 'queued'
        and n2.created_at < now() - make_interval(mins => greatest(1, coalesce(p_older_than_minutes, 10)))
      order by n2.created_at
      limit greatest(1, least(coalesce(p_limit, 200), 1000))
    )
    returning n.id, n.user_id, n.block_log_entry_id
  )
  select
    d.id,
    d.user_id,
    theirs.detail_text,
    theirs.intent,
    peer.detail_text,
    peer.intent,
    e.match_strength,
    e.block_id
  from drained d
  join public.block_log_entries e on e.id = d.block_log_entry_id
  left join public.local_signals theirs on theirs.id = e.my_signal_id
  left join public.local_signals peer on peer.id = e.peer_signal_id;
end;
$$;

revoke all on function public.drain_stale_match_notifications(int, int) from public, anon, authenticated;
grant execute on function public.drain_stale_match_notifications(int, int) to service_role;

comment on function public.drain_stale_match_notifications(int, int) is
  'Sweeper for match notifications no live turn delivered (crashed turn, non-Lana insert). '
  'service_role only; correct without a scheduler, safety net with one.';
