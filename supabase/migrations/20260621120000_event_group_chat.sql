-- Event group chat (Path 4): a group_event thread is auto-created when an event
-- is published, the host joins, and approved attendees auto-join. Lana is absent
-- from group threads. Blocking is pairwise on READ (we don't reject group posts).
-- Trigger functions are SECURITY DEFINER so they bypass the no_client_write RLS.

-- One group chat per event (guards trigger + backfill against duplicates).
create unique index if not exists chat_threads_event_group_uniq
  on public.chat_threads (event_id)
  where kind = 'group_event';

-- ---------------------------------------------------------------------------
-- Trigger: create the group chat when an event is published (status='open')
-- ---------------------------------------------------------------------------

create or replace function public._create_event_group_chat()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread uuid;
begin
  if new.status = 'open' then
    insert into public.chat_threads (kind, event_id, created_by)
    values ('group_event', new.id, new.host_id)
    returning id into v_thread;

    insert into public.chat_thread_members (thread_id, user_id)
    values (v_thread, new.host_id)
    on conflict (thread_id, user_id) do nothing;
  end if;
  return new;
end;
$$;

drop trigger if exists events_create_group_chat on public.events;
create trigger events_create_group_chat
  after insert on public.events
  for each row execute function public._create_event_group_chat();

-- ---------------------------------------------------------------------------
-- Trigger: membership follows RSVP — join on 'approved', leave on cancel/decline
-- ---------------------------------------------------------------------------

create or replace function public._sync_event_group_membership()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread uuid;
begin
  select id into v_thread
  from public.chat_threads
  where event_id = new.event_id and kind = 'group_event';

  if v_thread is null then
    return new;
  end if;

  if new.status = 'approved'
     and (tg_op = 'INSERT' or old.status is distinct from 'approved') then
    insert into public.chat_thread_members (thread_id, user_id)
    values (v_thread, new.requester_id)
    on conflict (thread_id, user_id) do update set left_at = null;  -- rejoin clears left_at

  elsif tg_op = 'UPDATE'
        and new.status in ('cancelled', 'declined')
        and old.status is distinct from new.status then
    update public.chat_thread_members
    set left_at = now()
    where thread_id = v_thread and user_id = new.requester_id and left_at is null;
  end if;

  return new;
end;
$$;

drop trigger if exists event_requests_group_membership on public.event_requests;
create trigger event_requests_group_membership
  after insert or update on public.event_requests
  for each row execute function public._sync_event_group_membership();

-- ---------------------------------------------------------------------------
-- Redefine send_message: group-aware block check.
-- 1:1 threads (shielded/direct/inquiry) reject when the other party is blocked.
-- group_event threads do NOT reject — blocking is enforced pairwise on read
-- (messages_select_member_unblocked already hides blocked senders per viewer).
-- (Faithful copy of the 20260618 body + the kind-aware block check.)
-- ---------------------------------------------------------------------------

create or replace function public.send_message(
  p_thread_id uuid,
  p_content text,
  p_client_dedupe_key uuid,
  p_reply_to uuid default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_msg_id uuid;
  v_existing uuid;
  v_kind public.chat_kind;
begin
  perform public._require_verified_neighbor_comms();

  if p_content is null or char_length(trim(p_content)) < 1 then
    raise exception 'empty_message' using errcode = 'P0001';
  end if;
  if char_length(p_content) > 8000 then
    raise exception 'message_too_long' using errcode = 'P0001';
  end if;
  if not public.lana_in_thread(p_thread_id, v_me) then
    raise exception 'not_thread_member' using errcode = 'P0001';
  end if;

  if p_client_dedupe_key is not null then
    select id into v_existing
    from public.messages
    where thread_id = p_thread_id and sender_id = v_me and client_dedupe_key = p_client_dedupe_key;
    if v_existing is not null then
      return v_existing;
    end if;
  end if;

  select kind into v_kind from public.chat_threads where id = p_thread_id;

  -- Block always wins on 1:1 threads. On group threads, do not reject the post
  -- (read-side filtering hides blocked senders for each viewer).
  if v_kind in ('shielded', 'direct', 'inquiry') and exists (
    select 1 from public.chat_thread_members m
    where m.thread_id = p_thread_id
      and m.user_id <> v_me
      and m.left_at is null
      and public.lana_is_blocked(v_me, m.user_id)
  ) then
    raise exception 'blocked' using errcode = 'P0001';
  end if;

  if p_reply_to is not null and not exists (
    select 1 from public.messages where id = p_reply_to and thread_id = p_thread_id
  ) then
    raise exception 'reply_to_not_in_thread' using errcode = 'P0001';
  end if;

  insert into public.messages (thread_id, sender_id, kind, content, reply_to, client_dedupe_key)
  values (p_thread_id, v_me, 'text', trim(p_content), p_reply_to, p_client_dedupe_key)
  returning id into v_msg_id;

  update public.chat_threads set last_message_at = now() where id = p_thread_id;

  return v_msg_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: get_my_group_threads (event group chats the caller belongs to)
-- ---------------------------------------------------------------------------

create or replace function public.get_my_group_threads()
returns table (
  thread_id uuid,
  event_id uuid,
  event_title text,
  last_message_at timestamptz,
  last_message_preview text,
  unread_count int,
  member_count int
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    t.id,
    e.id,
    e.title,
    t.last_message_at,
    (
      select case when lm.deleted_at is not null then '' else lm.content end
      from public.messages lm
      where lm.thread_id = t.id and not (auth.uid() = any (lm.deleted_for))
      order by lm.sent_at desc
      limit 1
    ),
    (
      select count(*)::int
      from public.messages um
      join public.chat_thread_members me
        on me.thread_id = t.id and me.user_id = auth.uid()
      where um.thread_id = t.id
        and um.sender_id is distinct from auth.uid()
        and um.deleted_at is null
        and not (auth.uid() = any (um.deleted_for))
        and (me.last_read_at is null or um.sent_at > me.last_read_at)
    ),
    (
      select count(*)::int
      from public.chat_thread_members cm
      where cm.thread_id = t.id and cm.left_at is null
    )
  from public.chat_threads t
  join public.chat_thread_members mem
    on mem.thread_id = t.id and mem.user_id = auth.uid() and mem.left_at is null
  join public.events e on e.id = t.event_id
  where t.kind = 'group_event' and t.archived_at is null
  order by t.last_message_at desc nulls last;
$$;

-- ---------------------------------------------------------------------------
-- Backfill: group chats for existing open events (host + approved/attended).
-- ---------------------------------------------------------------------------

do $$
declare
  r record;
  v_thread uuid;
begin
  for r in
    select e.id, e.host_id
    from public.events e
    where e.status = 'open'
      and not exists (
        select 1 from public.chat_threads t
        where t.event_id = e.id and t.kind = 'group_event'
      )
  loop
    insert into public.chat_threads (kind, event_id, created_by)
    values ('group_event', r.id, r.host_id)
    returning id into v_thread;

    insert into public.chat_thread_members (thread_id, user_id)
    values (v_thread, r.host_id)
    on conflict (thread_id, user_id) do nothing;

    insert into public.chat_thread_members (thread_id, user_id)
    select v_thread, er.requester_id
    from public.event_requests er
    where er.event_id = r.id and er.status in ('approved', 'attended')
    on conflict (thread_id, user_id) do nothing;
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public.send_message(uuid, text, uuid, uuid) from public, anon;
grant execute on function public.send_message(uuid, text, uuid, uuid) to authenticated;

revoke all on function public.get_my_group_threads() from public, anon;
grant execute on function public.get_my_group_threads() to authenticated;
