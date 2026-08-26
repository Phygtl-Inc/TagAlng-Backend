-- Self-check for 20261106120000_place_handles_claims.sql.
-- Rolls back at the end, so it is safe to re-run:
--   psql -v ON_ERROR_STOP=1 -d <database> -f supabase/tests/place_handles_claims_check.sql
-- Silence means every assertion held.
--
-- Impersonation goes through request.jwt.claim.sub, which is what Supabase's own
-- auth.uid() reads — so this runs against a real migrated database, not only a stub.

begin;

insert into public.users (id, founder_role) values
  ('00000000-0000-0000-0000-00000000a001', 'internal'),
  ('00000000-0000-0000-0000-00000000b001', null),
  ('00000000-0000-0000-0000-00000000b002', null);

insert into public.places (id, google_place_id, name, address, zip) values
  ('00000000-0000-0000-0000-00000000c001', 'chk-gp-1', 'Safeway',
   '921 E Hillsdale Blvd, Foster City, CA 94404, USA', '94404'),
  ('00000000-0000-0000-0000-00000000c002', 'chk-gp-2', 'Planet Fitness',
   '13801 Narcoossee Rd, Orlando, FL 32832, USA', '32832');

-- ── normalization, shape rules, derivation ────────────────────────────────────
do $$
declare v text;
begin
  -- NFKD is what makes the diacritic in "Café" cost nothing.
  v := public.normalize_place_handle('  St. Mark''s Café  Orlando! ');
  assert v = 'st-marks-cafe-orlando', 'normalize gave ' || coalesce(v, '<null>');

  -- The hyphen rule: this is what keeps place handles disjoint from users.handle.
  assert public._place_handle_shape_error('stmarks') = 'needs_locality',
    'a one-word handle must be refused';
  assert public._place_handle_shape_error('ab') = 'too_short', 'length floor';
  assert public._place_handle_shape_error('safeway-foster-city') is null,
    'a good handle was refused';
  assert public._place_handle_taken('admin') = 'protected', 'system route not protected';

  v := public.suggest_place_handle('00000000-0000-0000-0000-00000000c001');
  assert v = 'safeway-foster-city', 'suggest gave ' || coalesce(v, '<null>');
end $$;

-- ── availability and the reservation hold ─────────────────────────────────────
do $$
declare j jsonb;
begin
  j := public.check_place_handle('Safeway Foster City');
  assert j->>'status' = 'available', j::text;
  assert j->>'normalizedHandle' = 'safeway-foster-city', j::text;

  j := public.reserve_place_handle(
         'Safeway Foster City', 'hash-aaa', 'sess-1', 'supermarket', 'test');
  assert j->>'status' = 'reserved', j::text;

  -- Second visitor, same string, moments later.
  j := public.reserve_place_handle('safeway-foster-city', 'hash-bbb');
  assert j->>'status' = 'held', j::text;

  j := public.check_place_handle('safeway-foster-city');
  assert j->>'status' = 'unavailable', j::text;
  assert jsonb_array_length(j->'suggestions') = 3, j::text;
end $$;

-- ── no public handle before verification, enforced by the database ───────────
do $$
begin
  begin
    update public.places set handle = 'sneaky-handle'
     where id = '00000000-0000-0000-0000-00000000c001';
    raise exception 'CHECK_FAILED: a community_started place accepted a handle';
  exception
    when check_violation then null;                          -- expected
    when raise_exception then
      if sqlerrm like 'CHECK_FAILED%' then raise; end if;
  end;
end $$;

-- ── claim: draft does not move governance, evidence does ─────────────────────
do $$
declare j jsonb; v_gov text;
begin
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b001', true);

  j := public.bind_place_reservation('hash-aaa');
  assert j->>'status' = 'bound', j::text;

  j := public.save_place_claim(
         'hash-aaa', '00000000-0000-0000-0000-00000000c001', 'General Manager');
  assert j->>'status' = 'draft', j::text;
  select governance_state into v_gov
    from public.places where id = '00000000-0000-0000-0000-00000000c001';
  assert v_gov = 'community_started', 'a draft claim moved governance to ' || v_gov;

  -- Same call again with a method: updates the open claim, does not open a second.
  j := public.save_place_claim(
         'hash-aaa', '00000000-0000-0000-0000-00000000c001',
         null, 'domain_email', 'safeway.com');
  assert j->>'status' = 'pending_verification', j::text;
  assert j->>'suggestedHandle' = 'safeway-foster-city', j::text;

  select governance_state into v_gov
    from public.places where id = '00000000-0000-0000-0000-00000000c001';
  assert v_gov = 'claim_pending', 'governance is ' || v_gov;

  assert (select count(*) from public.place_claims
           where place_id = '00000000-0000-0000-0000-00000000c001') = 1,
    'a second claim row was opened on one place';
