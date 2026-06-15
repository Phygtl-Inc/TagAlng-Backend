-- Peer-to-peer shielded chat: threads, members, messages + block primitives.
-- Completes the core loop: nudge -> accept -> a real 1:1 chat opens under nicknames.
-- Follows repo conventions: RLS on every table, no client writes, all mutations via
-- SECURITY DEFINER RPCs, ordered pairs via _relationship_pair / user_low < user_high.
-- Comms mutations gate on _require_verified_neighbor_comms() (added 20260616) so
-- anonymous / phone-unverified guests cannot emit chat events (ATPR invariant 12).

-- ---------------------------------------------------------------------------
-- Block primitives (minimal slice of the moderation feature, pulled forward so
-- send_message can enforce "block always wins" from day one).
-- ---------------------------------------------------------------------------

create table if not exists public.user_blocks (
  blocker uuid not null references public.users (id) on delete cascade,
  blocked uuid not null references public.users (id) on delete cascade,
  reason text,
  created_at timestamptz not null default now(),
  primary key (blocker, blocked),
  constraint user_blocks_distinct check (blocker <> blocked)
);

comment on table public.user_blocks is
  'Directed block records. lana_is_blocked() checks both directions.';

create index if not exists user_blocks_blocked_idx on public.user_blocks (blocked);

alter table public.user_blocks enable row level security;

create policy "user_blocks_select_own"
  on public.user_blocks for select
  to authenticated
  using (blocker = auth.uid());

create policy "user_blocks_no_client_write"
  on public.user_blocks for all
  to authenticated
  using (false)
  with check (false);

create or replace function public.lana_is_blocked(p_viewer uuid, p_target uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1 from public.user_blocks
    where (blocker = p_viewer and blocked = p_target)
       or (blocker = p_target and blocked = p_viewer)
  );
$$;

comment on function public.lana_is_blocked(uuid, uuid) is
  'True if either user has blocked the other (symmetric).';

-- ---------------------------------------------------------------------------
-- Chat threads
-- ---------------------------------------------------------------------------

create type public.chat_kind as enum (
  'shielded',     -- 1:1, nicknames only (acquaintance tier)
  'direct',       -- 1:1, real names revealed (direct tier) -- same row, kind flips on unmask
  'group_event',  -- N:N, attendees of an event
  'inquiry'       -- 1:1, marketplace (feature #5)
);

create table if not exists public.chat_threads (
  id uuid primary key default gen_random_uuid(),
  kind public.chat_kind not null,
  -- 1:1 threads: ordered pair. group threads: null pair + event_id.
  user_low uuid references public.users (id) on delete cascade,
  user_high uuid references public.users (id) on delete cascade,
  event_id uuid references public.events (id) on delete cascade,
  created_by uuid references public.users (id) on delete set null,
  last_message_at timestamptz,
  archived_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint chat_threads_pair_ordered check (user_low is null or user_high is null or user_low < user_high),
  constraint chat_threads_shape check (
    (kind in ('shielded', 'direct') and user_low is not null and user_high is not null and event_id is null)
    or (kind = 'group_event' and event_id is not null and user_low is null and user_high is null)
    or (kind = 'inquiry' and user_low is not null and user_high is not null)
  )
);

comment on table public.chat_threads is
  'One row per conversation. 1:1 threads carry the ordered user pair; group threads carry event_id.';

-- One relationship chat per pair (shielded/direct share the row; kind flips on unmask).
create unique index if not exists chat_threads_relationship_uniq
  on public.chat_threads (user_low, user_high)
  where kind in ('shielded', 'direct');

create index if not exists chat_threads_event_idx on public.chat_threads (event_id);
create index if not exists chat_threads_pair_idx on public.chat_threads (user_low, user_high);

create trigger chat_threads_updated_at
  before update on public.chat_threads
  for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- Thread membership
-- ---------------------------------------------------------------------------

create table if not exists public.chat_thread_members (
  thread_id uuid not null references public.chat_threads (id) on delete cascade,
  user_id uuid not null references public.users (id) on delete cascade,
  joined_at timestamptz not null default now(),
  left_at timestamptz,
  last_read_at timestamptz,
  muted boolean not null default false,
  primary key (thread_id, user_id)
);

comment on table public.chat_thread_members is
  'Membership of a chat thread. left_at set on leave; read access preserved.';

create index if not exists chat_thread_members_user_idx
  on public.chat_thread_members (user_id) where left_at is null;

create or replace function public.lana_in_thread(p_thread uuid, p_viewer uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1 from public.chat_thread_members
    where thread_id = p_thread and user_id = p_viewer and left_at is null
  );
$$;

comment on function public.lana_in_thread(uuid, uuid) is
  'True if viewer is an active (not-left) member of the thread.';

-- ---------------------------------------------------------------------------
-- Messages (hottest table; indexed (thread_id, sent_at desc))
-- ---------------------------------------------------------------------------

create table if not exists public.messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.chat_threads (id) on delete cascade,
  sender_id uuid references public.users (id) on delete set null,  -- null = system / Lana
  kind text not null default 'text' check (kind in ('text', 'system', 'lana')),
  content text not null check (char_length(content) <= 8000),
  reply_to uuid references public.messages (id) on delete set null,
  client_dedupe_key uuid,
  deleted_at timestamptz,                          -- for_everyone tombstone
  deleted_for uuid[] not null default '{}',        -- for_me hide list
  sent_at timestamptz not null default now()
);

