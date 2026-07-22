-- Pinned "who brings what" + read receipts.
--
-- 1. event_bring_items: one claimable row per bring item. events.bring_items
--    (text[]) stays the authoritative list the host edits; a trigger mirrors it
--    into rows so claims survive list edits. Claiming posts a 'system' message
--    into the event group chat (which rides the existing realtime + unread
--    machinery for free).
-- 2. get_thread_read_receipts: per-member last_read_at + nickname, powering
--    "Seen by Maria" under the viewer's own messages (both 1:1 and group).
--
-- Conventions: RLS on every table, no client writes, mutations via SECURITY
-- DEFINER RPCs, comms mutations gate on _require_verified_neighbor_comms().

-- ---------------------------------------------------------------------------
-- Table
-- ---------------------------------------------------------------------------

create table if not exists public.event_bring_items (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.events (id) on delete cascade,
  label text not null check (char_length(label) between 1 and 60),
  position int not null default 0,
  claimed_by uuid references public.users (id) on delete set null,
  claimed_at timestamptz,
  created_at timestamptz not null default now()
);

comment on table public.event_bring_items is
  'Claimable bring-list items for an event, mirrored from events.bring_items. Rendered as the pinned "who brings what" card in the event group chat.';

create index if not exists event_bring_items_event_idx
  on public.event_bring_items (event_id, position);

-- One row per distinct label per event (case/whitespace-insensitive); also the
-- ON CONFLICT target for the sync trigger.
create unique index if not exists event_bring_items_label_uniq
  on public.event_bring_items (event_id, lower(btrim(label)));

alter table public.event_bring_items enable row level security;

-- Members of the event's group chat read (host + approved attendees).
create policy "event_bring_items_select_member"
  on public.event_bring_items for select
  to authenticated
  using (
    exists (
      select 1 from public.chat_threads t
      where t.event_id = event_bring_items.event_id
        and t.kind = 'group_event'
        and public.lana_in_thread(t.id, auth.uid())
    )
  );

create policy "event_bring_items_no_client_write"
  on public.event_bring_items for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Trigger: mirror events.bring_items into claimable rows.
