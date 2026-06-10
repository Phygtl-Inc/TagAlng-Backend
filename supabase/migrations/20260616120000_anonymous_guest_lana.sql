-- Anonymous guest Lana flow (Supabase signInAnonymously → profile_intake → OTP at intro).
-- Peer comms RPCs require a permanent, phone-verified neighbor (not anonymous guest).

create or replace function public.auth_is_anonymous()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select coalesce((auth.jwt()->>'is_anonymous')::boolean, false);
$$;

comment on function public.auth_is_anonymous() is
  'True when the current JWT is a Supabase anonymous session (guest before phone link).';

create or replace function public.auth_is_phone_verified()
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.users u
    where u.id = auth.uid()
      and u.phone_verified_at is not null
  );
$$;

comment on function public.auth_is_phone_verified() is
  'True when public.users.phone_verified_at is set for auth.uid().';

create or replace function public._require_verified_neighbor_comms()
returns void
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if public.auth_is_anonymous() then
    raise exception 'anonymous_user_comms_blocked' using errcode = 'P0001';
  end if;
  if not public.auth_is_phone_verified() then
    raise exception 'phone_not_verified' using errcode = 'P0001';
  end if;
end;
$$;

comment on function public._require_verified_neighbor_comms() is
  'Gate peer-to-peer actions until guest links phone OTP (F2 introduce turn).';

revoke all on function public.auth_is_anonymous() from public;
grant execute on function public.auth_is_anonymous() to authenticated;

revoke all on function public.auth_is_phone_verified() from public;
grant execute on function public.auth_is_phone_verified() to authenticated;

-- Nudges
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
  perform public._require_verified_neighbor_comms();
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
  perform public._require_verified_neighbor_comms();

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
  perform public._require_verified_neighbor_comms();
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
  perform public._require_verified_neighbor_comms();

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
  perform public._require_verified_neighbor_comms();

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
  perform public._require_verified_neighbor_comms();
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
  perform public._require_verified_neighbor_comms();

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

-- Ops: delete stale anonymous auth users with no linked phone (run via cron).
create or replace function public.cleanup_stale_anonymous_users(p_older_than interval default interval '30 days')
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_deleted int;
begin
  with doomed as (
    select u.id
    from auth.users u
    left join public.users p on p.id = u.id
    where u.is_anonymous is true
      and u.created_at < now() - p_older_than
      and (p.phone_verified_at is null)
  )
  delete from auth.users au
  using doomed d
  where au.id = d.id;

  get diagnostics v_deleted = row_count;
  return v_deleted;
end;
$$;

revoke all on function public.cleanup_stale_anonymous_users(interval) from public;
grant execute on function public.cleanup_stale_anonymous_users(interval) to service_role;
