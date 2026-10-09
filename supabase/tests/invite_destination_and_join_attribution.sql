-- 20270201120000_invite_destination_and_join_attribution · behaviour checks
-- (local validation container only — never against dev or prod)
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/invite_destination_and_join_attribution.sql
-- Everything runs in one transaction and is rolled back. Any failed assert aborts it.

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000ab001', 'inv-owner@example.com',   now(), false),
  ('00000000-0000-0000-0000-0000000ab002', 'inv-invitee@example.com', now(), false)
on conflict (id) do nothing;

-- A verified community with a handle, a chapter under it, a community with a handle
-- that is NOT verified, and a plain place with no page at all.
insert into public.places (id, google_place_id, name, lat, lng, created_by) values
  ('00000000-0000-0000-0000-0000000ac001', 'inv-parent',  'Invite Parent Club', 37.33, -121.88, '00000000-0000-0000-0000-0000000ab001'),
  ('00000000-0000-0000-0000-0000000ac003', 'inv-unver',   'Unverified Club',    28.54, -81.38,  '00000000-0000-0000-0000-0000000ab001'),
  ('00000000-0000-0000-0000-0000000ac004', 'inv-plain',   'Plain Gym',          28.54, -81.38,  '00000000-0000-0000-0000-0000000ab001');
insert into public.place_claims (place_id, requested_by, status, verification_method, submitted_at, resolved_at) values
  ('00000000-0000-0000-0000-0000000ac001', '00000000-0000-0000-0000-0000000ab001', 'verified', 'manual_founder', now(), now()),
  ('00000000-0000-0000-0000-0000000ac003', '00000000-0000-0000-0000-0000000ab001', 'verified', 'manual_founder', now(), now());
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000ab001', handle = 'invite-parent'
 where id = '00000000-0000-0000-0000-0000000ac001';
-- A handle exists only on a verified (or since-suspended) place
-- (places_handle_needs_verification): this one was verified, then suspended.
update public.places set governance_state = 'operator_verified', verified_at = now(),
       claimed_by = '00000000-0000-0000-0000-0000000ab001', handle = 'unverified-club'
 where id = '00000000-0000-0000-0000-0000000ac003';
update public.places set governance_state = 'suspended'
 where id = '00000000-0000-0000-0000-0000000ac003';
insert into public.places (id, google_place_id, name, lat, lng, parent_place_ref) values
  ('00000000-0000-0000-0000-0000000ac002', 'inv-chapter', 'Night Walkers', 37.33, -121.88,
   '00000000-0000-0000-0000-0000000ac001');

insert into public.circle_invites (id, token, owner_user_id, circle_type, place_ref, revoked_at) values
  ('00000000-0000-0000-0000-0000000ad001', 'tok-parent',  '00000000-0000-0000-0000-0000000ab001', 'other', '00000000-0000-0000-0000-0000000ac001', null),
  ('00000000-0000-0000-0000-0000000ad002', 'tok-chapter', '00000000-0000-0000-0000-0000000ab001', 'other', '00000000-0000-0000-0000-0000000ac002', null),
  ('00000000-0000-0000-0000-0000000ad003', 'tok-unlabel', '00000000-0000-0000-0000-0000000ab001', null,    null,                                    null),
  ('00000000-0000-0000-0000-0000000ad004', 'tok-revoked', '00000000-0000-0000-0000-0000000ab001', 'other', '00000000-0000-0000-0000-0000000ac001', now()),
  ('00000000-0000-0000-0000-0000000ad005', 'tok-unver',   '00000000-0000-0000-0000-0000000ab001', 'other', '00000000-0000-0000-0000-0000000ac003', null),
  ('00000000-0000-0000-0000-0000000ad006', 'tok-plain',   '00000000-0000-0000-0000-0000000ab001', 'other', '00000000-0000-0000-0000-0000000ac004', null);

-- ── 1 · invite_destination ───────────────────────────────────────────────────

do $$
declare
  j jsonb;
begin
  j := public.invite_destination('tok-parent');
  assert j is not null, 'handle community resolves';
  assert j->>'placeId' = '00000000-0000-0000-0000-0000000ac001', 'placeId is the invite place';
  assert j->>'link' = 'invite-parent', 'link is the handle';
  assert j->>'displayName' = 'Invite Parent Club', 'displayName is the place name';
  assert (select array_agg(k order by k) from jsonb_object_keys(j) k)
         = array['displayName', 'link', 'placeId'],
    'exactly placeId/link/displayName — no owner/inviter fields';

  j := public.invite_destination(' tok-parent ');
  assert j->>'link' = 'invite-parent', 'token is trimmed';

  j := public.invite_destination('tok-chapter');
  assert j is not null, 'chapter resolves';
  assert j->>'placeId' = '00000000-0000-0000-0000-0000000ac002', 'chapter placeId';
  assert j->>'link' = 'invite-parent/' ||
         (select chapter_handle from public.places where id = '00000000-0000-0000-0000-0000000ac002'),
    'chapter link is parent/chapter';
  assert j->>'link' = 'invite-parent/nightwalkers', 'chapter link value';
  assert j->>'displayName' = 'Night Walkers', 'chapter displayName';
  assert j::text not ilike '%ab001%', 'no owner id anywhere in the output';

  assert public.invite_destination('tok-unlabel') is null, 'unlabeled invite → null';
  assert public.invite_destination('tok-revoked') is null, 'revoked invite → null';
  assert public.invite_destination('tok-unver') is null, 'suspended (no longer verified) place → null';
  assert public.invite_destination('tok-plain') is null, 'place without a page → null';
  assert public.invite_destination('nope') is null, 'unknown token → null';
  assert public.invite_destination(null) is null, 'null token → null';
