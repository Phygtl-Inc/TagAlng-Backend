-- A not_going RSVP no longer drops group-chat membership.
--
-- 20260817 keyed membership on BOTH knobs (status approved/attended AND
-- rsvp_status <> 'not_going'), so tapping "Not going" stamped left_at and the
-- chat vanished from the guest's list mid-conversation. Product call: backing
-- out frees the capacity spot but you stay in the conversation. Membership now
-- keys on the host-owned status alone; only cancel/decline removes you.

-- ---------------------------------------------------------------------------
-- 1. Trigger: membership follows status only (body from 20260817, rsvp dropped)
-- ---------------------------------------------------------------------------

create or replace function public._sync_event_group_membership()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread uuid;
  v_was_member boolean;
  v_is_member boolean;
begin
  select id into v_thread
  from public.chat_threads
  where event_id = new.event_id and kind = 'group_event';

  if v_thread is null then
    return new;
  end if;

  v_is_member := new.status in ('approved', 'attended');
  v_was_member := tg_op = 'UPDATE'
                  and old.status in ('approved', 'attended');

  if v_is_member and not v_was_member then
    insert into public.chat_thread_members (thread_id, user_id)
    values (v_thread, new.requester_id)
    on conflict (thread_id, user_id) do update set left_at = null;  -- rejoin clears left_at

  elsif v_was_member and not v_is_member then
    update public.chat_thread_members
    set left_at = now()
    where thread_id = v_thread and user_id = new.requester_id and left_at is null;
  end if;

  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. Backfill: restore guests already evicted by a not_going flip. Safe to key
--    on status alone — the trigger above is the only writer of left_at, so any
--    approved/attended member with left_at set was evicted by the old rule.
-- ---------------------------------------------------------------------------

update public.chat_thread_members m
set left_at = null
from public.chat_threads t
join public.event_requests er on er.event_id = t.event_id
where t.id = m.thread_id
  and t.kind = 'group_event'
  and er.requester_id = m.user_id
  and er.status in ('approved', 'attended')
  and m.left_at is not null;