end $$;

-- ── only an internal reviewer approves ───────────────────────────────────────
do $$
declare v_claim uuid;
begin
  select id into v_claim from public.place_claims
   where place_id = '00000000-0000-0000-0000-00000000c001';

  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b002', true);
  begin
    perform public.approve_place_claim(v_claim);
    raise exception 'CHECK_FAILED: a non-internal user approved a claim';
  exception when raise_exception then
    if sqlerrm like 'CHECK_FAILED%' then raise; end if;
  end;
end $$;

-- ── approve: the whole transition, on the row the member already made ────────
do $$
declare j jsonb; p record;
begin
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000a001', true);

  j := public.approve_place_claim(
         (select id from public.place_claims
           where place_id = '00000000-0000-0000-0000-00000000c001'));
  assert j->>'status' = 'verified', j::text;
  assert j->>'handle' = 'safeway-foster-city', j::text;

  select handle, governance_state, claimed_by, source, (verified_at is not null) as stamped
    into p from public.places where id = '00000000-0000-0000-0000-00000000c001';
  assert p.handle = 'safeway-foster-city', 'handle is ' || coalesce(p.handle, '<null>');
  assert p.governance_state = 'operator_verified', p.governance_state;
  assert p.claimed_by = '00000000-0000-0000-0000-00000000b001',
    'claimed_by is the reviewer, not the claimant';
  assert p.source = 'owner_claimed', p.source;
  assert p.stamped, 'verified_at was not stamped';

  assert (select status from public.place_handle_reservations
           where token_hash = 'hash-aaa') = 'consumed', 'reservation not consumed';

  -- The public resolver sees verified places and nothing else.
  assert public.resolve_place_handle('safeway-foster-city') is not null,
    'resolver is blind to a verified place';
  assert public.resolve_place_handle('planet-fitness-orlando') is null,
    'resolver exposed a place that was never claimed';
end $$;

-- ── a reviewer cannot approve their own claim, and reject gives the place back ─
do $$
declare j jsonb; v_claim uuid; v_gov text;
begin
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000a001', true);

  perform public.reserve_place_handle('planet-fitness-orlando', 'hash-ccc');
  perform public.bind_place_reservation('hash-ccc');
  j := public.save_place_claim(
         'hash-ccc', '00000000-0000-0000-0000-00000000c002',
         'Owner', 'manual_review', null, 'evidence/chk.pdf');
  assert j->>'status' = 'pending_verification', j::text;

  select id into v_claim from public.place_claims
   where place_id = '00000000-0000-0000-0000-00000000c002';

  begin
    perform public.approve_place_claim(v_claim);
    raise exception 'CHECK_FAILED: a claimant approved their own claim';
  exception when raise_exception then
    if sqlerrm like 'CHECK_FAILED%' then raise; end if;
  end;

  j := public.reject_place_claim(v_claim, 'no evidence of authority');
  assert j->>'status' = 'rejected', j::text;

  select governance_state into v_gov
    from public.places where id = '00000000-0000-0000-0000-00000000c002';
  assert v_gov = 'community_started', 'rejected claim left governance at ' || v_gov;
  assert (select handle from public.places
           where id = '00000000-0000-0000-0000-00000000c002') is null,
    'a rejected claim left a public handle behind';
  assert (select status from public.place_handle_reservations
           where token_hash = 'hash-ccc') = 'released', 'reservation not released';
end $$;

