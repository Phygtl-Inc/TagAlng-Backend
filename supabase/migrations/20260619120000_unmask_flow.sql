-- Unmask flow: acquaintance -> direct (mutual reveal of real names).
-- Path 1 of the doc, completed: nudge -> shielded chat -> UNMASK -> direct.
-- A proposes, B accepts -> tier promotes to 'direct' AND the existing shielded
-- chat thread flips to kind='direct'. Mutual consent only (ATPR invariant 8).

-- ---------------------------------------------------------------------------
-- Status enum
-- ---------------------------------------------------------------------------

create type public.unmask_status as enum (
  'pending',    -- proposer asked; waiting on responder
  'accepted',   -- both consented; tier promoted to direct
  'declined',   -- responder said no (48h cooldown)
  'expired',    -- 48h passed with no response
  'cancelled'   -- block / account-delete cascade (set by other features)
);

-- ---------------------------------------------------------------------------
-- unmask_requests (one pending per pair)
-- ---------------------------------------------------------------------------

create table if not exists public.unmask_requests (
  id uuid primary key default gen_random_uuid(),
  -- ordered pair, so "one pending per pair" is enforceable regardless of direction
  user_low uuid not null references public.users (id) on delete cascade,
  user_high uuid not null references public.users (id) on delete cascade,
  proposer uuid not null references public.users (id) on delete cascade,
  responder uuid not null references public.users (id) on delete cascade,
  status public.unmask_status not null default 'pending',
  proposed_at timestamptz not null default now(),
  responded_at timestamptz,
  declined_by uuid references public.users (id) on delete set null,
  expires_at timestamptz not null default (now() + interval '48 hours'),
  created_at timestamptz not null default now(),
  constraint unmask_requests_distinct check (proposer <> responder),
  constraint unmask_requests_pair_ordered check (user_low < user_high)
);

comment on table public.unmask_requests is
  'Mutual unmask proposals. Proposer auto-consents at INSERT; responder accept promotes the pair to direct.';

-- One pending request per pair (in either direction).
create unique index if not exists unmask_requests_pending_uniq
  on public.unmask_requests (user_low, user_high)
  where status = 'pending';

create index if not exists unmask_requests_responder_idx
  on public.unmask_requests (responder, status, proposed_at desc);

create index if not exists unmask_requests_expiry_idx
  on public.unmask_requests (expires_at) where status = 'pending';

alter table public.unmask_requests enable row level security;

create policy "unmask_requests_select_parties"
  on public.unmask_requests for select
  to authenticated
  using (proposer = auth.uid() or responder = auth.uid());

create policy "unmask_requests_no_client_write"
  on public.unmask_requests for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Extend promote_relationship_tier to support the unmask_accepted -> direct edge.
-- Faithful copy of the 20260613 definition + the new trigger/target case.
-- (Runs after 0613, so this version wins. Grants persist across replace.)
-- ---------------------------------------------------------------------------

create or replace function public.promote_relationship_tier(
  p_other_user_id uuid,
  p_trigger text,
  p_proof_id uuid default null
)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_current public.relationship_tier := 'stranger';
  v_target public.relationship_tier;
  v_new public.relationship_tier;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_other_user_id is null or p_other_user_id = v_me then
    raise exception 'invalid_other_user' using errcode = 'P0001';
  end if;

  if p_trigger not in (
    'nudge_sent', 'nudge_accepted', 'intro_accepted', 'rsvp_attended_same_event', 'unmask_accepted'
  ) then
    raise exception 'invalid_tier_trigger' using errcode = 'P0001';
  end if;

  v_target := case p_trigger
    when 'nudge_sent' then 'nudge'
    when 'nudge_accepted' then 'acquaintance'
    when 'intro_accepted' then 'acquaintance'
    when 'rsvp_attended_same_event' then 'acquaintance'
    when 'unmask_accepted' then 'direct'
  end;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_other_user_id);

  select ur.tier into v_current
  from public.user_relationships ur
  where ur.user_low = v_low and ur.user_high = v_high;

  v_current := coalesce(v_current, 'stranger');
  v_new := public._tier_max(v_current, v_target);

  if v_new = v_current then
    return v_current;
  end if;

  insert into public.user_relationships (user_low, user_high, tier, last_transition_at, last_trigger)
  values (v_low, v_high, v_new, now(), p_trigger)
  on conflict (user_low, user_high) do update
  set tier = excluded.tier,
      last_transition_at = excluded.last_transition_at,
      last_trigger = excluded.last_trigger;

  insert into public.relationship_tier_events (
    user_low, user_high, viewer_user_id, from_tier, to_tier, trigger_event, proof_id
  )
  values (v_low, v_high, v_me, v_current, v_new, p_trigger, p_proof_id);

  return v_new;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: propose_unmask
-- ---------------------------------------------------------------------------