comment on table public.messages is
  'Chat messages. sender_id null for system/Lana. Masking (nickname vs real name) is governed by tier on read, not stored here.';

create index if not exists messages_thread_time_idx
  on public.messages (thread_id, sent_at desc);

-- Idempotency: a (thread, sender, client_dedupe_key) tuple sends at most once.
create unique index if not exists messages_dedupe_uniq
  on public.messages (thread_id, sender_id, client_dedupe_key)
  where sender_id is not null and client_dedupe_key is not null;

alter table public.chat_threads enable row level security;
alter table public.chat_thread_members enable row level security;
alter table public.messages enable row level security;

-- Threads: members read; no client writes.
create policy "chat_threads_select_member"
  on public.chat_threads for select
  to authenticated
  using (public.lana_in_thread(id, auth.uid()));

create policy "chat_threads_no_client_write"
  on public.chat_threads for all
  to authenticated
  using (false) with check (false);

-- Members: members of the same thread read; no client writes.
create policy "chat_thread_members_select_member"
  on public.chat_thread_members for select
  to authenticated
  using (public.lana_in_thread(thread_id, auth.uid()));

create policy "chat_thread_members_no_client_write"
  on public.chat_thread_members for all
  to authenticated
  using (false) with check (false);

-- Messages: read if member AND sender not blocked; no client writes.
create policy "messages_select_member_unblocked"
  on public.messages for select
  to authenticated
  using (
    public.lana_in_thread(thread_id, auth.uid())
    and (sender_id is null or not public.lana_is_blocked(auth.uid(), sender_id))
  );

create policy "messages_no_client_write"
  on public.messages for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Internal: open (or reuse) the 1:1 relationship thread for a pair.
-- Idempotent. Inserts members + a Lana opener only when newly created.
-- ---------------------------------------------------------------------------

