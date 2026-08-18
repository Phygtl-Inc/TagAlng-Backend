-- ---------------------------------------------------------------------------
-- You can nudge the people you actually share a place with.
-- ---------------------------------------------------------------------------
-- Nudges and intros were gated on `_users_share_home_block`, written when the
-- block was the only tie the product had. Communities are the other one: a gym
-- in St. Cloud draws members from several blocks, the roster already shows them
-- each other's nickname and avatar (§F: named only to its own members), and each
-- row carries a Nudge button. Tapping it raised `candidate_not_on_block` — the
-- product offered an action it then refused, for people it had just introduced
-- by name (2026-08-18).
--
-- So reachability becomes: a shared home block OR a confirmed shared place. The
-- block rule is not loosened — it is joined by the other real tie.
--
-- SAFETY. The block check was also doing duty as a coarse "can these two touch
-- each other" gate, and widening it must not open a door for a pair who have
-- blocked each other: `_users_can_reach` refuses on lana_is_blocked in either
-- direction, which neither send_nudge nor propose_intro checked before. Every
-- other guard is untouched — verified comms, the recipient's consent flag, the
-- 30-day duplicate rule, tier order, and enforce_nudge_limits' cooldowns.
--
-- The error names are deliberately unchanged (`recipient_not_on_block` /
-- `candidate_not_on_block`): the worker maps them to user_not_on_same_block and
-- the copy built on that mapping stays correct — there is still no path between
-- these two people, it is now just a wider test of what counts as one.
--
-- ROLLBACK: re-run 20260616120000_anonymous_guest_lana.sql (send_nudge,
-- propose_intro) and 20260628120000_lana_neighbor_intro.sql, then drop the two
-- helpers below.
-- ---------------------------------------------------------------------------

create or replace function public._users_share_community(p_a uuid, p_b uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.circle_affiliations a
    join public.circle_affiliations b
      on b.place_ref = a.place_ref
     and b.user_id = p_b
     and b.status = 'confirmed'
     and b.dismissed_at is null
    where a.user_id = p_a
      and a.status = 'confirmed'
      and a.dismissed_at is null
      and a.place_ref is not null
  );
$$;

comment on function public._users_share_community(uuid, uuid) is
  'True when both users hold a confirmed, non-dismissed affiliation at the same place. '
  'Members-only disclosure already shows these two each other on the roster, so it is '
  'the same tie that authorizes a nudge between them.';

create or replace function public._users_can_reach(p_a uuid, p_b uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select not public.lana_is_blocked(p_a, p_b)
     and (
       public._users_share_home_block(p_a, p_b)
       or public._users_share_community(p_a, p_b)
     );
$$;

comment on function public._users_can_reach(uuid, uuid) is
  'The one reachability test behind nudges and intros: not mutually blocked, and tied '
  'by a shared home block or a shared confirmed place.';

revoke all on function public._users_share_community(uuid, uuid) from public, anon;
revoke all on function public._users_can_reach(uuid, uuid) from public, anon;
grant execute on function public._users_share_community(uuid, uuid) to authenticated;
grant execute on function public._users_can_reach(uuid, uuid) to authenticated;

-- ── the three callers, unchanged except for that one test ────────────────────

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
  if not public._users_can_reach(auth.uid(), p_recipient_id) then
    raise exception 'recipient_not_on_block' using errcode = 'P0001';
  end if;

  insert into public.nudges (sender_id, recipient_id, context_message, status)
  values (auth.uid(), p_recipient_id, nullif(trim(p_context_message), ''), 'pending')
  returning id into nudge_id;

  perform public.promote_relationship_tier(p_recipient_id, 'nudge_sent', nudge_id);

  return nudge_id;
end;
$$;

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
  if not public._users_can_reach(auth.uid(), p_candidate_id) then
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

create or replace function public.lana_propose_neighbor_intro(
  p_candidate_id uuid,
  p_match_reason text,
  p_shared_dimensions text[] default '{}',
  p_match_score real default null,
  p_nudge_opener text default null,
  p_joint_moment_id uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_tier public.relationship_tier;
  v_nudge_id uuid;
  v_intro_id uuid;
  v_opener text;
  v_dup int;
begin
  perform public._require_verified_neighbor_comms();

  if p_candidate_id is null or p_candidate_id = v_me then
    raise exception 'invalid_candidate' using errcode = 'P0001';
  end if;
  if char_length(trim(p_match_reason)) < 10 then
    raise exception 'match_reason_too_short' using errcode = 'P0001';
  end if;
  if not public._users_can_reach(v_me, p_candidate_id) then
    raise exception 'candidate_not_on_block' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.users u
    where u.id = p_candidate_id and u.consent_to_receive_intros = true
  ) then
    raise exception 'candidate_consent_missing' using errcode = 'P0001';
  end if;

  select count(*)::int into v_dup
  from public.intros i
  where (
    (i.initiator_id = v_me and i.candidate_id = p_candidate_id)
    or (i.initiator_id = p_candidate_id and i.candidate_id = v_me)
  )
    and i.created_at > now() - interval '30 days'
    and i.status in ('proposed', 'accepted');

  if v_dup > 0 then
    raise exception 'duplicate_intro_recent' using errcode = 'P0001';
  end if;

  v_tier := public.get_relationship_tier(p_candidate_id);
  if public._tier_rank(v_tier) < public._tier_rank('nudge'::public.relationship_tier) then
    v_opener := nullif(trim(p_nudge_opener), '');
    if v_opener is null then
      v_opener := left(
        format('Hi — Lana thought we might click. %s', trim(p_match_reason)),
        280
      );
    end if;
    v_nudge_id := public.send_nudge(p_candidate_id, v_opener);
  end if;

  v_intro_id := public.propose_intro(
    p_candidate_id,
    trim(p_match_reason),
    coalesce(p_shared_dimensions, '{}'),
    p_match_score,
    p_joint_moment_id
  );

  return jsonb_build_object(
    'intro_id', v_intro_id,
    'nudge_id', v_nudge_id,
    'candidate_user_id', p_candidate_id,
    'match_reason', trim(p_match_reason),
    'shared_dimensions', coalesce(p_shared_dimensions, '{}'),
    'status', 'proposed'
  );
end;
$$;

revoke all on function public.send_nudge(uuid, text) from public, anon;
grant execute on function public.send_nudge(uuid, text) to authenticated;
revoke all on function public.propose_intro(uuid, text, text[], real, uuid) from public, anon;
grant execute on function public.propose_intro(uuid, text, text[], real, uuid) to authenticated;
revoke all on function public.lana_propose_neighbor_intro(uuid, text, text[], real, text, uuid)
  from public, anon;
grant execute on function public.lana_propose_neighbor_intro(uuid, text, text[], real, text, uuid)
  to authenticated;
