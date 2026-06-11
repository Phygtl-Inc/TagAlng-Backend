-- IRL-peer promotion (Path 2): direct -> irl_peer.
-- Two paths: (a) manual mutual "we met IRL" confirm, (b) auto after both attended
-- the same event + 24h grace. Linear ladder enforced: only pairs already at
-- 'direct' (i.e. unmasked) are promoted — co-attending alone never skips ahead.
-- Cron/auto path needs no auth.uid(), so promotion uses an explicit-pair helper
-- rather than promote_relationship_tier (which keys off auth.uid()).

-- ---------------------------------------------------------------------------
-- Sub-tables
-- ---------------------------------------------------------------------------

create table if not exists public.irl_confirmations (
  user_low uuid not null references public.users (id) on delete cascade,
  user_high uuid not null references public.users (id) on delete cascade,
  confirmed_by uuid not null references public.users (id) on delete cascade,
  confirmed_at timestamptz not null default now(),
  primary key (user_low, user_high, confirmed_by),
  constraint irl_confirmations_pair_ordered check (user_low < user_high)
);

comment on table public.irl_confirmations is
  'Manual "we met in real life" confirmations. Both parties present -> promote to irl_peer.';

create table if not exists public.irl_attendance_processed (
  event_id uuid primary key references public.events (id) on delete cascade,
  processed_at timestamptz not null default now()
);

comment on table public.irl_attendance_processed is
  'Marker so the auto-IRL cron processes each ended event only once.';

alter table public.irl_confirmations enable row level security;
alter table public.irl_attendance_processed enable row level security;

create policy "irl_confirmations_select_parties"
  on public.irl_confirmations for select
  to authenticated
  using (user_low = auth.uid() or user_high = auth.uid());

create policy "irl_confirmations_no_client_write"
  on public.irl_confirmations for all
  to authenticated
  using (false) with check (false);

-- irl_attendance_processed is internal: no client read/write policies (default deny).
create policy "irl_attendance_processed_no_client"
  on public.irl_attendance_processed for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Helper: promote a specific pair direct -> irl_peer (explicit users, no auth.uid)
-- ---------------------------------------------------------------------------

create or replace function public._promote_pair_to_irl(
  p_a uuid,
  p_b uuid,
  p_proof_id uuid default null
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_low uuid;
  v_high uuid;
  v_current public.relationship_tier;
begin
  if p_a is null or p_b is null or p_a = p_b then
    return false;
  end if;
  if public.lana_is_blocked(p_a, p_b) then
    return false;
  end if;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(p_a, p_b);

  select tier into v_current
  from public.user_relationships
  where user_low = v_low and user_high = v_high;
  v_current := coalesce(v_current, 'stranger');

  -- Linear ladder: only unmasked (direct) pairs become irl_peer.
  if v_current <> 'direct' then
    return false;
  end if;

  update public.user_relationships
  set tier = 'irl_peer', last_transition_at = now(), last_trigger = 'irl_met'
  where user_low = v_low and user_high = v_high;

  insert into public.relationship_tier_events (
    user_low, user_high, viewer_user_id, from_tier, to_tier, trigger_event, proof_id
  )
  values (v_low, v_high, v_low, 'direct', 'irl_peer', 'irl_met', p_proof_id);

  return true;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: confirm_irl_met (manual, mutual)
-- ---------------------------------------------------------------------------

create or replace function public.confirm_irl_met(p_other_user_id uuid)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_tier public.relationship_tier;
  v_both boolean;
begin
  perform public._require_verified_neighbor_comms();

  if p_other_user_id is null or p_other_user_id = v_me then
    raise exception 'invalid_other_user' using errcode = 'P0001';
  end if;

  v_tier := public.get_relationship_tier(p_other_user_id);
  if v_tier <> 'direct' then
    raise exception 'must_be_direct_to_confirm_irl' using errcode = 'P0001';
  end if;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_other_user_id);

  insert into public.irl_confirmations (user_low, user_high, confirmed_by)
  values (v_low, v_high, v_me)
  on conflict (user_low, user_high, confirmed_by) do nothing;

  select count(distinct confirmed_by) >= 2 into v_both
  from public.irl_confirmations
  where user_low = v_low and user_high = v_high;

  if v_both then
    perform public._promote_pair_to_irl(v_me, p_other_user_id, null);
  end if;

  return public.get_relationship_tier(p_other_user_id);
end;
$$;

-- ---------------------------------------------------------------------------
-- Cron: auto-promote co-attendees of ended events (ends_at + 24h)
-- ---------------------------------------------------------------------------

create or replace function public.promote_irl_from_attendance()
returns int
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_event uuid;
  v_pair record;
  v_count int := 0;
begin
  for v_event in
    select e.id
    from public.events e
    where e.ends_at is not null
      and e.ends_at + interval '24 hours' < now()
      and not exists (
        select 1 from public.irl_attendance_processed p where p.event_id = e.id
      )
  loop
    for v_pair in
      with attendees as (
        select requester_id as uid
        from public.event_requests
        where event_id = v_event and status = 'attended'
        union
        select host_id from public.events where id = v_event
      )
      select a.uid as ua, b.uid as ub
      from attendees a
      join attendees b on a.uid < b.uid
    loop
      if public._promote_pair_to_irl(v_pair.ua, v_pair.ub, v_event) then
        v_count := v_count + 1;
      end if;
    end loop;

    insert into public.irl_attendance_processed (event_id)
    values (v_event)
    on conflict (event_id) do nothing;
  end loop;

  return v_count;
end;
$$;

comment on function public.promote_irl_from_attendance() is
  'Promotes direct-tier co-attendees of ended events to irl_peer. Schedule every 15 min (service_role).';

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public._promote_pair_to_irl(uuid, uuid, uuid) from public, anon, authenticated;
grant execute on function public._promote_pair_to_irl(uuid, uuid, uuid) to service_role;

revoke all on function public.confirm_irl_met(uuid) from public, anon;
grant execute on function public.confirm_irl_met(uuid) to authenticated;

revoke all on function public.promote_irl_from_attendance() from public, anon, authenticated;
grant execute on function public.promote_irl_from_attendance() to service_role;
