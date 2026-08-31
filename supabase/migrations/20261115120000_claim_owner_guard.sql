-- Lana · a second claimant does not inherit the first one's claim ────────────────────
-- save_place_claim upserts on the partial unique index place_claims_one_open_per_place,
-- which is keyed on place_id alone, and its DO UPDATE never touched requested_by. So when
-- a place already had an open claim, a different person walking the flow silently updated
-- that stranger's row — including reservation_id = excluded.reservation_id, pointing the
-- other claimant's claim at this claimant's reservation — and got the stranger's claimId
-- back as if it were theirs.
--
-- complete_place_claim_by_email then looked for a claim with requested_by = the caller,
-- found none, and returned no_claim. The API read that as "not verified yet" and showed
-- the review screen, so the second claimant could confirm an email code and still never
-- publish, with no statement of why.
--
-- One open claim per place stays the rule (that is what the index encodes). This only
-- makes the collision explicit instead of writing through it.
create or replace function public.save_place_claim(
  p_token_hash    text,
  p_place_id      uuid,
  p_role_title    text default null,
  p_method        text default null,
  p_email_domain  text default null,
  p_evidence_path text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  r        public.place_handle_reservations;
  v_gov    text;
  v_claim  uuid;
  v_status text;
  v_other  uuid;
begin
  if auth.uid() is null then
    raise exception 'save_place_claim requires an authenticated caller';
  end if;

  select * into r
    from public.place_handle_reservations
   where token_hash = p_token_hash
     and user_id = auth.uid()
     and status in ('active', 'bound')
     and expires_at > now()
   for update;

  if r.id is null then
    return jsonb_build_object('status', 'expired');
  end if;

  select governance_state into v_gov
    from public.places where id = p_place_id for update;
  if v_gov is null then
    return jsonb_build_object('status', 'no_such_place');
  end if;
  -- Already governed by someone. This is where a second claimant is told no; it is NOT
  -- where a member-started place gets turned away (that is the whole point of the flow).
  if v_gov = 'operator_verified' then
    return jsonb_build_object('status', 'already_claimed');
  end if;

  -- Someone else got here first and is still waiting on a reviewer. Their row is not ours
  -- to edit, and the caller needs to hear that rather than a review promise we cannot keep.
  select id into v_other
    from public.place_claims
   where place_id = p_place_id
     and status in ('draft', 'pending_verification', 'needs_more_info')
     and requested_by <> auth.uid()
   limit 1;
  if v_other is not null then
    return jsonb_build_object('status', 'claim_pending_other', 'claimId', v_other);
  end if;

  v_status := case when p_method is null then 'draft' else 'pending_verification' end;

  insert into public.place_claims (
    place_id, reservation_id, requested_by, role_title,
    status, verification_method, verification_email_domain,
    evidence_storage_path, submitted_at
  ) values (
    p_place_id, r.id, auth.uid(), p_role_title,
    v_status, p_method, p_email_domain, p_evidence_path,
    case when p_method is null then null else now() end
  )
  on conflict (place_id) where status in ('draft', 'pending_verification', 'needs_more_info')
  do update set
    reservation_id            = excluded.reservation_id,
    role_title                = coalesce(excluded.role_title, place_claims.role_title),
    verification_method       = coalesce(excluded.verification_method,
                                         place_claims.verification_method),
    verification_email_domain = coalesce(excluded.verification_email_domain,
                                         place_claims.verification_email_domain),
    evidence_storage_path     = coalesce(excluded.evidence_storage_path,
                                         place_claims.evidence_storage_path),
    -- Submitting evidence advances a draft; it never drags a reviewed claim backwards.
    status                    = case
                                  when place_claims.status = 'draft'
                                   and excluded.status = 'pending_verification'
                                  then 'pending_verification'
                                  else place_claims.status
                                end,
    submitted_at              = coalesce(place_claims.submitted_at, excluded.submitted_at)
  returning id, status into v_claim, v_status;

  update public.place_handle_reservations set place_id = p_place_id where id = r.id;

  -- Governance moves to pending only once evidence is in. A pending claim must not
  -- freeze member Find/Create — nothing here touches memberships, events, or asks.
  if v_status = 'pending_verification' and v_gov = 'community_started' then
    update public.places set governance_state = 'claim_pending' where id = p_place_id;
  end if;

  return jsonb_build_object(
    'status', v_status, 'claimId', v_claim,
    'suggestedHandle', public.suggest_place_handle(p_place_id, r.normalized_handle));
end;
$$;

revoke execute on function public.save_place_claim(text, uuid, text, text, text, text)
  from public, anon;
grant execute on function public.save_place_claim(text, uuid, text, text, text, text)
  to authenticated;
