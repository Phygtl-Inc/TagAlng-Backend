-- Lana-mediated neighbor intro: nudge (if stranger) + formal intro record in one step.
-- Used when discovery preview or orchestrator proposes a match with match_reason.

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
  if not public._users_share_home_block(v_me, p_candidate_id) then
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

comment on function public.lana_propose_neighbor_intro(uuid, text, text[], real, text, uuid) is
  'Lana discovery/orchestrator: send nudge when stranger, then propose_intro with match_reason.';

revoke all on function public.lana_propose_neighbor_intro(uuid, text, text[], real, text, uuid)
  from public, anon;
grant execute on function public.lana_propose_neighbor_intro(uuid, text, text[], real, text, uuid)
  to authenticated;
