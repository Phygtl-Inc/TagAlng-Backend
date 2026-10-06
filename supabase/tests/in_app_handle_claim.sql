-- 20270108120000_in_app_handle_claim · behaviour checks
--
-- Run against a throwaway database with every migration applied (the local validation
-- container — never dev or prod). Everything happens in one transaction that is rolled back.
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/in_app_handle_claim.sql
-- Prints "in_app_handle_claim: all checks passed" or stops at the first failed assert.

begin;

-- ── fixtures ───────────────────────────────────────────────────────────────────
-- Users: insert into auth.users; the handle_new_user trigger makes the public.users row.
insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000000a1', 'pouya@example.com',  now(), false),  -- SJSU operator
  ('00000000-0000-0000-0000-0000000000a2', 'maya@example.com',   now(), false),  -- name-only creator
  ('00000000-0000-0000-0000-0000000000a3', 'member@example.com', now(), false),  -- just a member
  ('00000000-0000-0000-0000-0000000000a4', null,                 null,  true),   -- guest
  ('00000000-0000-0000-0000-0000000000a5', 'unconf@example.com', null,  false),  -- never confirmed
  ('00000000-0000-0000-0000-0000000000a6', 'owner@example.com',  now(), false),  -- location claimant
  ('00000000-0000-0000-0000-0000000000a7', 'staff@example.com',  now(), false)   -- internal reviewer
on conflict (id) do nothing;

update public.users set handle = 'tex'
 where id = '00000000-0000-0000-0000-0000000000a3';
update public.users set founder_role = 'internal'
 where id = '00000000-0000-0000-0000-0000000000a7';

insert into public.protected_handles (normalized_handle, reason, active)
values ('nike', 'test brand', true)
on conflict (normalized_handle) do update set active = true;

-- SJSU: a real place, made in-app, hand-verified with a manual_founder claim (as in prod).
insert into public.places (id, google_place_id, name, place_type, address, created_by)
values ('00000000-0000-0000-0000-00000000b001', 'gp-sjsu', 'San Jose State University',
        'school', '1 Washington Sq, San Jose, CA 95192, USA',
        '00000000-0000-0000-0000-0000000000a3');
insert into public.place_claims (place_id, requested_by, status, verification_method,
                                 review_notes, submitted_at, resolved_at)
values ('00000000-0000-0000-0000-00000000b001', '00000000-0000-0000-0000-0000000000a1',
        'verified', 'manual_founder', 'test: founder verified by hand', now(), now());
update public.places
   set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000000a1', claimed_at = now()
 where id = '00000000-0000-0000-0000-00000000b001';

-- A name-only community Maya made in chat (creator:<slug>, no type, community_started).
insert into public.places (id, google_place_id, name, place_type, created_by)
values ('00000000-0000-0000-0000-00000000b002', 'creator:austin-run-club', 'Austin Run Club',
        null, '00000000-0000-0000-0000-0000000000a2');

-- A community on a real gym, started by the member.
insert into public.places (id, google_place_id, name, place_type, address, created_by)
values ('00000000-0000-0000-0000-00000000b003', 'gp-gym', 'Fitness CF', 'fitness',
        '9145 Narcoossee Rd, Orlando, FL 32827, USA', '00000000-0000-0000-0000-0000000000a3');

-- A store mid-way through lana.help's location claim by email: bare reservation "safeway".
insert into public.places (id, google_place_id, name, address)
values ('00000000-0000-0000-0000-00000000b004', 'gp-safeway', 'Safeway',
        '1050 Foster City Blvd, Foster City, CA 94404, USA');
insert into public.place_handle_reservations (id, normalized_handle, token_hash, user_id,
                                              place_id, status, expires_at)
values ('00000000-0000-0000-0000-00000000c004', 'safeway', 'tok-safeway',
        '00000000-0000-0000-0000-0000000000a6', '00000000-0000-0000-0000-00000000b004',
        'bound', now() + interval '10 minutes');
