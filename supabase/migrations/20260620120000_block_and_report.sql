-- Block & report: block_user / unblock_user / report_message + moderation tables.
-- Builds on user_blocks + lana_is_blocked() (added in 20260618). "Blocked" is a
-- parallel lane (the user_blocks row), not a tier value — so no ALTER TYPE on
-- relationship_tier. We record pre_block_tier to restore (and re-shield) on unblock.

-- ---------------------------------------------------------------------------
-- Extend user_blocks to carry restore + safety metadata
-- ---------------------------------------------------------------------------

alter table public.user_blocks
  add column if not exists reason_category text,
  add column if not exists pre_block_tier public.relationship_tier,
  add column if not exists blocked_unblockable boolean not null default false;

-- ---------------------------------------------------------------------------
-- Moderation enums + tables
-- ---------------------------------------------------------------------------

create type public.moderation_report_category as enum (
  'harassment','spam','sexual','self_harm','threat','off_platform_ask','csam','other'
);

create type public.moderation_report_status as enum (
  'open','in_review','resolved','dismissed'
);

create type public.moderation_action_kind as enum (
  'warn','soft_suspend','hard_suspend','ban','message_hold','message_delete','tier_revoke'
);

create type public.moderation_actor as enum (
  'system','human_moderator','auto_safety'
);

create table if not exists public.moderation_reports (
  id uuid primary key default gen_random_uuid(),
  reporter uuid references public.users (id) on delete set null,  -- nullable: anonymized if reporter deleted (reports kept for evidence)
  target_user uuid not null references public.users (id) on delete cascade,
  target_message_id uuid references public.messages (id) on delete set null,
  target_thread_id uuid references public.chat_threads (id) on delete set null,
  category public.moderation_report_category not null,
  description text,
  context jsonb not null default '[]'::jsonb,  -- snapshot of surrounding messages
  status public.moderation_report_status not null default 'open',
  reviewer uuid references public.users (id) on delete set null,
  resolution_note text,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  constraint moderation_reports_distinct check (reporter is distinct from target_user)
);

comment on table public.moderation_reports is
  'User/message reports. Targets cannot see they were reported (RLS: reporter-only read).';

create index if not exists moderation_reports_target_idx
  on public.moderation_reports (target_user, created_at desc);
create index if not exists moderation_reports_reporter_idx
  on public.moderation_reports (reporter, created_at desc);

create table if not exists public.moderation_actions (
  id uuid primary key default gen_random_uuid(),
  target_user uuid not null references public.users (id) on delete cascade,
  related_report uuid references public.moderation_reports (id) on delete set null,
  kind public.moderation_action_kind not null,
  reason text,
  actor public.moderation_actor not null default 'auto_safety',
  actor_user_id uuid references public.users (id) on delete set null,
  expires_at timestamptz,                 -- for soft/hard suspends
  rescinded_at timestamptz,
  rescinded_by uuid references public.users (id) on delete set null,
  created_at timestamptz not null default now()
);

comment on table public.moderation_actions is
  'Actions taken against a user. Targets CAN read their own (so they know they are suspended).';

create index if not exists moderation_actions_target_active_idx
  on public.moderation_actions (target_user)
  where rescinded_at is null;

alter table public.moderation_reports enable row level security;
alter table public.moderation_actions enable row level security;

-- Reporter can see their own reports; target cannot (no policy grants them read).
create policy "moderation_reports_select_own"
  on public.moderation_reports for select
  to authenticated
  using (reporter = auth.uid());

create policy "moderation_reports_no_client_write"
  on public.moderation_reports for all
  to authenticated
  using (false) with check (false);

-- Target reads their own actions (to learn they are suspended).
create policy "moderation_actions_select_target"
  on public.moderation_actions for select
  to authenticated
  using (target_user = auth.uid());

create policy "moderation_actions_no_client_write"
  on public.moderation_actions for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Helper: is this user currently suspended? (for future send-gating)
-- ---------------------------------------------------------------------------

create or replace function public.lana_is_suspended(p_user uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1 from public.moderation_actions
    where target_user = p_user
      and kind in ('soft_suspend','hard_suspend','ban')
      and rescinded_at is null
      and (expires_at is null or expires_at > now())
  );
