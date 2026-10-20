-- Any community can be claimed; the reviewer decides. A takeover is clean.
--
-- WHY (2026-10-19, prod: claiming "Mizu Sushi & Steakhouse" said "Someone already runs this
-- community"). Starting a community and taking its link self-verifies the starter
-- (claim_community_handle_for, 20270108120000), so almost every community with a link was
-- operator_verified and submit_community_claim_for refused it. The real owner of a
-- restaurant a customer started had no way to ask for it. A Lana reviewer approves every
-- claim anyway, so the submit gate only ever stopped legitimate owners.
--
-- WHAT
--   1. submit_community_claim_for: a verified owner no longer blocks a claim (suspended
--      still does), and "already yours" is checked first, so an owner claiming her own
--      community is told so instead of "someone already runs this".
--   2. approve_place_claim on a place someone already runs = a takeover. Product decision:
--      the previous owner and every co-owner lose all rights; open co-owner invites are
--      revoked. The claimant is the only operator. The place keeps its existing link.
--   3. places_sync_operator grants the new claimed_by before retiring the old one. Retiring
--      first left the place with no live operator for a moment and
--      place_managers_keep_one_operator raised, so any transfer of claimed_by failed.
--   4. "Started it" (places.created_by) stops granting anything once a reviewer has made
--      someone else the verified owner: _community_starter, used by attach/detach_chapter
--      and the claim gate. A starter who self-verified is unaffected (her operator row is
--      what counts) until a takeover retires it.

-- ── 0 · who started it, while that still means something ───────────────────────

create or replace function public._community_starter(p_place_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  -- The starter's standing ends when the place is verified: from then on only a live
  -- operator row counts (hers too, if she self-verified by taking its link).
  select p_user_id is not null and exists (
    select 1 from public.places p
     where p.id = p_place_id
       and p.created_by = p_user_id
       and p.governance_state not in ('operator_verified', 'suspended'));
$$;

revoke all on function public._community_starter(uuid, uuid) from public, anon, authenticated;
grant execute on function public._community_starter(uuid, uuid) to service_role;

-- ── 1 · submit: claimable unless suspended ─────────────────────────────────────

create or replace function public.submit_community_claim_for(
  p_user_id       uuid,
  p_place_id      uuid,
  p_contact_email text,
  p_contact_phone text default null,
  p_note          text default null,
  p_proof_path    text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_gov text;
  c     public.place_claims;
begin
  if p_user_id is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select governance_state into v_gov from public.places where id = p_place_id for update;
  if v_gov is null then
    raise exception 'no_such_place' using errcode = 'P0001';
  end if;
  -- Hers first: an owner claiming her own community hears "already yours", not "taken".
  if public._community_starter(p_place_id, p_user_id)
     or exists (select 1 from public.place_managers m
                 where m.place_id = p_place_id and m.user_id = p_user_id
                   and m.role = 'operator' and m.removed_at is null) then
    raise exception 'already_yours' using errcode = 'P0001';
  end if;
  -- A verified owner no longer blocks a claim: the reviewer decides (2026-10-19). Starting a
  -- community and taking its link self-verifies (20270108120000), so the real owner of a
  -- restaurant someone else started could never ask for it. Only a suspended place is shut.
  if v_gov = 'suspended' then
    raise exception 'already_claimed' using errcode = 'P0001';
  end if;

  -- One at a time for her: a pending claim, or a declined one she has not closed yet.
  -- An approved claim does not hold the slot — the profile hides it once it is approved.
  -- Serialize her submits so two taps cannot both pass this check.
  perform pg_advisory_xact_lock(hashtext('community_claim:' || p_user_id::text));
  if exists (
    select 1 from public.place_claims x
     where x.requested_by = p_user_id and x.origin = 'in_app' and x.closed_at is null
       and x.status in ('draft', 'pending_verification', 'needs_more_info', 'rejected')
  ) then
    raise exception 'claim_open' using errcode = 'P0001';
  end if;

  begin
    insert into public.place_claims (
      place_id, requested_by, status, verification_method, origin,
      contact_email, contact_phone, claim_note, evidence_storage_path, submitted_at
    ) values (
      p_place_id, p_user_id, 'pending_verification', 'manual_review', 'in_app',
      p_contact_email, p_contact_phone, p_note, p_proof_path, now()
    )
    returning * into c;
  exception
    -- place_claims_one_open_per_place: someone (website or app) already has it open.
    when unique_violation then
      raise exception 'claim_open' using errcode = 'P0001';
  end;

  -- Same as save_place_claim: governance shows the claim is pending; nothing members
  -- built on the place is touched. reject_place_claim reverts this.
  if v_gov = 'community_started' then
    update public.places set governance_state = 'claim_pending' where id = p_place_id;
  end if;

  return public._community_claim_row(c);
end;
$$;

-- ── 2 · approve: a takeover is clean, and the link stays ──────────────────────

create or replace function public.approve_place_claim(
  p_claim_id uuid,
  p_handle   text default null,
  p_notes    text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  c        public.place_claims;
  v_held   text;
  v        text;
  v_err    text;
begin
  if not exists (
    select 1 from public.users u where u.id = auth.uid() and u.founder_role = 'internal'
  ) then
    raise exception 'approve_place_claim is internal-only';
  end if;

  select * into c from public.place_claims where id = p_claim_id for update;
  if c.id is null then
    return jsonb_build_object('status', 'no_such_claim');
  end if;
  if c.status not in ('pending_verification', 'needs_more_info') then
    return jsonb_build_object('status', 'not_open', 'claimStatus', c.status);
  end if;
  if c.requested_by = auth.uid() then
    raise exception 'a claimant cannot approve their own claim';
  end if;

  select normalized_handle into v_held
    from public.place_handle_reservations where id = c.reservation_id;

  -- Reviewer's override wins; then the reserved string, but only if it can be published;
  -- otherwise the locality-derived suggestion (§9.6).
  -- A place that already has its link keeps it: an in-app claim reserves nothing, and a
  -- takeover must not rename get.lana.help/<handle> under its members.
  v := public.normalize_place_handle(coalesce(
         p_handle,
         (select p.handle from public.places p where p.id = c.place_id),
         case when public._place_handle_shape_error(v_held) is null then v_held end,
         public.suggest_place_handle(c.place_id, v_held)));

  -- The link it already has was checked when it was taken (a one-word link is legal there,
  -- under the guard's proofs) — only a NEW link is checked here.
  if v is distinct from (select p.handle from public.places p where p.id = c.place_id) then
    v_err := public._place_handle_shape_error(v);
    if v_err is not null then
      return jsonb_build_object('status', 'bad_handle', 'reason', v_err, 'heldHandle', v_held);
    end if;
    if exists (select 1 from public.places where handle = v and id <> c.place_id)
       or exists (select 1 from public.protected_handles
                   where normalized_handle = v and active) then
      return jsonb_build_object('status', 'bad_handle', 'reason', 'taken');
    end if;
  end if;

  -- Claim first: places_verified_needs_claim_trg refuses operator_verified without it.
  update public.place_claims
     set status       = 'verified',
         reviewed_by  = auth.uid(),
         review_notes = coalesce(p_notes, review_notes),
         resolved_at  = now()
   where id = c.id;

  update public.places
     set governance_state = 'operator_verified',
         handle           = v,
         verified_at      = now(),
         claimed_by       = c.requested_by,
         claimed_at       = now(),
         source           = 'owner_claimed'
   where id = c.place_id;

  update public.place_handle_reservations
     set status = 'consumed' where id = c.reservation_id;

  -- The claimant runs it now, and nobody else does (product decision 2026-10-19: a takeover
  -- is clean — the previous owner and the co-owners they appointed lose every right).
  -- Grant first so the keep-one-operator trigger never sees an empty place.
  insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
  values (c.place_id, c.requested_by, 'operator', 'approved_claim', now(), auth.uid())
  on conflict (place_id, user_id) do update
     set role = 'operator', removed_at = null,
         verification_method = 'approved_claim', verified_at = now();

  update public.place_managers m
     set removed_at = now()
   where m.place_id = c.place_id
     and m.user_id <> c.requested_by
     and m.removed_at is null;

  update public.community_coowner_invites i
     set status = 'revoked', responded_at = now()
   where i.place_id = c.place_id and i.status = 'proposed';

  return jsonb_build_object('status', 'verified', 'handle', v, 'placeId', c.place_id);
end;
$$;

-- ── 3 · transfer: grant before retire ─────────────────────────────────────────

create or replace function public.places_sync_operator()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  -- Grant first, retire second: retiring the only operator before the new one exists trips
  -- place_managers_keep_one_operator, so a transfer (an approved takeover) raised.
  if new.claimed_by is not null
     and (tg_op = 'INSERT' or new.claimed_by is distinct from old.claimed_by) then
    -- A claimant whose earlier claimed_by row was retired by a transfer AWAY gets it back
    -- when ownership returns: claimed_by moving to them is the grant. Without this the
    -- on-conflict no-op left the owner with no operator row at all (A→B→A, QA 2026-10-05).
    -- Only a row claimed_by made is revived; a manual grant someone removed stays removed.
    insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
    values (new.id, new.claimed_by, 'operator', 'claimed_by_sync',
            coalesce(new.claimed_at, now()), new.claimed_by)
    on conflict (place_id, user_id) do update
       set removed_at = null, role = 'operator'
     where public.place_managers.removed_at is not null
       and public.place_managers.verification_method in ('claimed_by_sync', 'backfill_claimed_by');
  end if;
  if tg_op = 'UPDATE'
     and old.claimed_by is not null
     and old.claimed_by is distinct from new.claimed_by then
    update public.place_managers m
       set removed_at = now()
     where m.place_id = new.id
       and m.user_id = old.claimed_by
       and m.role = 'operator'
       and m.removed_at is null
       and m.verification_method in ('claimed_by_sync', 'backfill_claimed_by');
  end if;
  return new;
end;
$$;

-- ── 4 · chapters: a replaced starter can no longer attach or detach ────────────

create or replace function public.attach_chapter(
  p_user_id uuid,
  p_chapter uuid,
  p_parent  uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_ch   public.places%rowtype;
  v_par  public.places%rowtype;
  v_inherit boolean := false;
begin
  if p_user_id is null or p_chapter is null or p_parent is null then
    raise exception 'chapter_args_required' using errcode = '22023';
  end if;
  if p_chapter = p_parent then
    raise exception 'chapter_cannot_parent_itself' using errcode = '23514';
  end if;

  select * into v_ch  from public.places where id = p_chapter for update;
  if not found then raise exception 'chapter_not_found' using errcode = 'P0002'; end if;
  select * into v_par from public.places where id = p_parent;
  if not found then raise exception 'parent_not_found' using errcode = 'P0002'; end if;

  -- Standing on the chapter: its creator or its operator.
  if not public._community_starter(p_chapter, p_user_id)
     and not public.is_community_operator(p_chapter, p_user_id) then
    raise exception 'not_your_community' using errcode = '42501';
  end if;
  -- Standing on the parent: a confirmed member, its creator, or its operator.
  if not public._community_starter(p_parent, p_user_id)
     and not public.is_community_operator(p_parent, p_user_id)
     and not exists (
       select 1 from public.circle_affiliations a
        where a.user_id = p_user_id and a.place_ref = p_parent
          and a.status = 'confirmed' and a.dismissed_at is null) then
    raise exception 'not_a_member_of_parent' using errcode = '42501';
  end if;

  if v_ch.parent_place_ref = p_parent then
    return jsonb_build_object('chapterId', v_ch.id, 'parentId', v_par.id,
                              'parentName', v_par.name, 'alreadyAttached', true,
                              'inheritedLocation', false);
  end if;
  if v_ch.parent_place_ref is not null then
    raise exception 'chapter_has_another_parent' using errcode = '23514';
  end if;

  if v_ch.place_type = 'creator' then
    raise exception 'creator_community_cannot_be_chapter' using errcode = '23514';
  end if;
  if v_par.governance_state = 'suspended' or v_ch.governance_state = 'suspended' then
    raise exception 'community_suspended' using errcode = '42501';
  end if;

  if v_ch.lat is null or v_ch.lng is null then
    if v_par.lat is null or v_par.lng is null then
      raise exception 'chapter_needs_location' using errcode = '23514';
    end if;
    v_inherit := true;
  end if;

  update public.places
     set parent_place_ref = p_parent,
         lat     = case when v_inherit then v_par.lat else lat end,
         lng     = case when v_inherit then v_par.lng else lng end,
         zip     = case when v_inherit then coalesce(zip, v_par.zip) else zip end,
         address = case when v_inherit then coalesce(address, v_par.address) else address end,
         updated_at = now()
   where id = p_chapter;

  return jsonb_build_object('chapterId', v_ch.id, 'parentId', v_par.id,
                            'parentName', v_par.name, 'alreadyAttached', false,
                            'inheritedLocation', v_inherit);
end;
$$;

create or replace function public.detach_chapter(
  p_user_id uuid,
  p_chapter uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_ch  public.places%rowtype;
  v_par public.places%rowtype;
begin
  select * into v_ch from public.places where id = p_chapter for update;
  if not found then raise exception 'chapter_not_found' using errcode = 'P0002'; end if;
  if v_ch.parent_place_ref is null then
    return jsonb_build_object('chapterId', v_ch.id, 'wasAttached', false);
  end if;
  select * into v_par from public.places where id = v_ch.parent_place_ref;

  if not public._community_starter(v_ch.id, p_user_id)
     and not public.is_community_operator(v_ch.id, p_user_id)
     and not public._community_starter(v_par.id, p_user_id)
     and not public.is_community_operator(v_par.id, p_user_id) then
    raise exception 'not_your_community' using errcode = '42501';
  end if;

  -- The point it inherited stays: it is still where the club meets.
  update public.places set parent_place_ref = null, updated_at = now() where id = p_chapter;
  return jsonb_build_object('chapterId', v_ch.id, 'wasAttached', true,
                            'parentId', v_par.id, 'parentName', v_par.name);
end;
$$;

-- ============================================================================
-- ROLLBACK
--   Re-run the definitions from 20270205120000 (submit_community_claim_for),
--   20270108120000 (approve_place_claim), 20270107120000 (places_sync_operator) and
--   20270125120000 (attach_chapter, detach_chapter); drop function public._community_starter.
--   Takeovers already approved are not undone by a rollback: their place_managers rows
--   carry removed_at and can be revived by hand.
-- ============================================================================
