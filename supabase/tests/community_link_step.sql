-- 20270130120000_community_link_step · behaviour checks (local validation container only)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/community_link_step.sql

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-00000000aa01', 'maker@example.com',  now(), false),
  ('00000000-0000-0000-0000-00000000aa02', null,                 null,  true),
  ('00000000-0000-0000-0000-00000000aa03', 'other@example.com',  now(), false)
on conflict (id) do nothing;
update public.users set handle = 'rosie' where id = '00000000-0000-0000-0000-00000000aa03';
insert into public.protected_handles (normalized_handle, reason, active)
values ('nike', 'test brand', true) on conflict (normalized_handle) do update set active = true;

-- A place someone else already started, and one that already has its link.
insert into public.places (id, google_place_id, name, created_by)
values ('00000000-0000-0000-0000-00000000bb01', 'gp-cafe', 'Corner Cafe',
        '00000000-0000-0000-0000-00000000aa03');
insert into public.places (id, google_place_id, name, created_by)
values ('00000000-0000-0000-0000-00000000bb02', 'creator:rosettas-bakery', 'Rosetta''s Bakery',
        '00000000-0000-0000-0000-00000000aa01');
insert into public.place_claims (place_id, requested_by, status, verification_method,
                                 submitted_at, resolved_at)
values ('00000000-0000-0000-0000-00000000bb02', '00000000-0000-0000-0000-00000000aa01',
        'verified', 'manual_founder', now(), now());
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-00000000aa01', handle = 'rosettas-bakery-orl'
 where id = '00000000-0000-0000-0000-00000000bb02';

do $$
declare
  maker uuid := '00000000-0000-0000-0000-00000000aa01';
  guest uuid := '00000000-0000-0000-0000-00000000aa02';
  j     jsonb;
begin
  -- Guests never reach the step.
  j := public.check_community_handle_for(guest, 'anything', 'Anything');
  assert j->>'status' = 'sign_in_required', 'guest ' || j::text;

  -- The prefill: compact name first, then dashed.
  j := public.check_community_handle_for(maker, '', 'Sunset Readers');
  assert j->>'status' = 'invalid'
     and j->'suggestions'->>0 = 'sunsetreaders'
     and j->'suggestions'->>1 = 'sunset-readers', 'prefill from name ' || j::text;

  j := public.check_community_handle_for(maker, 'SunsetReaders', 'Sunset Readers');
  assert j->>'status' = 'available' and j->>'normalizedHandle' = 'sunsetreaders',
         'free bare link ' || j::text;

  j := public.check_community_handle_for(maker, 'nike', 'Nike Runners');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'protected', 'protected ' || j::text;
  j := public.check_community_handle_for(maker, 'rosie', 'Rosie Club');
  assert j->>'status' = 'unavailable' and j->>'reason' = 'member', 'member ' || j::text;
  assert not (j->'suggestions' ? 'rosie'), 'never suggests what was refused';

  j := public.check_community_handle_for(maker, 'a', 'Sunset Readers');
  assert j->>'status' = 'invalid' and j->>'reason' = 'too_short', 'shape ' || j::text;

  -- A place that already exists: someone else's is not theirs to link …
  j := public.check_community_handle_for(maker, 'cornercafe', 'Corner Cafe', 'gp-cafe');
  assert j->>'status' = 'not_eligible', 'someone else''s place ' || j::text;
  -- … and one that already has a link says which.
  j := public.check_community_handle_for(maker, 'rosettasbakery', 'Rosetta''s Bakery',
                                         'creator:rosettas-bakery');
  assert j->>'status' = 'already_has_handle' and j->>'handle' = 'rosettas-bakery-orl',
         'existing link ' || j::text;
  -- A place that does not exist yet is fine.
  j := public.check_community_handle_for(maker, 'newspot', 'New Spot', 'gp-new');
  assert j->>'status' = 'available', 'new place ' || j::text;

  raise notice 'community_link_step: all checks passed';
end;
$$;

rollback;
