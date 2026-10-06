-- In-app community handle claim · Proof B for every type · claim resolved before verify
--
-- WHY (Tommaso, BUG_HANDLE_GUARD_ASJID.md, 2026-10-05; design in
-- docs/superpowers/specs/2026-10-06-in-app-community-handle-claim-design.md)
--
--   San Jose State University could not get "sjsu", for two independent reasons:
--     · Proof B in places_single_token_handle_guard (20270106120000) was gated on
--       place_type = 'creator'. A school that went through the identical reserve-and-email
--       flow was refused on its type, not on evidence.
--     · SJSU was made in the app. The in-app create path (community_capture → add_circle)
--       writes no handle, no reservation and no claim, and the PWA has no handle screen at
--       all, so a community made there can never produce the proof the guard asks for.
--
--   Separately, complete_place_claim_by_email and approve_place_claim set
--   governance_state = 'operator_verified' BEFORE resolving the claim to 'verified', so
--   places_verified_needs_claim_trg (20261228120005) refuses the update for every claim on a
--   place with no other verified claim — dashed handles included.
--
-- WHAT
--   1. _place_handle_proven: the one definition of "this place may hold this bare handle".
--      Proof A unchanged; Proof B without the place_type condition, reservation_email only.
--      (Not domain_email: that would let a location claimed by email take a bare brand word
--      like "safeway" — location claims keep locality handles.)
--   2. The guard calls it. Member-handle and protected checks stay unconditional.
--   3. claim_community_handle(_for): a signed-in, email-confirmed user who either operates
--      the community, or created a name-only one (creator:… place, community_started),
--      reserves and holds a handle in one step. The name-only creator becomes its verified
--      operator, exactly as lana.help self-verifies creators. Members of a community on a
--      real place cannot self-verify — that is still lana.help's location claim.
--   4. community_handle_offer(_for): the same eligibility plus a suggested handle, so the
--      worker and the PWA only offer the claim to someone who can make it.
--   5. complete_place_claim_by_email / approve_place_claim: the claim is resolved first.
--      Handle choice is unchanged — a bare reservation already falls back to
--      suggest_place_handle because _place_handle_shape_error requires a locality by default.
--
-- A first handle never stamps handle_renamed_at: the community's one rename stays unspent.

-- ── 1 · the proof, in one place ─────────────────────────────────────────────────

create or replace function public._place_handle_proven(p_place_id uuid, p_handle text)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
           select 1
             from public.external_community_identities e
            where e.place_id = p_place_id
              and e.ownership_verified_at is not null
              and lower(e.username) = p_handle)
      or exists (
           select 1
             from public.place_claims c
             join public.place_handle_reservations r on r.id = c.reservation_id
            where c.place_id = p_place_id
              and c.status = 'verified'
              and c.verification_method = 'reservation_email'
              and r.normalized_handle = p_handle);
$$;

revoke all on function public._place_handle_proven(uuid, text) from public, anon, authenticated;

comment on function public._place_handle_proven(uuid, text) is
  'Proof of a bare (hyphen-free) handle: a verified external identity with that username, or '
  'a verified reservation_email claim whose reservation is exactly that handle. Any place '
  'type. Used by places_single_token_handle_guard and claim_community_handle_for.';

-- ── 2 · the guard ───────────────────────────────────────────────────────────────

create or replace function public.places_single_token_handle_guard()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if new.handle is null then
    return new;
  end if;

  -- Compound handles are unchanged: always allowed.
  if position('-' in new.handle) > 0 then
    return new;
  end if;

  -- A bare token is the same shape as a member handle; one string, one owner.
  if exists (select 1 from public.users u where u.handle = new.handle) then
    raise exception 'handle_taken_by_user'
      using errcode = 'P0001',
            hint = 'That handle already belongs to a member. Use a compound handle (run-with-maya).';
  end if;

  -- Ownership is never a way around a protected word.
  if exists (select 1 from public.protected_handles h
              where h.normalized_handle = new.handle and h.active) then
    raise exception 'handle_protected'
      using errcode = 'P0001',
            hint = 'That handle is reserved.';
  end if;

  if public._place_handle_proven(new.id, new.handle) then
    return new;
  end if;

  raise exception 'single_token_handle_requires_matching_identity'
    using errcode = 'P0001',
          hint = 'Use a compound handle (run-with-maya), verify an external account with this '
                 'exact username, or reserve it and confirm by email.';
end;
$$;

comment on function public.places_single_token_handle_guard() is
  'Option C, widened 20270106 and 20270108. A hyphen-free handle needs proof of the name '
  '(_place_handle_proven), for any place type. Member handles and protected_handles are '
  'refused regardless of proof.';