insert into public.place_claims (place_id, reservation_id, requested_by, status, submitted_at)
values ('00000000-0000-0000-0000-00000000b004', '00000000-0000-0000-0000-00000000c004',
        '00000000-0000-0000-0000-0000000000a6', 'pending_verification', now());

-- A place waiting on an internal reviewer, dashed reservation.
insert into public.places (id, google_place_id, name, address)
values ('00000000-0000-0000-0000-00000000b005', 'gp-church', 'Grace Church',
        '12 Main St, Austin, TX 78701, USA');
insert into public.place_handle_reservations (id, normalized_handle, token_hash, user_id,
                                              place_id, status, expires_at)
values ('00000000-0000-0000-0000-00000000c005', 'grace-church-austin', 'tok-grace',
        '00000000-0000-0000-0000-0000000000a6', '00000000-0000-0000-0000-00000000b005',
        'bound', now() + interval '10 minutes');
insert into public.place_claims (id, place_id, reservation_id, requested_by, status, submitted_at)
values ('00000000-0000-0000-0000-00000000d005', '00000000-0000-0000-0000-00000000b005',
        '00000000-0000-0000-0000-00000000c005', '00000000-0000-0000-0000-0000000000a6',
        'pending_verification', now());

-- ── checks ─────────────────────────────────────────────────────────────────────
do $$
declare
  pouya  uuid := '00000000-0000-0000-0000-0000000000a1';
  maya   uuid := '00000000-0000-0000-0000-0000000000a2';
  member uuid := '00000000-0000-0000-0000-0000000000a3';
  guest  uuid := '00000000-0000-0000-0000-0000000000a4';
  unconf uuid := '00000000-0000-0000-0000-0000000000a5';
  sjsu   uuid := '00000000-0000-0000-0000-00000000b001';
  club   uuid := '00000000-0000-0000-0000-00000000b002';
  gym    uuid := '00000000-0000-0000-0000-00000000b003';
  r      jsonb;
  ok     boolean;
