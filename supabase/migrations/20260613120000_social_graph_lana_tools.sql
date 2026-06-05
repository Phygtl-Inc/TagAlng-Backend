-- Social graph: relationship tiers, nudge accept, intros, co-host invites (Lana tools v0.1)

drop function if exists public.send_nudge(uuid);
drop function if exists public.get_my_nudges(text);

create type public.relationship_tier as enum (
  'stranger',
  'nudge',
  'acquaintance',
  'direct',
  'irl_peer'
);

comment on type public.relationship_tier is
  'Pairwise neighbor disclosure ladder. Event-driven promotion only.';

-- Canonical unordered pair (user_low < user_high by uuid)
create table if not exists public.user_relationships (
  user_low uuid not null references public.users (id) on delete cascade,
  user_high uuid not null references public.users (id) on delete cascade,
  tier public.relationship_tier not null default 'stranger',
  last_transition_at timestamptz,
  last_trigger text,
  primary key (user_low, user_high),
  constraint user_relationships_ordered check (user_low < user_high)
);

comment on table public.user_relationships is
  'Pairwise tier between neighbors on a block. Promoted by event triggers only.';

create index if not exists user_relationships_low_idx on public.user_relationships (user_low);
create index if not exists user_relationships_high_idx on public.user_relationships (user_high);

create table if not exists public.relationship_tier_events (
  id uuid primary key default gen_random_uuid(),
  user_low uuid not null references public.users (id) on delete cascade,
  user_high uuid not null references public.users (id) on delete cascade,
  viewer_user_id uuid not null references public.users (id) on delete cascade,
  from_tier public.relationship_tier,
  to_tier public.relationship_tier not null,
  trigger_event text not null,
  proof_id uuid,
  created_at timestamptz not null default now()
);

create index if not exists relationship_tier_events_pair_idx
  on public.relationship_tier_events (user_low, user_high, created_at desc);

alter table public.user_relationships enable row level security;
alter table public.relationship_tier_events enable row level security;

create policy "user_relationships_select_involved"
  on public.user_relationships for select
  to authenticated
  using (auth.uid() = user_low or auth.uid() = user_high);

create policy "user_relationships_no_client_write"
  on public.user_relationships for all
  to authenticated
  using (false)
  with check (false);

create policy "tier_events_select_involved"
  on public.relationship_tier_events for select
  to authenticated
  using (
    auth.uid() = user_low
    or auth.uid() = user_high
    or auth.uid() = viewer_user_id
  );

create policy "tier_events_no_client_write"
  on public.relationship_tier_events for all
  to authenticated
  using (false)
  with check (false);

alter table public.users
  add column if not exists consent_to_receive_intros boolean not null default true;

-- Extend nudges
alter table public.nudges
  add column if not exists status text not null default 'pending';

alter table public.nudges
  add column if not exists context_message text;

alter table public.nudges
  add column if not exists responded_at timestamptz;

alter table public.nudges
  drop constraint if exists nudges_status_check;

alter table public.nudges
  add constraint nudges_status_check
  check (status in ('pending', 'accepted', 'declined'));

-- Intros
create table if not exists public.intros (
  id uuid primary key default gen_random_uuid(),
  initiator_id uuid not null references public.users (id) on delete cascade,
  candidate_id uuid not null references public.users (id) on delete cascade,
  match_score real,
  match_reason text not null check (char_length(match_reason) between 10 and 280),
  shared_dimensions text[] not null default '{}',
  joint_moment_id uuid,
  status text not null default 'proposed'
    check (status in ('proposed', 'accepted', 'declined', 'expired')),
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint intros_distinct_users check (initiator_id <> candidate_id)
);

create index if not exists intros_initiator_idx on public.intros (initiator_id, created_at desc);
create index if not exists intros_candidate_idx on public.intros (candidate_id, created_at desc);
create index if not exists intros_pair_recent_idx
  on public.intros (initiator_id, candidate_id, created_at desc);