-- ── resolve or create · both doors land on one row ───────────────────────────
do $$
declare j jsonb; v_first uuid;
begin
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b001', true);

  -- A gym we have never seen: created, and marked as merely submitted.
  j := public.resolve_or_create_place(
         'chk-gp-new', 'Lake Nona Performance Club',
         '9169 Narcoossee Rd, Orlando, FL 32827, USA', 28.37, -81.25, '32827', 'gym');
  assert (j->>'created')::boolean, j::text;
  assert j->>'matchState' = 'created', j::text;
  assert j->>'nextAction' = 'create_claim', j::text;
  assert j->>'suggestedHandle' = 'lake-nona-performance-club-orlando', j::text;
  v_first := (j->>'placeId')::uuid;

  assert (select source from public.places where id = v_first) = 'operator_submitted',
    'a submitted place must not claim owner provenance';
  -- The website says "gym"; our own vocabulary says "fitness".
  assert (select place_type from public.places where id = v_first) = 'fitness',
    'sector was not mapped onto place_type';

  -- Same google place id again — one row, and now it is a claim, not a creation.
  j := public.resolve_or_create_place('chk-gp-new', 'Lake Nona Performance Club');
  assert not (j->>'created')::boolean, j::text;
  assert (j->>'placeId')::uuid = v_first, 'a second row was created for one place';
  assert j->>'matchState' = 'existing_community_started', j::text;
  assert j->>'nextAction' = 'claim_existing', j::text;
  assert (select count(*) from public.places
           where google_place_id = 'chk-gp-new') = 1, 'duplicate place row';

  -- The member's door: a place grounded from chat, then met by an operator. This is the
  -- convergence the whole feature exists for.
  j := public.resolve_or_create_place('chk-gp-2', 'Planet Fitness');
  assert (j->>'placeId')::uuid = '00000000-0000-0000-0000-00000000c002',
    'operator claim did not land on the member-grounded row';
  assert not (j->>'created')::boolean, j::text;

  -- Already governed: the flow must say so rather than open a second claim.
  j := public.resolve_or_create_place('chk-gp-1', 'Safeway');
  assert j->>'matchState' = 'existing_operator_verified', j::text;
  assert j->>'nextAction' = 'already_claimed', j::text;
end $$;

-- ── a claim still needs an owner, even though picking a place does not ───────
do $$
begin
  perform set_config('request.jwt.claim.sub', '', true);
  -- Choosing the building is open now: the email is asked for at the verify step, so the
  -- visitor is still anonymous here.
  perform public.resolve_or_create_place('chk-gp-openpick', 'Open Pick Gym');
  -- But a claim records who is claiming, so it cannot be made by nobody.
  begin
    perform public.save_place_claim('hash-none',
              '00000000-0000-0000-0000-00000000c002', 'Owner', 'domain_email');
    raise exception 'CHECK_FAILED: an anonymous caller opened a claim';
  exception when raise_exception then
    if sqlerrm like 'CHECK_FAILED%' then raise; end if;
  end;
end $$;

-- ── a brand name may be held, but never published bare ───────────────────────
do $$
declare j jsonb; v_claim uuid;
begin
  -- §7.7: the hero must be able to continue from a bare name, or its own copy
  -- ("we'll help complete this handle next") is a promise with no next step.
  j := public.check_place_handle('Safeway');
  assert j->>'status' = 'available', j::text;
  assert (j->>'needsLocality')::boolean, 'a bare name must be flagged as owing a locality';

  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b002', true);
  j := public.reserve_place_handle('Safeway', 'hash-bare');
  assert j->>'status' = 'reserved', j::text;
  assert (j->>'needsLocality')::boolean, j::text;

  -- A hyphenated name owes nothing.
  j := public.check_place_handle('safeway-foster-city-2');
  assert not (j->>'needsLocality')::boolean, j::text;

  -- And the bare name still cannot reach the public: publishing is the strict gate.
  assert public._place_handle_shape_error('safeway') = 'needs_locality', 'publish gate';
  assert public._place_handle_shape_error('safeway', false) is null, 'hold gate';
  begin
    update public.places set handle = 'safeway', governance_state = 'operator_verified'
     where id = '00000000-0000-0000-0000-00000000c002';
    raise exception 'CHECK_FAILED: a bare handle was published';
  exception
    when check_violation then null;
    when raise_exception then
      if sqlerrm like 'CHECK_FAILED%' then raise; end if;
  end;
end $$;