begin
  -- Offer: SJSU's operator is eligible and gets a usable suggestion.
  r := public.community_handle_offer_for(pouya, sjsu);
  assert (r->>'eligible')::boolean, 'offer: operator eligible ' || r::text;
  assert r->>'suggestion' = 'san-jose-state-university', 'offer: suggestion ' || r::text;

  -- Who may claim.
  r := public.claim_community_handle_for(guest, sjsu, 'sjsu');
  assert r->>'status' = 'sign_in_required', 'guest refused ' || r::text;
  r := public.claim_community_handle_for(unconf, sjsu, 'sjsu');
  assert r->>'status' = 'sign_in_required', 'unconfirmed email refused ' || r::text;
  r := public.claim_community_handle_for(member, sjsu, 'sjsu');
  assert r->>'status' = 'not_eligible', 'non-operator refused ' || r::text;
  r := public.claim_community_handle_for(member, gym, 'fitnesscf');
  assert r->>'status' = 'not_eligible', 'real-place starter cannot self-verify ' || r::text;
  r := public.community_handle_offer_for(member, gym);
  assert not (r->>'eligible')::boolean, 'real-place starter gets no offer ' || r::text;

  -- What may be claimed.
  r := public.claim_community_handle_for(pouya, sjsu, 'a!');
  assert r->>'status' = 'invalid', 'bad shape ' || r::text;
  r := public.claim_community_handle_for(pouya, sjsu, 'Nike');
  assert r->>'status' = 'unavailable' and r->>'reason' = 'protected', 'protected ' || r::text;
  r := public.claim_community_handle_for(pouya, sjsu, 'tex');
  assert r->>'status' = 'unavailable' and r->>'reason' = 'member', 'member handle ' || r::text;
  assert jsonb_array_length(r->'suggestions') > 0, 'refusal carries suggestions ' || r::text;

  -- Item 1 + option A: a SCHOOL gets a bare handle, without spending its rename.
  r := public.claim_community_handle_for(pouya, sjsu, 'SJSU');
  assert r->>'status' = 'claimed' and r->>'handle' = 'sjsu', 'school claims sjsu ' || r::text;
  assert (select handle = 'sjsu' and handle_renamed_at is null
            from public.places where id = sjsu), 'sjsu stored, rename unspent';
  assert exists (select 1 from public.place_claims c
                   join public.place_handle_reservations h on h.id = c.reservation_id
                  where c.place_id = sjsu and c.status = 'verified'
                    and c.verification_method = 'reservation_email'
                    and h.normalized_handle = 'sjsu' and h.status = 'consumed'),
         'proof row written';
  r := public.claim_community_handle_for(pouya, sjsu, 'sjsu-2');
  assert r->>'status' = 'already_has_handle', 'second claim refused ' || r::text;

  -- Taken by another community: refused with suggestions.
  r := public.claim_community_handle_for(maya, club, 'sjsu');
  assert r->>'status' = 'unavailable' and r->>'reason' = 'taken', 'taken ' || r::text;

  -- Name-only creator self-verifies and becomes the operator.
  r := public.claim_community_handle_for(maya, club, 'austinrun');
  assert r->>'status' = 'claimed', 'name-only creator claims ' || r::text;
  assert (select governance_state = 'operator_verified' and claimed_by = maya
            from public.places where id = club), 'creator verified';
  assert public.is_community_operator(club, maya), 'creator became operator';

  -- The guard still refuses a bare handle with no proof, for any type.
  begin
    update public.places set handle = 'fitnesscf' where id = gym;
    ok := false;
  exception when others then
    ok := sqlerrm in ('single_token_handle_requires_matching_identity',
                      'new row for relation "places" violates check constraint "places_handle_needs_verification"');
  end;
  assert ok, 'unproven bare handle refused';
  begin
    update public.places set handle = 'tex-2', governance_state = 'operator_verified'
     where id = gym;
    ok := false;
  exception when others then
    ok := sqlerrm = 'operator_verified_requires_verified_claim';
  end;
  assert ok, 'verify without a claim still refused';

  raise notice 'claim/offer checks passed';
end;
$$;

-- Item 2: the email claim completes instead of raising, and a bare reservation degrades to
-- the locality handle (never the bare brand word).
do $$
declare r jsonb;
begin
  r := public.complete_place_claim_by_email('tok-safeway');
  assert r->>'status' = 'verified', 'email claim completes ' || r::text;
  assert r->>'handle' like 'safeway-%', 'bare reservation degrades to locality ' || r::text;
  assert (select governance_state = 'operator_verified' from public.places
           where id = '00000000-0000-0000-0000-00000000b004'), 'store verified';
  raise notice 'complete_place_claim_by_email checks passed';
end;
$$;

-- Admin approval completes instead of raising.
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000a7', true);
select set_config('request.jwt.claims',
  '{"sub":"00000000-0000-0000-0000-0000000000a7","role":"authenticated"}', true);
do $$
declare r jsonb;
begin
  r := public.approve_place_claim('00000000-0000-0000-0000-00000000d005');
  assert r->>'status' = 'verified' and r->>'handle' = 'grace-church-austin',
         'admin approval completes ' || r::text;
  raise notice 'approve_place_claim checks passed';
end;
$$;

-- The authenticated wrappers read auth.uid().
select set_config('request.jwt.claim.sub', '00000000-0000-0000-0000-0000000000a4', true);
select set_config('request.jwt.claims',
  '{"sub":"00000000-0000-0000-0000-0000000000a4","role":"authenticated"}', true);
do $$
declare r jsonb;
begin
  r := public.claim_community_handle('00000000-0000-0000-0000-00000000b003', 'anything');
  assert r->>'status' = 'sign_in_required', 'wrapper uses auth.uid() ' || r::text;
  raise notice 'in_app_handle_claim: all checks passed';
end;
$$;

rollback;