$$;

comment on function public.lana_is_suspended(uuid) is
  'True if the user has an active (non-rescinded, non-expired) suspend/ban action.';

-- ---------------------------------------------------------------------------
-- RPC: block_user (block + cascade: cancel pending nudges/unmasks, archive chat)
-- ---------------------------------------------------------------------------

create or replace function public.block_user(
  p_blocked_user_id uuid,
  p_reason_category text default null,
  p_reason text default null
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_tier public.relationship_tier;
  v_unblockable boolean;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_blocked_user_id is null or p_blocked_user_id = v_me then
    raise exception 'invalid_target' using errcode = 'P0001';
  end if;

  v_tier := public.get_relationship_tier(p_blocked_user_id);
  -- IRL-peer blocks for safety reasons require support to undo (ATPR §5).
  v_unblockable := (v_tier = 'irl_peer'
    and coalesce(p_reason_category, '') in ('harassment','threat','sexual','self_harm','csam'));

  insert into public.user_blocks (blocker, blocked, reason, reason_category, pre_block_tier, blocked_unblockable)
  values (v_me, p_blocked_user_id, p_reason, p_reason_category, v_tier, v_unblockable)
  on conflict (blocker, blocked) do update
  set reason = excluded.reason,
      reason_category = excluded.reason_category,
      blocked_unblockable = user_blocks.blocked_unblockable or excluded.blocked_unblockable;

  -- Cancel pending nudges in both directions (status enum allows only declined here).
  update public.nudges
  set status = 'declined', responded_at = now()
  where status = 'pending'
    and ((sender_id = v_me and recipient_id = p_blocked_user_id)
      or (sender_id = p_blocked_user_id and recipient_id = v_me));

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_blocked_user_id);

  -- Cancel any pending unmask request for the pair.
  update public.unmask_requests
  set status = 'cancelled', responded_at = now()
  where user_low = v_low and user_high = v_high and status = 'pending';

  -- Archive the shared 1:1 chat.
  update public.chat_threads
  set archived_at = now()
  where user_low = v_low and user_high = v_high
    and kind in ('shielded', 'direct')
    and archived_at is null;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: unblock_user (restore tier, re-shield if was direct, un-archive chat)
-- ---------------------------------------------------------------------------

create or replace function public.unblock_user(p_blocked_user_id uuid)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_pre public.relationship_tier;
  v_unblockable boolean;
  v_restored public.relationship_tier;
  v_current public.relationship_tier;
  v_thread uuid;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select pre_block_tier, blocked_unblockable into v_pre, v_unblockable
  from public.user_blocks
  where blocker = v_me and blocked = p_blocked_user_id;

  if not found then
    raise exception 'not_blocked' using errcode = 'P0001';
  end if;
  if v_unblockable then
    raise exception 'unblock_requires_support' using errcode = 'P0001';
  end if;

  delete from public.user_blocks
  where blocker = v_me and blocked = p_blocked_user_id;

  -- Restored tier: unmask is undone (direct -> acquaintance); irl_peer grandfathered.
  v_restored := case
    when v_pre in ('direct', 'acquaintance') then 'acquaintance'
    when v_pre = 'irl_peer' then 'irl_peer'
    else 'stranger'
  end::public.relationship_tier;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_blocked_user_id);

  select tier into v_current
  from public.user_relationships
  where user_low = v_low and user_high = v_high;
  v_current := coalesce(v_current, 'stranger');

  if public._tier_rank(v_restored) <> public._tier_rank(v_current) then
    update public.user_relationships
    set tier = v_restored, last_transition_at = now(), last_trigger = 'unblock_restore'
    where user_low = v_low and user_high = v_high;

    insert into public.relationship_tier_events (
      user_low, user_high, viewer_user_id, from_tier, to_tier, trigger_event
    )
    values (v_low, v_high, v_me, v_current, v_restored, 'unblock_restore');
  end if;

  -- Un-archive the chat; re-shield if the restored tier is below 'direct'.
  select id into v_thread
  from public.chat_threads
  where user_low = v_low and user_high = v_high
    and kind in ('shielded', 'direct');

  if v_thread is not null then
    update public.chat_threads
    set archived_at = null,
        kind = case when v_restored in ('stranger','nudge','acquaintance') then 'shielded'::public.chat_kind
                    else kind end
    where id = v_thread;
  end if;

  return v_restored;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: report_message (snapshot context + auto-escalation)