-- Adds rows for new labels; removes UNCLAIMED rows whose label left the list
-- (a claimed row is someone's commitment — never silently dropped).
-- ---------------------------------------------------------------------------

create or replace function public._sync_event_bring_items()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  insert into public.event_bring_items (event_id, label, position)
  select new.id, btrim(t), ord::int
  from unnest(coalesce(new.bring_items, '{}')) with ordinality as e(t, ord)
  where char_length(btrim(t)) between 1 and 60
  on conflict (event_id, lower(btrim(label))) do nothing;

  delete from public.event_bring_items b
  where b.event_id = new.id
    and b.claimed_by is null
    and not exists (
      select 1 from unnest(coalesce(new.bring_items, '{}')) as t
      where lower(btrim(t)) = lower(btrim(b.label))
    );

  return new;
end;
$$;

drop trigger if exists events_sync_bring_items on public.events;
create trigger events_sync_bring_items
  after insert or update of bring_items on public.events
  for each row execute function public._sync_event_bring_items();

-- Backfill: rows for existing open events that already carry a bring list.
insert into public.event_bring_items (event_id, label, position)
select e.id, btrim(t), ord::int
from public.events e,
     unnest(e.bring_items) with ordinality as b(t, ord)
where e.status = 'open'
  and char_length(btrim(t)) between 1 and 60
on conflict (event_id, lower(btrim(label))) do nothing;

-- ---------------------------------------------------------------------------
-- Internal: the event's group thread (null if none).
-- ---------------------------------------------------------------------------

create or replace function public._event_group_thread(p_event_id uuid)
returns uuid
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select id from public.chat_threads
  where event_id = p_event_id and kind = 'group_event'
  limit 1;
$$;

-- ---------------------------------------------------------------------------
-- RPC: get_event_bring_items (members of the event group chat)
-- ---------------------------------------------------------------------------

create or replace function public.get_event_bring_items(p_event_id uuid)
returns table (
  id uuid,
  label text,
  "position" int,
  claimed_by uuid,
  claimed_at timestamptz,
  claimer_nickname text,
  claimer_avatar_url text,
  is_mine boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    b.id,
    b.label,
    b.position,
    b.claimed_by,
    b.claimed_at,
    u.nickname,
    u.profile_photo_url,
    (b.claimed_by = auth.uid())
  from public.event_bring_items b
  left join public.users u on u.id = b.claimed_by
  where b.event_id = p_event_id
    and public.lana_in_thread(public._event_group_thread(p_event_id), auth.uid())
  order by b.position, b.created_at;
$$;

-- ---------------------------------------------------------------------------
-- RPC: claim_bring_item — atomic first-tap-wins; posts a system message.
-- ---------------------------------------------------------------------------

create or replace function public.claim_bring_item(p_item_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_event uuid;
  v_label text;
  v_thread uuid;
  v_nickname text;
begin
  perform public._require_verified_neighbor_comms();

  select event_id, label into v_event, v_label
  from public.event_bring_items where id = p_item_id;
  if v_event is null then
    raise exception 'bring_item_not_found' using errcode = 'P0001';
  end if;

  v_thread := public._event_group_thread(v_event);
  if v_thread is null or not public.lana_in_thread(v_thread, v_me) then
    raise exception 'not_thread_member' using errcode = 'P0001';
  end if;

  -- Atomic: only the first claimer's UPDATE finds claimed_by null.
  update public.event_bring_items
  set claimed_by = v_me, claimed_at = now()
  where id = p_item_id and claimed_by is null;
  if not found then
    raise exception 'already_claimed' using errcode = 'P0001';
  end if;

  select nickname into v_nickname from public.users where id = v_me;

  insert into public.messages (thread_id, sender_id, kind, content)
  values (v_thread, null, 'system',
          format('%s is bringing %s', coalesce(v_nickname, 'A neighbour'), v_label));

  update public.chat_threads set last_message_at = now() where id = v_thread;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: unclaim_bring_item — the claimer (or the host) reopens an item.
-- ---------------------------------------------------------------------------

create or replace function public.unclaim_bring_item(p_item_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_event uuid;
  v_label text;
  v_claimer uuid;
  v_thread uuid;
begin
  perform public._require_verified_neighbor_comms();

  select event_id, label, claimed_by into v_event, v_label, v_claimer
  from public.event_bring_items where id = p_item_id;
  if v_event is null then
    raise exception 'bring_item_not_found' using errcode = 'P0001';
  end if;
  if v_claimer is null then
    return;  -- already open; nothing to do
  end if;

  if v_claimer <> v_me and not exists (
    select 1 from public.events e
    where e.id = v_event and (e.host_id = v_me or e.cohost_id = v_me)
  ) then
    raise exception 'not_claimer_or_host' using errcode = 'P0001';
  end if;

  update public.event_bring_items
  set claimed_by = null, claimed_at = null
  where id = p_item_id;

  v_thread := public._event_group_thread(v_event);
  if v_thread is not null then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_thread, null, 'system', format('%s is open to claim again', v_label));
    update public.chat_threads set last_message_at = now() where id = v_thread;
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: add_bring_item — host/co-host appends to the list. Writes through
-- events.bring_items (the authoritative array) so previews stay consistent;
-- the sync trigger creates the claimable row.
-- ---------------------------------------------------------------------------

create or replace function public.add_bring_item(p_event_id uuid, p_label text)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_label text := btrim(coalesce(p_label, ''));
begin
  perform public._require_verified_neighbor_comms();

  if char_length(v_label) < 1 or char_length(v_label) > 60 then
    raise exception 'invalid_bring_label' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.events e
    where e.id = p_event_id and (e.host_id = v_me or e.cohost_id = v_me)
  ) then
    raise exception 'not_event_host' using errcode = 'P0001';
  end if;

  update public.events
  set bring_items = bring_items || v_label
  where id = p_event_id
    and cardinality(coalesce(bring_items, '{}')) < 12
    and not exists (
      select 1 from unnest(coalesce(bring_items, '{}')) t
      where lower(btrim(t)) = lower(v_label)
    );
  if not found then
    raise exception 'bring_list_full_or_duplicate' using errcode = 'P0001';
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: get_thread_read_receipts — other active members' read cursors.
-- Powers "Seen by Maria" under the viewer's messages. Nicknames only (the
-- masking tier governs real names elsewhere; nickname is safe in both kinds).
-- ---------------------------------------------------------------------------

create or replace function public.get_thread_read_receipts(p_thread_id uuid)
returns table (
  user_id uuid,
  nickname text,
  avatar_url text,
  last_read_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select m.user_id, u.nickname, u.profile_photo_url, m.last_read_at
  from public.chat_thread_members m
  join public.users u on u.id = m.user_id
  where m.thread_id = p_thread_id
    and m.left_at is null
    and m.user_id <> auth.uid()
    and public.lana_in_thread(p_thread_id, auth.uid())
    and not public.lana_is_blocked(auth.uid(), m.user_id);
$$;

-- ---------------------------------------------------------------------------
-- Realtime: claims update the pinned card live (RLS still applies).
-- ---------------------------------------------------------------------------

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
       where pubname = 'supabase_realtime'
         and schemaname = 'public' and tablename = 'event_bring_items'
     ) then
    alter publication supabase_realtime add table public.event_bring_items;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public._event_group_thread(uuid) from public, anon, authenticated;
grant execute on function public._event_group_thread(uuid) to service_role;

revoke all on function public.get_event_bring_items(uuid) from public, anon;
grant execute on function public.get_event_bring_items(uuid) to authenticated;

revoke all on function public.claim_bring_item(uuid) from public, anon;
grant execute on function public.claim_bring_item(uuid) to authenticated;

revoke all on function public.unclaim_bring_item(uuid) from public, anon;
grant execute on function public.unclaim_bring_item(uuid) to authenticated;

revoke all on function public.add_bring_item(uuid, text) from public, anon;
grant execute on function public.add_bring_item(uuid, text) to authenticated;

revoke all on function public.get_thread_read_receipts(uuid) from public, anon;
grant execute on function public.get_thread_read_receipts(uuid) to authenticated;
