-- Close the intro/nudge inconsistency: accepting an intro should behave like a mutual
-- nudge — promote to acquaintance AND open the shielded chat. Until now accept_intro
-- only promoted the tier, leaving introduced neighbors at 'acquaintance' with nowhere
-- to talk. Faithful copy of the 20260616 accept_intro + the _open_relationship_thread
-- hook (the same one accept_nudge uses). Non-breaking: still returns the new tier.

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

  -- Open the shielded 1:1 chat for the pair (idempotent), matching accept_nudge.
  perform public._open_relationship_thread(auth.uid(), v_initiator);

  return v_new_tier;
end;
$$;

revoke all on function public.accept_intro(uuid) from public, anon;
grant execute on function public.accept_intro(uuid) to authenticated;
