-- Message safety gateway (#7, Option A): the lana-worker becomes the mandatory send
-- path. Clients can no longer write messages directly — send_message is locked to
-- service_role. The worker AI-scans content, then calls worker_send_message (pass) or
-- create_message_hold (unsafe). Held messages can be released via "Send anyway"
-- (override_held_message) unless hard-blocked (hate/violence). Also enforces suspension.

-- ---------------------------------------------------------------------------
-- Holds table
-- ---------------------------------------------------------------------------

create table if not exists public.lana_message_holds (
  id uuid primary key default gen_random_uuid(),
  would_be_sender uuid not null references public.users (id) on delete cascade,
  would_be_thread_id uuid not null references public.chat_threads (id) on delete cascade,
  content text not null,
  reply_to uuid references public.messages (id) on delete set null,
  client_dedupe_key uuid,
  reason text not null default 'detected_unsafe',     -- detected_unsafe | suspended | rate_limit
  reason_category text,                                -- hate|violence|sexual|self_harm|pii_leak|off_platform_ask|spam|other
  send_anyway_allowed boolean not null default true,   -- false for hard-block categories
  detector text,                                       -- vertex | heuristic | manual
  detector_score jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  released_at timestamptz,
  released_by uuid references public.users (id) on delete set null,
  released_message_id uuid references public.messages (id) on delete set null,
  denied_at timestamptz,
  denied_by uuid references public.users (id) on delete set null
);

comment on table public.lana_message_holds is
  'Messages intercepted pre-send by the safety gateway. Sender may "Send anyway" unless send_anyway_allowed is false.';

create index if not exists lana_message_holds_sender_open_idx
  on public.lana_message_holds (would_be_sender, created_at desc)
  where released_at is null and denied_at is null;

alter table public.lana_message_holds enable row level security;

create policy "lana_message_holds_select_own"
  on public.lana_message_holds for select
  to authenticated
  using (would_be_sender = auth.uid());

create policy "lana_message_holds_no_client_write"
  on public.lana_message_holds for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- worker_send_message — the worker's persist path (service_role; explicit sender).
-- Mirrors send_message (group-aware block) + a suspension gate. auth.uid() is the
-- service role here, so the verified sender is passed in by the worker.
-- ---------------------------------------------------------------------------

create or replace function public.worker_send_message(
  p_sender uuid,
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
  v_msg_id uuid;
  v_existing uuid;
  v_kind public.chat_kind;
begin
  if p_sender is null then
    raise exception 'sender_required' using errcode = 'P0001';
  end if;
  if p_content is null or char_length(trim(p_content)) < 1 then
    raise exception 'empty_message' using errcode = 'P0001';
  end if;
  if char_length(p_content) > 8000 then
    raise exception 'message_too_long' using errcode = 'P0001';
  end if;
  if public.lana_is_suspended(p_sender) then
    raise exception 'suspended' using errcode = 'P0001';
  end if;
  if not public.lana_in_thread(p_thread_id, p_sender) then
    raise exception 'not_thread_member' using errcode = 'P0001';
  end if;

  if p_client_dedupe_key is not null then
    select id into v_existing
    from public.messages
    where thread_id = p_thread_id and sender_id = p_sender and client_dedupe_key = p_client_dedupe_key;
    if v_existing is not null then
      return v_existing;
    end if;
  end if;

  select kind into v_kind from public.chat_threads where id = p_thread_id;

  if v_kind in ('shielded', 'direct', 'inquiry') and exists (
    select 1 from public.chat_thread_members m
    where m.thread_id = p_thread_id and m.user_id <> p_sender and m.left_at is null
      and public.lana_is_blocked(p_sender, m.user_id)
  ) then
    raise exception 'blocked' using errcode = 'P0001';
  end if;

  if p_reply_to is not null and not exists (
    select 1 from public.messages where id = p_reply_to and thread_id = p_thread_id
  ) then
    raise exception 'reply_to_not_in_thread' using errcode = 'P0001';
  end if;

  insert into public.messages (thread_id, sender_id, kind, content, reply_to, client_dedupe_key)
  values (p_thread_id, p_sender, 'text', trim(p_content), p_reply_to, p_client_dedupe_key)
  returning id into v_msg_id;

  update public.chat_threads set last_message_at = now() where id = p_thread_id;
  return v_msg_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- create_message_hold — the worker parks an unsafe message (service_role).
-- ---------------------------------------------------------------------------

create or replace function public.create_message_hold(
  p_sender uuid,
  p_thread_id uuid,
  p_content text,
  p_reason_category text,
  p_send_anyway_allowed boolean default true,
  p_detector text default 'vertex',
  p_detector_score jsonb default '{}'::jsonb,
  p_reply_to uuid default null,
  p_client_dedupe_key uuid default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_id uuid;
begin
  if p_sender is null or p_thread_id is null then
    raise exception 'sender_and_thread_required' using errcode = 'P0001';
  end if;

  insert into public.lana_message_holds (
    would_be_sender, would_be_thread_id, content, reply_to, client_dedupe_key,
    reason, reason_category, send_anyway_allowed, detector, detector_score
  )
  values (
    p_sender, p_thread_id, p_content, p_reply_to, p_client_dedupe_key,
    'detected_unsafe', p_reason_category, coalesce(p_send_anyway_allowed, true),
    p_detector, coalesce(p_detector_score, '{}'::jsonb)
  )
  returning id into v_id;

  return v_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- override_held_message — sender's "Send anyway" (authenticated).
-- Re-checks suspension/membership/block; rejects hard-blocked holds.
-- ---------------------------------------------------------------------------

create or replace function public.override_held_message(p_hold_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_hold record;
  v_kind public.chat_kind;
  v_msg_id uuid;
begin
  perform public._require_verified_neighbor_comms();

  select * into v_hold from public.lana_message_holds where id = p_hold_id;
  if v_hold is null or v_hold.would_be_sender <> v_me then
    raise exception 'hold_not_found' using errcode = 'P0001';
  end if;
  if v_hold.released_at is not null or v_hold.denied_at is not null then
    raise exception 'hold_already_resolved' using errcode = 'P0001';
  end if;
  if not v_hold.send_anyway_allowed then
    raise exception 'hard_blocked' using errcode = 'P0001';
  end if;
  if public.lana_is_suspended(v_me) then
    raise exception 'suspended' using errcode = 'P0001';
  end if;
  if not public.lana_in_thread(v_hold.would_be_thread_id, v_me) then
    raise exception 'not_thread_member' using errcode = 'P0001';
  end if;

  select kind into v_kind from public.chat_threads where id = v_hold.would_be_thread_id;
  if v_kind in ('shielded', 'direct', 'inquiry') and exists (
    select 1 from public.chat_thread_members m
    where m.thread_id = v_hold.would_be_thread_id and m.user_id <> v_me and m.left_at is null
      and public.lana_is_blocked(v_me, m.user_id)
  ) then
    raise exception 'blocked' using errcode = 'P0001';
  end if;

  insert into public.messages (thread_id, sender_id, kind, content, reply_to, client_dedupe_key)
  values (v_hold.would_be_thread_id, v_me, 'text', trim(v_hold.content), v_hold.reply_to, v_hold.client_dedupe_key)
  returning id into v_msg_id;

  update public.chat_threads set last_message_at = now() where id = v_hold.would_be_thread_id;
  update public.lana_message_holds
  set released_at = now(), released_by = v_me, released_message_id = v_msg_id
  where id = p_hold_id;

  return v_msg_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- get_my_message_holds — sender sees their own open holds (authenticated).
-- ---------------------------------------------------------------------------

create or replace function public.get_my_message_holds()
returns table (
  hold_id uuid,
  thread_id uuid,
  content text,
  reason_category text,
  send_anyway_allowed boolean,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select id, would_be_thread_id, content, reason_category, send_anyway_allowed, created_at
  from public.lana_message_holds
  where would_be_sender = auth.uid()
    and released_at is null and denied_at is null
  order by created_at desc;
$$;

-- ---------------------------------------------------------------------------
-- Lock the direct send path: clients must now send via the worker gateway.
-- (No internal DB caller uses send_message — system/Lana messages are inserted
-- directly by other functions — so this is safe.)
-- ---------------------------------------------------------------------------

revoke execute on function public.send_message(uuid, text, uuid, uuid) from public, anon, authenticated;
grant execute on function public.send_message(uuid, text, uuid, uuid) to service_role;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public.worker_send_message(uuid, uuid, text, uuid, uuid) from public, anon, authenticated;
grant execute on function public.worker_send_message(uuid, uuid, text, uuid, uuid) to service_role;

revoke all on function public.create_message_hold(uuid, uuid, text, text, boolean, text, jsonb, uuid, uuid) from public, anon, authenticated;
grant execute on function public.create_message_hold(uuid, uuid, text, text, boolean, text, jsonb, uuid, uuid) to service_role;

revoke all on function public.override_held_message(uuid) from public, anon;
grant execute on function public.override_held_message(uuid) to authenticated;

revoke all on function public.get_my_message_holds() from public, anon;
grant execute on function public.get_my_message_holds() to authenticated;