create or replace function public.propose_unmask(p_other_user_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_tier public.relationship_tier;
  v_id uuid;
begin
  perform public._require_verified_neighbor_comms();

  if p_other_user_id is null or p_other_user_id = v_me then
    raise exception 'invalid_other_user' using errcode = 'P0001';
  end if;
  if public.lana_is_blocked(v_me, p_other_user_id) then
    raise exception 'blocked' using errcode = 'P0001';
  end if;

  -- Must currently be acquaintances to propose unmask (linear ladder).
  v_tier := public.get_relationship_tier(p_other_user_id);
  if v_tier <> 'acquaintance' then
    raise exception 'must_be_acquaintance_to_unmask' using errcode = 'P0001';
  end if;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_other_user_id);

  -- One pending per pair.
  if exists (
    select 1 from public.unmask_requests
    where user_low = v_low and user_high = v_high and status = 'pending'
  ) then
    raise exception 'unmask_already_pending' using errcode = 'P0001';
  end if;

  -- 48h cooldown after a decline (ATPR §K).
  if exists (
    select 1 from public.unmask_requests
    where user_low = v_low and user_high = v_high
      and status = 'declined'
      and responded_at > now() - interval '48 hours'
  ) then
    raise exception 'unmask_cooldown' using errcode = 'P0001';
  end if;

  insert into public.unmask_requests (user_low, user_high, proposer, responder, status, expires_at)
  values (v_low, v_high, v_me, p_other_user_id, 'pending', now() + interval '48 hours')
  returning id into v_id;

  return v_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: accept_unmask (mutual consent reached -> promote to direct + flip chat)
-- ---------------------------------------------------------------------------

create or replace function public.accept_unmask(p_request_id uuid)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_proposer uuid;
  v_low uuid;
  v_high uuid;
  v_thread uuid;
  v_new_tier public.relationship_tier;
begin
  perform public._require_verified_neighbor_comms();

  -- Lock the request; second concurrent accept finds nothing and is a no-op race-loser.
  select proposer into v_proposer
  from public.unmask_requests
  where id = p_request_id
    and responder = v_me
    and status = 'pending'
    and expires_at > now()
  for update;

  if v_proposer is null then
    raise exception 'unmask_not_found_or_expired' using errcode = 'P0001';
  end if;

  update public.unmask_requests
  set status = 'accepted', responded_at = now()
  where id = p_request_id;

  -- Promote the pair to 'direct' (logs a relationship_tier_event).
  v_new_tier := public.promote_relationship_tier(v_proposer, 'unmask_accepted', p_request_id);

  -- Flip the existing relationship chat to 'direct' (or open one if none exists,
  -- e.g. acquaintance reached via intro without a prior chat).
  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, v_proposer);

  select id into v_thread
  from public.chat_threads
  where user_low = v_low and user_high = v_high
    and kind in ('shielded', 'direct');

  if v_thread is not null then
    update public.chat_threads
    set kind = 'direct', last_message_at = now()
    where id = v_thread;
  else
    insert into public.chat_threads (kind, user_low, user_high, created_by)
    values ('direct', v_low, v_high, v_me)
    returning id into v_thread;

    insert into public.chat_thread_members (thread_id, user_id)
    values (v_thread, v_low), (v_thread, v_high)
    on conflict (thread_id, user_id) do nothing;
  end if;

  insert into public.messages (thread_id, sender_id, kind, content)
  values (v_thread, null, 'lana',
    'You both shared your real names. You''re connected directly now.');

  update public.chat_threads set last_message_at = now() where id = v_thread;

  return v_new_tier;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: decline_unmask
-- ---------------------------------------------------------------------------

create or replace function public.decline_unmask(p_request_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
begin
  perform public._require_verified_neighbor_comms();

  update public.unmask_requests
  set status = 'declined', responded_at = now(), declined_by = v_me
  where id = p_request_id
    and responder = v_me
    and status = 'pending';

  if not found then
    raise exception 'unmask_not_found_or_already_handled' using errcode = 'P0001';
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Cron helper: expire stale pending requests (schedule via pg_cron / scheduled fn).
-- ---------------------------------------------------------------------------

create or replace function public.expire_unmasks()
returns int
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_count int;
begin
  update public.unmask_requests
  set status = 'expired'
  where status = 'pending' and expires_at < now();
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.expire_unmasks() is
  'Marks pending unmask requests past expires_at as expired. Schedule hourly (service_role).';

-- ---------------------------------------------------------------------------
-- Realtime: responder observes the proposal; both observe the accept.
-- Guarded: only if the supabase_realtime publication exists, and idempotent.
-- ---------------------------------------------------------------------------

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
       where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'unmask_requests'
     ) then
    execute 'alter publication supabase_realtime add table public.unmask_requests';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public.promote_relationship_tier(uuid, text, uuid) from public, anon;
grant execute on function public.promote_relationship_tier(uuid, text, uuid) to authenticated;

revoke all on function public.propose_unmask(uuid) from public, anon;
grant execute on function public.propose_unmask(uuid) to authenticated;

revoke all on function public.accept_unmask(uuid) from public, anon;
grant execute on function public.accept_unmask(uuid) to authenticated;

revoke all on function public.decline_unmask(uuid) from public, anon;
grant execute on function public.decline_unmask(uuid) to authenticated;

revoke all on function public.expire_unmasks() from public, anon, authenticated;
grant execute on function public.expire_unmasks() to service_role;