-- ---------------------------------------------------------------------------

create or replace function public.report_message(
  p_category text,
  p_message_id uuid default null,
  p_thread_id uuid default null,
  p_target_user_id uuid default null,
  p_description text default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_target uuid;
  v_thread uuid;
  v_context jsonb;
  v_report_id uuid;
  v_recent int;
  v_distinct int;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_category not in ('harassment','spam','sexual','self_harm','threat','off_platform_ask','csam','other') then
    raise exception 'invalid_category' using errcode = 'P0001';
  end if;

  -- Rate limit: 20 reports / day / reporter.
  select count(*) into v_recent
  from public.moderation_reports
  where reporter = v_me and created_at > now() - interval '1 day';
  if v_recent >= 20 then
    raise exception 'report_rate_limit' using errcode = 'P0001';
  end if;

  -- Resolve target + thread from the message if given.
  if p_message_id is not null then
    select sender_id, thread_id into v_target, v_thread
    from public.messages where id = p_message_id;
  end if;
  v_target := coalesce(v_target, p_target_user_id);
  v_thread := coalesce(v_thread, p_thread_id);

  if v_target is null then
    raise exception 'report_target_required' using errcode = 'P0001';
  end if;
  if v_target = v_me then
    raise exception 'cannot_report_self' using errcode = 'P0001';
  end if;

  -- Snapshot up to the last 10 messages in the thread for evidence.
  if v_thread is not null then
    select coalesce(jsonb_agg(s.obj), '[]'::jsonb) into v_context
    from (
      select jsonb_build_object(
        'id', id, 'sender', sender_id, 'content', content, 'sent_at', sent_at
      ) as obj
      from public.messages
      where thread_id = v_thread
      order by sent_at desc
      limit 10
    ) s;
  end if;

  insert into public.moderation_reports (
    reporter, target_user, target_message_id, target_thread_id, category, description, context, status
  )
  values (
    v_me, v_target, p_message_id, v_thread, p_category::public.moderation_report_category,
    p_description, coalesce(v_context, '[]'::jsonb), 'open'
  )
  returning id into v_report_id;

  -- Auto-escalation (ATPR §5 moderation rules).
  if p_category in ('threat','csam','self_harm') then
    insert into public.moderation_actions (target_user, related_report, kind, reason, actor, expires_at)
    values (v_target, v_report_id, 'hard_suspend', 'severe_category_' || p_category, 'auto_safety', now() + interval '7 days');
  else
    select count(distinct reporter) into v_distinct
    from public.moderation_reports
    where target_user = v_target and created_at > now() - interval '7 days';

    if v_distinct >= 5 then
      insert into public.moderation_actions (target_user, related_report, kind, reason, actor, expires_at)
      values (v_target, v_report_id, 'hard_suspend', 'five_reports_7d', 'auto_safety', now() + interval '7 days');
    elsif v_distinct >= 3 then
      insert into public.moderation_actions (target_user, related_report, kind, reason, actor, expires_at)
      values (v_target, v_report_id, 'soft_suspend', 'three_reports_7d', 'auto_safety', now() + interval '24 hours');
    end if;
  end if;

  return v_report_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- Realtime: a suspended user learns mid-session (RLS: target reads own actions).
-- Guarded: only if the supabase_realtime publication exists, and idempotent.
-- ---------------------------------------------------------------------------

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
       where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'moderation_actions'
     ) then
    execute 'alter publication supabase_realtime add table public.moderation_actions';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public.lana_is_suspended(uuid) from public, anon;
grant execute on function public.lana_is_suspended(uuid) to authenticated;

revoke all on function public.block_user(uuid, text, text) from public, anon;
grant execute on function public.block_user(uuid, text, text) to authenticated;

revoke all on function public.unblock_user(uuid) from public, anon;
grant execute on function public.unblock_user(uuid) to authenticated;

revoke all on function public.report_message(text, uuid, uuid, uuid, text) from public, anon;
grant execute on function public.report_message(text, uuid, uuid, uuid, text) to authenticated;