-- ── a bare reservation still publishes a proper handle ───────────────────────
do $$
declare j jsonb; v_claim uuid; v_place uuid;
begin
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b001', true);
  -- Hold the brand name only, exactly as the hero now allows.
  j := public.reserve_place_handle('Sukisushi', 'hash-brand');
  assert j->>'status' = 'reserved', j::text;
  perform public.bind_place_reservation('hash-brand');

  j := public.resolve_or_create_place(
         'chk-gp-suki', 'Suki Sushi', '120 S Orange Ave, Orlando, FL 32801, USA',
         28.54, -81.37, '32801', 'supermarket');
  v_place := (j->>'placeId')::uuid;
  perform public.save_place_claim('hash-brand', v_place, 'Owner', 'manual_review');
  select id into v_claim from public.place_claims where place_id = v_place;

  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000a001', true);
  j := public.approve_place_claim(v_claim);
  -- The bare hold must not become the public handle; the derived one must.
  assert j->>'status' = 'verified', j::text;
  assert j->>'handle' = 'suki-sushi-orlando',
    'bare reservation published as ' || coalesce(j->>'handle', '<null>');
end $$;

-- ── the locality is not repeated when the name already carries it ────────────
do $$
declare v text;
begin
  insert into public.places (id, google_place_id, name, address, zip) values
    ('00000000-0000-0000-0000-00000000c003', 'chk-gp-lib', 'Orlando Public Library',
     '101 E Central Blvd, Orlando, FL 32801, USA', '32801'),
    ('00000000-0000-0000-0000-00000000c004', 'chk-gp-ymca', 'Downtown YMCA',
     '433 N Mills Ave, Orlando, FL 32803, USA', '32803');

  v := public.suggest_place_handle('00000000-0000-0000-0000-00000000c003');
  assert v = 'orlando-public-library', 'stuttered: ' || coalesce(v, '<null>');

  -- A name without the locality still gets one appended.
  v := public.suggest_place_handle('00000000-0000-0000-0000-00000000c004');
  assert v = 'downtown-ymca-orlando', 'got ' || coalesce(v, '<null>');
end $$;

-- ── derivation, asserted with literals instead of through a row ──────────────
do $$
begin
  -- The name already says Orlando, as a whole token.
  assert public._place_handle_base('Orlando Public Library', 'Orlando')
       = 'orlando-public-library', public._place_handle_base('Orlando Public Library', 'Orlando');
  -- It does not, so the locality is appended.
  assert public._place_handle_base('Downtown YMCA', 'Orlando')
       = 'downtown-ymca-orlando', public._place_handle_base('Downtown YMCA', 'Orlando');
  -- Whole-token matching: "orlando" inside "north-orlando" is a different segment, and
  -- a substring of one word must not suppress a locality that is still needed.
  assert public._place_handle_base('North Orlando Gym', 'Orlando')
       = 'north-orlando-gym', public._place_handle_base('North Orlando Gym', 'Orlando');
  assert public._place_handle_base('Yorkshire Cafe', 'York')
       = 'yorkshire-cafe-york', public._place_handle_base('Yorkshire Cafe', 'York');
  -- A single word with no locality cannot be published, so it derives nothing.
  assert public._place_handle_base('Soulcycle', null) is null, 'bare name with no locality';
  -- A ZIP is a fine locality; it satisfies the hyphen rule on its own.
  assert public._place_handle_base('Suki Sushi', '32801') = 'suki-sushi-32801',
    public._place_handle_base('Suki Sushi', '32801');
end $$;