alter table public.intros enable row level security;

create policy "intros_select_parties"
  on public.intros for select
  to authenticated
  using (initiator_id = auth.uid() or candidate_id = auth.uid());

create policy "intros_no_client_write"
  on public.intros for all
  to authenticated
  using (false)
  with check (false);

-- Co-host invites
create table if not exists public.event_cohost_invites (
  id uuid primary key default gen_random_uuid(),
  event_id uuid references public.events (id) on delete cascade,
  host_id uuid not null references public.users (id) on delete cascade,
  candidate_id uuid not null references public.users (id) on delete cascade,
  overlap_reason text not null check (char_length(overlap_reason) between 10 and 280),
  session_id uuid references public.lana_sessions (id) on delete set null,
  status text not null default 'proposed'
    check (status in ('proposed', 'accepted', 'declined')),
  created_at timestamptz not null default now(),
  responded_at timestamptz,
  constraint event_cohost_distinct check (host_id <> candidate_id)
);

create index if not exists event_cohost_host_idx on public.event_cohost_invites (host_id, created_at desc);
create index if not exists event_cohost_candidate_idx on public.event_cohost_invites (candidate_id, created_at desc);

alter table public.events
  add column if not exists cohost_id uuid references public.users (id) on delete set null;

alter table public.event_cohost_invites enable row level security;

create policy "cohost_invites_select_parties"
  on public.event_cohost_invites for select
  to authenticated
  using (host_id = auth.uid() or candidate_id = auth.uid());

create policy "cohost_invites_no_client_write"
  on public.event_cohost_invites for all
  to authenticated
  using (false)
  with check (false);

-- Internal helpers
create or replace function public._relationship_pair(p_a uuid, p_b uuid)
returns table (user_low uuid, user_high uuid)
language sql
immutable
as $$
  select least(p_a, p_b), greatest(p_a, p_b);
$$;

create or replace function public._tier_rank(p_tier public.relationship_tier)
returns int
language sql
immutable
as $$
  select case p_tier
    when 'stranger' then 0
    when 'nudge' then 1
    when 'acquaintance' then 2
    when 'direct' then 3
    when 'irl_peer' then 4
  end;
$$;

create or replace function public._tier_max(p_a public.relationship_tier, p_b public.relationship_tier)
returns public.relationship_tier
language sql
immutable
as $$
  select case
    when public._tier_rank(p_a) >= public._tier_rank(p_b) then p_a
    else p_b
  end;
$$;

create or replace function public._users_share_home_block(p_a uuid, p_b uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.users ua
    join public.users ub on ub.id = p_b
    where ua.id = p_a
      and ua.home_block_id is not null
      and ua.home_block_id = ub.home_block_id
  );
$$;

create or replace function public.get_relationship_tier(p_other_user_id uuid)
returns public.relationship_tier
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_tier public.relationship_tier;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_other_user_id is null or p_other_user_id = v_me then
    return 'stranger';
  end if;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_other_user_id);

  select ur.tier into v_tier
  from public.user_relationships ur
  where ur.user_low = v_low and ur.user_high = v_high;

  return coalesce(v_tier, 'stranger');
end;
$$;

create or replace function public.get_relationship_tiers_for_user(
  p_user_id uuid,
  p_other_user_ids uuid[]
)
returns table (other_user_id uuid, tier public.relationship_tier)
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_other uuid;
  v_low uuid;
  v_high uuid;
  v_tier public.relationship_tier;
begin
  if p_user_id is null then
    return;
  end if;

  foreach v_other in array coalesce(p_other_user_ids, '{}')
  loop
    if v_other is null or v_other = p_user_id then
      continue;
    end if;
    select user_low, user_high into v_low, v_high
    from public._relationship_pair(p_user_id, v_other);
    select ur.tier into v_tier
    from public.user_relationships ur
    where ur.user_low = v_low and ur.user_high = v_high;
    other_user_id := v_other;
    tier := coalesce(v_tier, 'stranger');
    return next;
  end loop;