-- ── 3 · availability and eligibility ────────────────────────────────────────────

-- Why a handle cannot be had, or null if it is free. _place_handle_taken plus the two
-- places it does not look: member handles (a bare token collides with them) and retired
-- aliases (one audience's old link is never handed to somebody else).
create or replace function public._place_handle_unavailable(p_handle text)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select coalesce(
    public._place_handle_taken(p_handle),
    case
      when position('-' in p_handle) = 0
       and exists (select 1 from public.users u where u.handle = p_handle) then 'member'
      when exists (select 1 from public.place_handle_aliases a where a.handle = p_handle)
        then 'retired'
    end);
$$;

revoke all on function public._place_handle_unavailable(text) from public, anon, authenticated;

-- Null when p_user_id may claim a handle for p_place_id now; otherwise the reason.
create or replace function public._community_handle_claim_status(p_user_id uuid, p_place_id uuid)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    -- The email IS the proof: a guest, or an account whose address was never confirmed,
    -- has nothing to show.
    when p_user_id is null
      or not exists (
           select 1 from auth.users u
            where u.id = p_user_id
              and coalesce(u.is_anonymous, false) = false
              and u.email is not null
              and u.email_confirmed_at is not null)
      then 'sign_in_required'
    when p.id is null then 'place_not_found'
    when p.handle is not null then 'already_has_handle'
    when public.is_community_operator(p.id, p_user_id) then null
    -- A name-only community belongs to whoever made it, as on lana.help. A community on a
    -- real place does not: membership is not ownership of the gym.
    when p.governance_state = 'community_started'
     and p.google_place_id like 'creator:%'
     and p.created_by = p_user_id then null
    else 'not_eligible'
  end
  from (select 1) one
  left join public.places p on p.id = p_place_id;
$$;

revoke all on function public._community_handle_claim_status(uuid, uuid)
  from public, anon, authenticated;

-- Up to three free handles near what was wanted: the wanted string's compound forms, the
-- place's locality-derived handle, then numbered variants.
create or replace function public._community_handle_suggestions(p_place_id uuid, p_wanted text)
returns text[]
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_name  text;
  v_base  text;
  v_out   text[] := '{}';
  v_try   text;
  i       int;
begin
  select public.normalize_place_handle(p.name) into v_name
    from public.places p where p.id = p_place_id;

  foreach v_try in array array[
    p_wanted,
    v_name,
    public.suggest_place_handle(p_place_id, null)
  ] loop
    exit when array_length(v_out, 1) >= 3;
    if v_try is not null
       and length(v_try) <= 48
       and public._place_handle_shape_error(v_try, false) is null
       and public._place_handle_unavailable(v_try) is null
       and not v_try = any(v_out)
       -- A bare suggestion has to be grantable, i.e. by the claim itself, so offer it only
       -- when nothing else would refuse it.
       and (position('-' in v_try) > 0
            or not exists (select 1 from public.protected_handles h
                            where h.normalized_handle = v_try and h.active)) then
      v_out := v_out || v_try;
    end if;
  end loop;

  v_base := coalesce(p_wanted, v_name);
  if v_base is not null then
    for i in 2..9 loop
      exit when array_length(v_out, 1) >= 3;
      v_try := v_base || '-' || i::text;
      if length(v_try) <= 48 and public._place_handle_unavailable(v_try) is null
         and not v_try = any(v_out) then
        v_out := v_out || v_try;
      end if;
    end loop;
  end if;

  return v_out;
end;
$$;

revoke all on function public._community_handle_suggestions(uuid, text)
  from public, anon, authenticated;

-- ── 4 · the offer ───────────────────────────────────────────────────────────────

create or replace function public.community_handle_offer_for(p_user_id uuid, p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_why  text := public._community_handle_claim_status(p_user_id, p_place_id);
  v_sugg text[];
begin
  if v_why is not null then
    return jsonb_build_object('eligible', false, 'reason', v_why);
  end if;
  v_sugg := public._community_handle_suggestions(p_place_id, null);
  return jsonb_build_object(
    'eligible', true,
    'placeId', p_place_id,
    'suggestion', v_sugg[1],
    'suggestions', to_jsonb(v_sugg));
end;
$$;

revoke all on function public.community_handle_offer_for(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.community_handle_offer_for(uuid, uuid) to service_role;

create or replace function public.community_handle_offer(p_place_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select public.community_handle_offer_for(auth.uid(), p_place_id);
$$;

revoke all on function public.community_handle_offer(uuid) from public, anon;
grant execute on function public.community_handle_offer(uuid) to authenticated;

-- ── 5 · the claim ───────────────────────────────────────────────────────────────

create or replace function public.claim_community_handle_for(
  p_user_id  uuid,
  p_place_id uuid,
  p_handle   text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v       text := public.normalize_place_handle(p_handle);
  v_why   text;
  v_err   text;
  v_type  text;
  v_op    boolean;
  v_res   uuid;
begin
  -- Lock first, so two taps (or two operators) cannot both pass the checks below.
  perform 1 from public.places where id = p_place_id for update;

  v_why := public._community_handle_claim_status(p_user_id, p_place_id);
  if v_why is not null then
    return jsonb_build_object('status', v_why);
  end if;

  v_err := public._place_handle_shape_error(v, false);
  if v_err is not null then
    return jsonb_build_object('status', 'invalid', 'reason', v_err, 'normalizedHandle', v);
  end if;

  v_why := public._place_handle_unavailable(v);
  if v_why is not null then
    return jsonb_build_object(
      'status', 'unavailable', 'reason', v_why, 'normalizedHandle', v,
      'suggestions', to_jsonb(public._community_handle_suggestions(p_place_id, v)));
  end if;

  select p.place_type into v_type from public.places p where p.id = p_place_id;
  v_op := public.is_community_operator(p_place_id, p_user_id);

  begin
    -- The same two rows lana.help writes for a creator: the reservation (already spent —
    -- it is never a live hold) and the verified claim that ties it to this account.
    insert into public.place_handle_reservations (
      normalized_handle, token_hash, user_id, place_id, status, source,
      requested_place_type, expires_at
    ) values (
      v, encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex'),
      p_user_id, p_place_id, 'consumed', 'in_app', v_type, now()
    )
    returning id into v_res;

    insert into public.place_claims (
      place_id, reservation_id, requested_by, role_title, status, verification_method,
      review_notes, submitted_at, resolved_at
    ) values (
      p_place_id, v_res, p_user_id,
      case when v_op then 'Operator' else 'Creator' end,
      'verified', 'reservation_email',
      case when v_op
        then 'In-app handle claim by the community''s operator, signed in with a confirmed email.'
        else 'In-app handle claim by the creator of a name-only community, signed in with a '
             || 'confirmed email. Self-verified, as lana.help does for creators.'
      end,
      now(), now()
    );

    -- Claim first, then the place: places_verified_needs_claim_trg needs the verified claim,
    -- and the guard's Proof B reads it. claimed_by only fills an empty slot, so an existing
    -- operator is untouched; places_sync_operator_trg makes a new one an operator.
    update public.places p
       set handle           = v,
           governance_state = 'operator_verified',
           verified_at      = coalesce(p.verified_at, now()),
           claimed_by       = coalesce(p.claimed_by, p_user_id),
           claimed_at       = coalesce(p.claimed_at, now()),
           updated_at       = now()
     where p.id = p_place_id;
  exception
    when unique_violation then
      return jsonb_build_object(
        'status', 'unavailable', 'reason', 'taken', 'normalizedHandle', v,
        'suggestions', to_jsonb(public._community_handle_suggestions(p_place_id, v)));
    when sqlstate 'P0001' then
      -- The guard is the last word; translate its refusal rather than surface a 500.
      if sqlerrm not in ('handle_taken_by_user', 'handle_protected',
                         'single_token_handle_requires_matching_identity') then
        raise;
      end if;
      return jsonb_build_object(
        'status', 'unavailable', 'reason', sqlerrm, 'normalizedHandle', v,
        'suggestions', to_jsonb(public._community_handle_suggestions(p_place_id, v)));
  end;

  return jsonb_build_object('status', 'claimed', 'handle', v, 'placeId', p_place_id);
end;
$$;

revoke all on function public.claim_community_handle_for(uuid, uuid, text)
  from public, anon, authenticated;
grant execute on function public.claim_community_handle_for(uuid, uuid, text) to service_role;

create or replace function public.claim_community_handle(p_place_id uuid, p_handle text)
returns jsonb
language sql
volatile
security definer
set search_path = pg_catalog, public
as $$
  select public.claim_community_handle_for(auth.uid(), p_place_id, p_handle);
$$;

revoke all on function public.claim_community_handle(uuid, text) from public, anon;
grant execute on function public.claim_community_handle(uuid, text) to authenticated;

comment on function public.claim_community_handle(uuid, text) is
  'Give a community its first handle. Caller must be signed in with a confirmed email and '
  'either operate the community or have created a name-only one (they become its verified '
  'operator). Writes a consumed in_app reservation + verified reservation_email claim, which '
  'is the guard''s Proof B. Does not spend the one rename.';

-- ── 6 · complete_place_claim_by_email: resolve the claim before verifying the place ─

create or replace function public.complete_place_claim_by_email(p_token_hash text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  r      public.place_handle_reservations;
  c      public.place_claims;
  v_gov  text;
  v      text;
  v_err  text;
begin
  select * into r
    from public.place_handle_reservations
   where token_hash = p_token_hash
     and status in ('active', 'bound')
     and expires_at > now()
   for update;

  if r.id is null then
    return jsonb_build_object('status', 'expired');
  end if;
  if r.user_id is null then
    return jsonb_build_object('status', 'not_signed_in');
  end if;
  if r.place_id is null then
    return jsonb_build_object('status', 'no_place');
  end if;

  select governance_state into v_gov
    from public.places where id = r.place_id for update;
  if v_gov = 'operator_verified' then
    return jsonb_build_object('status', 'already_claimed');
  end if;

  select * into c
    from public.place_claims
   where place_id = r.place_id
     and requested_by = r.user_id
     and status in ('draft', 'pending_verification', 'needs_more_info')
   order by created_at desc
   limit 1
   for update;

  if c.id is null then
    return jsonb_build_object('status', 'no_claim');
  end if;

  -- The reserved string is published only if it can be; otherwise the locality-derived
  -- handle, so "safeway" becomes safeway-foster-city rather than failing here.
  v := public.normalize_place_handle(coalesce(
         case when public._place_handle_shape_error(r.normalized_handle) is null
              then r.normalized_handle end,
         public.suggest_place_handle(r.place_id, r.normalized_handle)));

  v_err := public._place_handle_shape_error(v);
  if v_err is not null then
    return jsonb_build_object('status', 'bad_handle', 'reason', v_err);
  end if;
  if exists (select 1 from public.places where handle = v and id <> r.place_id)
     or exists (select 1 from public.protected_handles
                 where normalized_handle = v and active) then
    return jsonb_build_object('status', 'bad_handle', 'reason', 'taken');
  end if;

  -- Claim first: places_verified_needs_claim_trg refuses operator_verified without it.
  update public.place_claims
     set status              = 'verified',
         verification_method = 'domain_email',
         resolved_at         = now(),
         review_notes        = coalesce(review_notes, 'self-serve: one-time code confirmed')
   where id = c.id;

  update public.places
     set governance_state = 'operator_verified',
         handle           = v,
         verified_at      = now(),
         claimed_by       = r.user_id,
         claimed_at       = now(),
         source           = 'owner_claimed'
   where id = r.place_id;

  update public.place_handle_reservations
     set status = 'consumed' where id = r.id;

  return jsonb_build_object(
    'status', 'verified', 'handle', v, 'placeId', r.place_id, 'claimId', c.id);
end;
$$;

revoke execute on function public.complete_place_claim_by_email(text)
  from public, anon, authenticated;

-- ── 7 · approve_place_claim: same reorder ───────────────────────────────────────

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
  v := public.normalize_place_handle(coalesce(
         p_handle,
         case when public._place_handle_shape_error(v_held) is null then v_held end,
         public.suggest_place_handle(c.place_id, v_held)));

  v_err := public._place_handle_shape_error(v);
  if v_err is not null then
    return jsonb_build_object('status', 'bad_handle', 'reason', v_err, 'heldHandle', v_held);
  end if;
  if exists (select 1 from public.places where handle = v and id <> c.place_id)
     or exists (select 1 from public.protected_handles
                 where normalized_handle = v and active) then
    return jsonb_build_object('status', 'bad_handle', 'reason', 'taken');
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

  return jsonb_build_object('status', 'verified', 'handle', v, 'placeId', c.place_id);
end;
$$;

revoke execute on function public.approve_place_claim(uuid, text, text) from public, anon;
grant execute on function public.approve_place_claim(uuid, text, text) to authenticated;

-- ============================================================================
-- ROLLBACK
--   Restore places_single_token_handle_guard from 20270106120000, complete_place_claim_by_email
--   from 20261110120000 and approve_place_claim from 20261108120000; then
--     drop function public.claim_community_handle(uuid, text);
--     drop function public.claim_community_handle_for(uuid, uuid, text);
--     drop function public.community_handle_offer(uuid);
--     drop function public.community_handle_offer_for(uuid, uuid);
--     drop function public._community_handle_suggestions(uuid, text);
--     drop function public._community_handle_claim_status(uuid, uuid);
--     drop function public._place_handle_unavailable(text);
--     drop function public._place_handle_proven(uuid, text);
--   Handles already granted stay; they were granted with proof.
-- ============================================================================