create or replace function public._open_relationship_thread(p_a uuid, p_b uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_low uuid;
  v_high uuid;
  v_thread uuid;
begin
  select user_low, user_high into v_low, v_high
  from public._relationship_pair(p_a, p_b);

  insert into public.chat_threads (kind, user_low, user_high, created_by)
  values ('shielded', v_low, v_high, p_a)
  on conflict (user_low, user_high) where kind in ('shielded', 'direct')
  do nothing
  returning id into v_thread;

  if v_thread is null then
    -- thread already existed; reuse it
    select id into v_thread
    from public.chat_threads
    where user_low = v_low and user_high = v_high
      and kind in ('shielded', 'direct');
    return v_thread;
  end if;

  insert into public.chat_thread_members (thread_id, user_id)
  values (v_thread, v_low), (v_thread, v_high)
  on conflict (thread_id, user_id) do nothing;

  insert into public.messages (thread_id, sender_id, kind, content)
  values (
    v_thread, null, 'lana',
    'You''re connected. Your real names stay private until you both choose to share them — say hi!'
  );

  update public.chat_threads set last_message_at = now() where id = v_thread;

  return v_thread;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: send_message
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
begin
  -- Gate: authenticated, non-anonymous, phone-verified neighbor (ATPR invariant 12).
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

  -- Idempotent return on dedupe key.
  if p_client_dedupe_key is not null then
    select id into v_existing
    from public.messages
    where thread_id = p_thread_id
      and sender_id = v_me
      and client_dedupe_key = p_client_dedupe_key;
    if v_existing is not null then
      return v_existing;
    end if;
  end if;

  -- Block always wins: reject if any other active member is blocked either way.
  if exists (
    select 1 from public.chat_thread_members m
    where m.thread_id = p_thread_id
      and m.user_id <> v_me
      and m.left_at is null
      and public.lana_is_blocked(v_me, m.user_id)
  ) then
    raise exception 'blocked' using errcode = 'P0001';
  end if;

  -- reply_to must belong to the same thread.
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
-- RPC: get_thread_messages (paginated; newest-first, before-cursor)
-- ---------------------------------------------------------------------------

create or replace function public.get_thread_messages(
  p_thread_id uuid,
  p_limit int default 50,
  p_before timestamptz default null
)
returns table (
  id uuid,
  sender_id uuid,
  kind text,
  content text,
  reply_to uuid,
  sent_at timestamptz,
  deleted boolean,
  is_mine boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    m.id,
    m.sender_id,
    m.kind,
    case when m.deleted_at is not null then '' else m.content end,
    m.reply_to,
    m.sent_at,
    (m.deleted_at is not null),
    (m.sender_id = auth.uid())
  from public.messages m
  where m.thread_id = p_thread_id
    and public.lana_in_thread(p_thread_id, auth.uid())
    and not (auth.uid() = any (m.deleted_for))
    and (m.sender_id is null or not public.lana_is_blocked(auth.uid(), m.sender_id))
    and (p_before is null or m.sent_at < p_before)
  order by m.sent_at desc
  limit greatest(1, least(coalesce(p_limit, 50), 100));
$$;

-- ---------------------------------------------------------------------------
-- RPC: mark_thread_read
-- ---------------------------------------------------------------------------

create or replace function public.mark_thread_read(
  p_thread_id uuid,
  p_up_to_message_id uuid default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_ts timestamptz := now();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if not public.lana_in_thread(p_thread_id, v_me) then
    raise exception 'not_thread_member' using errcode = 'P0001';
  end if;

  if p_up_to_message_id is not null then
    select sent_at into v_ts
    from public.messages
    where id = p_up_to_message_id and thread_id = p_thread_id;
    v_ts := coalesce(v_ts, now());
  end if;

  update public.chat_thread_members
  set last_read_at = greatest(coalesce(last_read_at, v_ts), v_ts)
  where thread_id = p_thread_id and user_id = v_me;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: get_my_threads (1:1 relationship chats with last message + unread count)
-- ---------------------------------------------------------------------------

create or replace function public.get_my_threads()
returns table (
  thread_id uuid,
  kind public.chat_kind,
  other_user_id uuid,
  other_nickname text,
  other_avatar_url text,
  tier public.relationship_tier,
  last_message_at timestamptz,
  last_message_preview text,
  unread_count int
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    t.id,
    t.kind,
    other.id,
    other.nickname,
    other.profile_photo_url,
    public.get_relationship_tier(other.id),
    t.last_message_at,
    (
      select case when lm.deleted_at is not null then '' else lm.content end
      from public.messages lm
      where lm.thread_id = t.id
        and not (auth.uid() = any (lm.deleted_for))
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
    )
  from public.chat_threads t
  join public.chat_thread_members mem
    on mem.thread_id = t.id and mem.user_id = auth.uid() and mem.left_at is null
  join public.users other
    on other.id = case when t.user_low = auth.uid() then t.user_high else t.user_low end
  where t.kind in ('shielded', 'direct')
    and t.archived_at is null
    and not public.lana_is_blocked(auth.uid(), other.id)
  order by t.last_message_at desc nulls last;
$$;

-- ---------------------------------------------------------------------------
-- RPC: delete_message (for_everyone within 1h, or for_me)
-- ---------------------------------------------------------------------------

create or replace function public.delete_message(
  p_message_id uuid,
  p_kind text default 'for_me'
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_sender uuid;
  v_sent timestamptz;
  v_thread uuid;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_kind not in ('for_me', 'for_everyone') then
    raise exception 'invalid_delete_kind' using errcode = 'P0001';
  end if;

  select sender_id, sent_at, thread_id into v_sender, v_sent, v_thread
  from public.messages where id = p_message_id;

  if v_thread is null or not public.lana_in_thread(v_thread, v_me) then
    raise exception 'message_not_found' using errcode = 'P0001';
  end if;

  if p_kind = 'for_everyone' then
    if v_sender is distinct from v_me then
      raise exception 'not_message_sender' using errcode = 'P0001';
    end if;
    if v_sent < now() - interval '1 hour' then
      raise exception 'delete_window_expired' using errcode = 'P0001';
    end if;
    update public.messages
    set deleted_at = now()
    where id = p_message_id;
  else
    update public.messages
    set deleted_for = (
      select array(select distinct unnest(deleted_for || array[v_me]))
    )
    where id = p_message_id;
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Hook: opening the shielded chat when a nudge is accepted.
-- Rebased on the 20260616 accept_nudge (uses _require_verified_neighbor_comms);
-- non-breaking: still returns the new tier, the thread opens as a side effect.
-- ---------------------------------------------------------------------------

create or replace function public.accept_nudge(p_nudge_id uuid)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_sender uuid;
  v_new_tier public.relationship_tier;
begin
  perform public._require_verified_neighbor_comms();

  update public.nudges n
  set status = 'accepted',
      responded_at = now()
  where n.id = p_nudge_id
    and n.recipient_id = auth.uid()
    and n.status = 'pending'
  returning n.sender_id into v_sender;

  if v_sender is null then
    raise exception 'nudge_not_found_or_already_handled' using errcode = 'P0001';
  end if;

  v_new_tier := public.promote_relationship_tier(v_sender, 'nudge_accepted', p_nudge_id);

  -- Open the shielded 1:1 chat for the pair (idempotent).
  perform public._open_relationship_thread(auth.uid(), v_sender);

  return v_new_tier;
end;
$$;

-- ---------------------------------------------------------------------------
-- Realtime: clients observe new messages / thread changes (RLS still applies).
-- Guarded: only if the supabase_realtime publication exists, and idempotent.
-- ---------------------------------------------------------------------------

do $$
declare
  v_tbl text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach v_tbl in array array['messages', 'chat_threads', 'chat_thread_members'] loop
      if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = v_tbl
      ) then
        execute format('alter publication supabase_realtime add table public.%I', v_tbl);
      end if;
    end loop;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Grants (clients call RPCs; internal helpers are service_role / definer-only).
-- ---------------------------------------------------------------------------

revoke all on function public.lana_is_blocked(uuid, uuid) from public, anon;
grant execute on function public.lana_is_blocked(uuid, uuid) to authenticated;

revoke all on function public.lana_in_thread(uuid, uuid) from public, anon;
grant execute on function public.lana_in_thread(uuid, uuid) to authenticated;

revoke all on function public._open_relationship_thread(uuid, uuid) from public, anon, authenticated;
grant execute on function public._open_relationship_thread(uuid, uuid) to service_role;

revoke all on function public.send_message(uuid, text, uuid, uuid) from public, anon;
grant execute on function public.send_message(uuid, text, uuid, uuid) to authenticated;

revoke all on function public.get_thread_messages(uuid, int, timestamptz) from public, anon;
grant execute on function public.get_thread_messages(uuid, int, timestamptz) to authenticated;

revoke all on function public.mark_thread_read(uuid, uuid) from public, anon;
grant execute on function public.mark_thread_read(uuid, uuid) to authenticated;

revoke all on function public.get_my_threads() from public, anon;
grant execute on function public.get_my_threads() to authenticated;

revoke all on function public.delete_message(uuid, text) from public, anon;
grant execute on function public.delete_message(uuid, text) to authenticated;