end;
$$;

-- A chapter whose parent loses its page (suspended) stops resolving, both ways.
savepoint parent_unverified;
update public.places set governance_state = 'suspended'
 where id = '00000000-0000-0000-0000-0000000ac001';
do $$
begin
  assert public.invite_destination('tok-chapter') is null, 'chapter of a suspended parent → null';
  assert public.invite_destination('tok-parent') is null, 'suspended parent → null';
end;
$$;
rollback to savepoint parent_unverified;

-- Defense in depth: should the suspended place's word ever resolve to ANOTHER community
-- (an alias row — place_handle_aliases_not_live normally forbids one equal to a live
-- handle, so it is switched off for this case only), the invite must not follow it there.
savepoint alias_elsewhere;
alter table public.place_handle_aliases disable trigger place_handle_aliases_not_live;
insert into public.place_handle_aliases (handle, place_id)
values ('unverified-club', '00000000-0000-0000-0000-0000000ac001');
do $$
begin
  assert public.resolve_place_handle('unverified-club')->>'placeId'
         = '00000000-0000-0000-0000-0000000ac001', 'fixture: the word resolves elsewhere';
  assert public.invite_destination('tok-unver') is null,
    'an invite never redirects to a different community than its own';
end;
$$;
rollback to savepoint alias_elsewhere;

-- ── 2 · redemption columns + view ────────────────────────────────────────────

insert into public.circle_invite_redemptions (invite_id, user_id) values
  ('00000000-0000-0000-0000-0000000ad001', '00000000-0000-0000-0000-0000000ab002');
do $$
begin
  assert (select count(*) from public.community_invite_joins) = 0, 'an open is not a join';
end;
$$;
update public.circle_invite_redemptions
   set joined_at = now(), joined_place_ref = '00000000-0000-0000-0000-0000000ac001'
 where invite_id = '00000000-0000-0000-0000-0000000ad001'
   and user_id = '00000000-0000-0000-0000-0000000ab002';
do $$
declare
  r record;
begin
  select * into r from public.community_invite_joins
   where invite_id = '00000000-0000-0000-0000-0000000ad001';
  assert r.token = 'tok-parent', 'view carries the token';
  assert r.inviter_user_id = '00000000-0000-0000-0000-0000000ab001', 'view carries the inviter';
  assert r.invitee_user_id = '00000000-0000-0000-0000-0000000ab002', 'view carries the invitee';
  assert r.place_ref = '00000000-0000-0000-0000-0000000ac001', 'view carries the place';
  assert r.joined_at is not null, 'view carries joined_at';
end;
$$;

set local role service_role;
do $$
begin
  assert (select count(*) from public.community_invite_joins) >= 1, 'service_role reads the view';
end;
$$;
reset role;

-- ── 3 · grants ───────────────────────────────────────────────────────────────

do $$
begin
  assert has_function_privilege('anon', 'public.invite_destination(text)', 'execute'),
    'anon may execute invite_destination';
  assert has_function_privilege('authenticated', 'public.invite_destination(text)', 'execute'),
    'authenticated may execute invite_destination';
  assert not has_table_privilege('anon', 'public.community_invite_joins', 'select'),
    'anon may not select the view';
  assert not has_table_privilege('authenticated', 'public.community_invite_joins', 'select'),
    'authenticated may not select the view';
end;
$$;

set local role anon;
do $$
begin
  assert public.invite_destination('tok-parent')->>'link' = 'invite-parent',
    'anon resolves an invite destination';
  begin
    perform 1 from public.community_invite_joins;
    assert false, 'anon must not read community_invite_joins';
  exception when insufficient_privilege then null;
  end;
end;
$$;
reset role;

set local role authenticated;
do $$
begin
  assert public.invite_destination('tok-chapter') is not null,
    'authenticated resolves an invite destination';
  begin
    perform 1 from public.community_invite_joins;
    assert false, 'authenticated must not read community_invite_joins';
  exception when insufficient_privilege then null;
  end;
end;
$$;
reset role;

select 'invite_destination_and_join_attribution: all assertions passed' as result;

rollback;