-- ── a confirmed email completes the claim, with no reviewer ──────────────────
do $$
declare j jsonb; v_place uuid; p record;
begin
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b001', true);
  j := public.reserve_place_handle('Selfserve Gym Orlando', 'hash-selfserve');
  assert j->>'status' = 'reserved', j::text;
  perform public.bind_place_reservation('hash-selfserve');
  j := public.resolve_or_create_place(
         'chk-gp-selfserve', 'Selfserve Gym',
         '9169 Narcoossee Rd, Orlando, FL 32827, USA', 28.37, -81.25, '32827', 'gym');
  v_place := (j->>'placeId')::uuid;
  perform public.save_place_claim('hash-selfserve', v_place, 'Owner', 'domain_email');

  -- No founder_role involved: this is the self-serve path.
  perform set_config('request.jwt.claim.sub', '', true);
  j := public.complete_place_claim_by_email('hash-selfserve');
  assert j->>'status' = 'verified', j::text;
  assert j->>'handle' = 'selfserve-gym-orlando', j::text;

  select governance_state, handle, source into p
    from public.places where id = v_place;
  assert p.governance_state = 'operator_verified', p.governance_state;
  assert p.handle = 'selfserve-gym-orlando', coalesce(p.handle, '<null>');
  assert p.source = 'owner_claimed', p.source;
  assert (select status from public.place_claims where place_id = v_place) = 'verified',
    'claim not resolved';
  assert (select status from public.place_handle_reservations
           where token_hash = 'hash-selfserve') = 'consumed', 'reservation not consumed';

  -- Running it twice must not republish or re-consume.
  j := public.complete_place_claim_by_email('hash-selfserve');
  assert j->>'status' = 'expired', j::text;

  -- Without a place selected there is nothing to publish.
  j := public.reserve_place_handle('Noplace Gym Orlando', 'hash-noplace');
  perform set_config('request.jwt.claim.sub',
                     '00000000-0000-0000-0000-00000000b002', true);
  perform public.bind_place_reservation('hash-noplace');
  perform set_config('request.jwt.claim.sub', '', true);
  j := public.complete_place_claim_by_email('hash-noplace');
  assert j->>'status' = 'no_place', j::text;
end $$;

-- ── the building can be chosen before anyone signs in ────────────────────────
do $$
declare j jsonb;
begin
  perform set_config('request.jwt.claim.sub', '', true);
  j := public.resolve_or_create_place(
         'chk-gp-anonpick', 'Anon Picked Gym',
         '9169 Narcoossee Rd, Orlando, FL 32827, USA', 28.37, -81.25, '32827', 'gym');
  assert (j->>'created')::boolean, j::text;
  assert j->>'suggestedHandle' = 'anon-picked-gym-orlando', j::text;
  -- Anonymous, so no author is recorded rather than a fake one.
  assert (select created_by from public.places
           where google_place_id = 'chk-gp-anonpick') is null, 'created_by should be null';
  -- Convergence still holds without a session.
  j := public.resolve_or_create_place('chk-gp-anonpick', 'Anon Picked Gym');
  assert not (j->>'created')::boolean, j::text;
end $$;

-- ── the claim-flow community card ────────────────────────────────────────────
do $$
declare j jsonb; v_place uuid := '00000000-0000-0000-0000-00000000c002';
begin
  update public.places set blurb = 'Busy in the mornings.' where id = v_place;

  -- No members: the card says so, and there is nothing to count.
  j := public.place_claim_card(v_place);
  assert not (j->>'hasMembers')::boolean, j::text;
  assert (j->>'memberCount')::int = 0, j::text;

  -- Two members is a real community and says so with a number. No floor: hiding "2"
  -- rendered the card as "people are already here", which says nothing at all.
  insert into public.circle_affiliations (user_id, place_ref, noun, emoji) values
    ('00000000-0000-0000-0000-00000000b001', v_place, 'gym', '🏋️'),
    ('00000000-0000-0000-0000-00000000b002', v_place, 'gym', '🏋️');
  j := public.place_claim_card(v_place);
  assert (j->>'hasMembers')::boolean, j::text;
  assert (j->>'memberCount')::int = 2, 'two members must show as two: ' || j::text;
  assert j->>'blurb' = 'Busy in the mornings.', j::text;
  assert j->>'noun' = 'gym', j::text;
  assert j->>'emoji' = '🏋️', j::text;

  insert into public.circle_affiliations (user_id, place_ref, noun, emoji) values
    ('00000000-0000-0000-0000-00000000a001', v_place, 'gym', '🏋️');
  j := public.place_claim_card(v_place);
  assert (j->>'memberCount')::int = 3, j::text;
  assert j->>'emoji' = '🏋️', j::text;

  -- A dismissed membership is not a member.
  update public.circle_affiliations set dismissed_at = now()
   where place_ref = v_place and user_id = '00000000-0000-0000-0000-00000000a001';
  j := public.place_claim_card(v_place);
  assert (j->>'memberCount')::int = 2, 'dismissed rows must not be counted: ' || j::text;

  assert public.place_claim_card('00000000-0000-0000-0000-0000000000ff') is null,
    'unknown place should return null';
end $$;

rollback;