end;
$$;

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
    'nudge_sent', 'nudge_accepted', 'intro_accepted', 'rsvp_attended_same_event'
  ) then
    raise exception 'invalid_tier_trigger' using errcode = 'P0001';
  end if;

  v_target := case p_trigger
    when 'nudge_sent' then 'nudge'
    when 'nudge_accepted' then 'acquaintance'
    when 'intro_accepted' then 'acquaintance'
    when 'rsvp_attended_same_event' then 'acquaintance'
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

-- Nudges: send + accept
create or replace function public.send_nudge(
  p_recipient_id uuid,
  p_context_message text default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  nudge_id uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_recipient_id is null or p_recipient_id = auth.uid() then
    raise exception 'invalid_recipient' using errcode = 'P0001';
  end if;
  if not public._users_share_home_block(auth.uid(), p_recipient_id) then
    raise exception 'recipient_not_on_block' using errcode = 'P0001';
  end if;

  insert into public.nudges (sender_id, recipient_id, context_message, status)
  values (auth.uid(), p_recipient_id, nullif(trim(p_context_message), ''), 'pending')
  returning id into nudge_id;

  perform public.promote_relationship_tier(p_recipient_id, 'nudge_sent', nudge_id);

  return nudge_id;
end;
$$;

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
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

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
  return v_new_tier;
end;
$$;

create or replace function public.decline_nudge(p_nudge_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.nudges n
  set status = 'declined',
      responded_at = now()
  where n.id = p_nudge_id
    and n.recipient_id = auth.uid()
    and n.status = 'pending';

  if not found then
    raise exception 'nudge_not_found_or_already_handled' using errcode = 'P0001';
  end if;
end;
$$;

-- Intros
create or replace function public.propose_intro(
  p_candidate_id uuid,
  p_match_reason text,
  p_shared_dimensions text[] default '{}',
  p_match_score real default null,
  p_joint_moment_id uuid default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_intro_id uuid;
  v_tier public.relationship_tier;
  v_dup int;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_candidate_id is null or p_candidate_id = auth.uid() then
    raise exception 'invalid_candidate' using errcode = 'P0001';
  end if;
  if char_length(trim(p_match_reason)) < 10 then
    raise exception 'match_reason_too_short' using errcode = 'P0001';
  end if;
  if not public._users_share_home_block(auth.uid(), p_candidate_id) then
    raise exception 'candidate_not_on_block' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.users u
    where u.id = auth.uid() and u.phone_verified_at is not null
  ) then
    raise exception 'phone_not_verified' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.users u
    where u.id = p_candidate_id and u.consent_to_receive_intros = true
  ) then
    raise exception 'candidate_consent_missing' using errcode = 'P0001';
  end if;

  v_tier := public.get_relationship_tier(p_candidate_id);
  if public._tier_rank(v_tier) < public._tier_rank('nudge'::public.relationship_tier) then
    raise exception 'tier_too_low_send_nudge_first' using errcode = 'P0001';
  end if;

  select count(*)::int into v_dup
  from public.intros i
  where (
    (i.initiator_id = auth.uid() and i.candidate_id = p_candidate_id)
    or (i.initiator_id = p_candidate_id and i.candidate_id = auth.uid())
  )
    and i.created_at > now() - interval '30 days'
    and i.status in ('proposed', 'accepted');

  if v_dup > 0 then
    raise exception 'duplicate_intro_recent' using errcode = 'P0001';
  end if;

  insert into public.intros (
    initiator_id,
    candidate_id,
    match_score,
    match_reason,
    shared_dimensions,
    joint_moment_id,
    expires_at
  )
  values (
    auth.uid(),
    p_candidate_id,
    p_match_score,
    trim(p_match_reason),
    coalesce(p_shared_dimensions, '{}'),
    p_joint_moment_id,
    now() + interval '72 hours'
  )
  returning id into v_intro_id;

  return v_intro_id;
end;
$$;

create or replace function public.accept_intro(p_intro_id uuid)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_initiator uuid;
  v_new_tier public.relationship_tier;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.intros i
  set status = 'accepted'
  where i.id = p_intro_id
    and i.candidate_id = auth.uid()
    and i.status = 'proposed'
    and i.expires_at > now()
  returning i.initiator_id into v_initiator;

  if v_initiator is null then
    raise exception 'intro_not_found_or_expired' using errcode = 'P0001';
  end if;

  v_new_tier := public.promote_relationship_tier(v_initiator, 'intro_accepted', p_intro_id);
  return v_new_tier;
end;
$$;

create or replace function public.decline_intro(p_intro_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.intros i
  set status = 'declined'
  where i.id = p_intro_id
    and i.candidate_id = auth.uid()
    and i.status = 'proposed';

  if not found then
    raise exception 'intro_not_found' using errcode = 'P0001';
  end if;
end;
$$;

-- Co-host
create or replace function public.propose_cohost(
  p_candidate_id uuid,
  p_overlap_reason text,
  p_event_id uuid default null,
  p_session_id uuid default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_invite_id uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_candidate_id is null or p_candidate_id = auth.uid() then
    raise exception 'invalid_candidate' using errcode = 'P0001';
  end if;
  if char_length(trim(p_overlap_reason)) < 10 then
    raise exception 'overlap_reason_too_short' using errcode = 'P0001';
  end if;
  if not public._users_share_home_block(auth.uid(), p_candidate_id) then
    raise exception 'candidate_not_on_block' using errcode = 'P0001';
  end if;
  if p_event_id is not null and not exists (
    select 1 from public.events e
    where e.id = p_event_id and e.host_id = auth.uid()
  ) then
    raise exception 'not_event_host' using errcode = 'P0001';
  end if;

  insert into public.event_cohost_invites (
    event_id,
    host_id,
    candidate_id,
    overlap_reason,
    session_id
  )
  values (
    p_event_id,
    auth.uid(),
    p_candidate_id,
    trim(p_overlap_reason),
    p_session_id
  )
  returning id into v_invite_id;

  return v_invite_id;
end;
$$;

create or replace function public.accept_cohost_invite(p_invite_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_event_id uuid;
  v_host_id uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.event_cohost_invites i
  set status = 'accepted',
      responded_at = now()
  where i.id = p_invite_id
    and i.candidate_id = auth.uid()
    and i.status = 'proposed'
  returning i.event_id, i.host_id into v_event_id, v_host_id;

  if v_host_id is null then
    raise exception 'cohost_invite_not_found' using errcode = 'P0001';
  end if;

  if v_event_id is not null then
    update public.events e
    set cohost_id = auth.uid(),
        updated_at = now()
    where e.id = v_event_id
      and e.host_id = v_host_id;
  end if;

  return v_event_id;
end;
$$;

-- Refresh nudges list with status
create or replace function public.get_my_nudges(p_direction text default 'received')
returns table (
  id uuid,
  other_user_id uuid,
  nickname text,
  avatar_url text,
  sent_at timestamptz,
  status text,
  context_message text,
  shared_count int
)
language sql
security invoker
set search_path = pg_catalog, public
stable
as $$
  select
    n.id,
    case when p_direction = 'sent' then n.recipient_id else n.sender_id end,
    u.nickname,
    u.profile_photo_url,
    n.sent_at,
    n.status,
    n.context_message,
    0
  from public.nudges n
  join public.users u on u.id = case when p_direction = 'sent' then n.recipient_id else n.sender_id end
  where (
    (p_direction = 'received' and n.recipient_id = auth.uid())
    or (p_direction = 'sent' and n.sender_id = auth.uid())
  )
  order by n.sent_at desc;
$$;

-- create_event: optional cohost_id (must match accepted invite)
create or replace function public.create_event(p_fields jsonb)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public, extensions
as $$
declare
  new_id uuid;
  v_lat double precision;
  v_lng double precision;
  v_tags text[];
  v_cohost uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_lat := (p_fields->>'lat')::double precision;
  v_lng := (p_fields->>'lng')::double precision;

  if v_lat is null or v_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;

  if p_fields->>'title' is null or char_length(p_fields->>'title') < 1 then
    raise exception 'title_required' using errcode = 'P0001';
  end if;

  select coalesce(array_agg(t), '{}')
  into v_tags
  from jsonb_array_elements_text(coalesce(p_fields->'cohort_tags', '[]'::jsonb)) as t;

  if not public.validate_event_cohort_tags(v_tags) then
    raise exception 'invalid_cohort' using errcode = 'P0001';
  end if;

  v_cohost := nullif(p_fields->>'cohost_id', '')::uuid;
  if v_cohost is not null then
    if not exists (
      select 1
      from public.event_cohost_invites i
      where i.host_id = auth.uid()
        and i.candidate_id = v_cohost
        and i.status = 'accepted'
    ) then
      raise exception 'cohost_not_accepted' using errcode = 'P0001';
    end if;
  end if;

  insert into public.events (
    host_id,
    cohost_id,
    cluster_id,
    block_id,
    title,
    description,
    starts_at,
    ends_at,
    location,
    venue_name,
    cohort_tags,
    max_attendees,
    cover_image_url
  )
  values (
    auth.uid(),
    v_cohost,
    coalesce(p_fields->>'cluster_id', 'lake-nona'),
    p_fields->>'block_id',
    p_fields->>'title',
    p_fields->>'description',
    coalesce((p_fields->>'starts_at')::timestamptz, now() + interval '7 days'),
    (p_fields->>'ends_at')::timestamptz,
    extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography,
    p_fields->>'venue_name',
    v_tags,
    (p_fields->>'max_attendees')::integer,
    p_fields->>'cover_image_url'
  )
  returning id into new_id;

  if v_cohost is not null then
    update public.event_cohost_invites i
    set event_id = new_id
    where i.host_id = auth.uid()
      and i.candidate_id = v_cohost
      and i.status = 'accepted'
      and i.event_id is null;
  end if;

  return new_id;
end;
$$;

-- Grants
revoke all on function public.get_relationship_tier(uuid) from public, anon;
grant execute on function public.get_relationship_tier(uuid) to authenticated;

revoke all on function public.get_relationship_tiers_for_user(uuid, uuid[]) from public, anon, authenticated;
grant execute on function public.get_relationship_tiers_for_user(uuid, uuid[]) to service_role;

revoke all on function public.promote_relationship_tier(uuid, text, uuid) from public, anon;
grant execute on function public.promote_relationship_tier(uuid, text, uuid) to authenticated;

revoke all on function public.send_nudge(uuid, text) from public, anon;
grant execute on function public.send_nudge(uuid, text) to authenticated;

revoke all on function public.accept_nudge(uuid) from public, anon;
grant execute on function public.accept_nudge(uuid) to authenticated;

revoke all on function public.decline_nudge(uuid) from public, anon;
grant execute on function public.decline_nudge(uuid) to authenticated;

revoke all on function public.propose_intro(uuid, text, text[], real, uuid) from public, anon;
grant execute on function public.propose_intro(uuid, text, text[], real, uuid) to authenticated;

revoke all on function public.accept_intro(uuid) from public, anon;
grant execute on function public.accept_intro(uuid) to authenticated;

revoke all on function public.decline_intro(uuid) from public, anon;
grant execute on function public.decline_intro(uuid) to authenticated;

revoke all on function public.propose_cohost(uuid, text, uuid, uuid) from public, anon;
grant execute on function public.propose_cohost(uuid, text, uuid, uuid) to authenticated;

revoke all on function public.accept_cohost_invite(uuid) from public, anon;
grant execute on function public.accept_cohost_invite(uuid) to authenticated;
