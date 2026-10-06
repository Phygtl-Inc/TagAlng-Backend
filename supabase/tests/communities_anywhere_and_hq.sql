-- 20270109120000_communities_anywhere_and_hq + 20270110120000 (own words only) · checks
--
-- Local validation container only (never dev/prod); one transaction, rolled back.
--   psql -h 127.0.0.1 -p 55432 -U supabase_admin -d postgres -v ON_ERROR_STOP=1 \
--        -f supabase/tests/communities_anywhere_and_hq.sql

begin;

insert into auth.users (id, email, email_confirmed_at, is_anonymous) values
  ('00000000-0000-0000-0000-0000000000e1', 'host@example.com',   now(), false),
  ('00000000-0000-0000-0000-0000000000e2', 'seeker@example.com', now(), false),
  ('00000000-0000-0000-0000-0000000000e3', 'member@example.com', now(), false)
on conflict (id) do nothing;

-- Placeless, chat-made: the podcasters community.
insert into public.places (id, google_place_id, name, blurb, created_by)
values ('00000000-0000-0000-0000-0000000000f1', 'creator:podcast-club', 'Podcast Club',
        'For people who make shows and want feedback', '00000000-0000-0000-0000-0000000000e1');
-- Placeless, but a test row: never offered.
insert into public.places (id, google_place_id, name, is_test, created_by)
values ('00000000-0000-0000-0000-0000000000f2', 'creator:podcast-test', 'Podcast Test Lab', true,
        '00000000-0000-0000-0000-0000000000e1');
-- Placeless with nobody in it: never offered.
insert into public.places (id, google_place_id, name, created_by)
values ('00000000-0000-0000-0000-0000000000f3', 'creator:empty-pods', 'Empty Podcasters',
        '00000000-0000-0000-0000-0000000000e1');
-- A real place far away, with a location.
insert into public.places (id, google_place_id, name, place_type, lat, lng, zip, address)
values ('00000000-0000-0000-0000-0000000000f4', 'gp-sjsu-2', 'San Jose State University',
        'school', 37.335, -121.881, '95192', '1 Washington Sq, San Jose, CA 95192, USA');
-- Placeless, NOT about podcasts — but its member calls themselves a podcaster.
insert into public.places (id, google_place_id, name, created_by)
values ('00000000-0000-0000-0000-0000000000f5', 'creator:mic-check', 'Mic Check Crew',
        '00000000-0000-0000-0000-0000000000e3');

insert into public.circle_affiliations (user_id, circle_type, circle_key, source, status, place_ref)
values
  ('00000000-0000-0000-0000-0000000000e1', 'hobby',  'podcast_club', 'profile_add', 'confirmed', '00000000-0000-0000-0000-0000000000f1'),
  ('00000000-0000-0000-0000-0000000000e1', 'hobby',  'podcast_test', 'profile_add', 'confirmed', '00000000-0000-0000-0000-0000000000f2'),
  ('00000000-0000-0000-0000-0000000000e3', 'school', 'sjsu',         'profile_add', 'confirmed', '00000000-0000-0000-0000-0000000000f4'),
  ('00000000-0000-0000-0000-0000000000e3', 'hobby',  'mic_check',    'profile_add', 'confirmed', '00000000-0000-0000-0000-0000000000f5');

-- One member's public claim says "Podcaster". That must never make their communities
-- podcast communities (prod 2026-10-06: Gym Fans answered "any communities for podcasters?").
insert into public.user_identity_claims (user_id, concept, label, confidence, disclosure,
                                         subject_kind, transient)
values ('00000000-0000-0000-0000-0000000000e3', 'podcaster', 'Podcaster', 0.9,
        'public', 'self', false);

do $$
declare
  seeker uuid := '00000000-0000-0000-0000-0000000000e2';
  host   uuid := '00000000-0000-0000-0000-0000000000e1';
  member uuid := '00000000-0000-0000-0000-0000000000e3';
  club   uuid := '00000000-0000-0000-0000-0000000000f1';
  sjsu   uuid := '00000000-0000-0000-0000-0000000000f4';
  ids    uuid[];
  r      record;
  j      jsonb;
begin

  -- The podcasters ask finds the placeless community by its own words (stems + prefix).
  select array_agg(place_id) into ids
    from public.discover_communities_anywhere(seeker, 'any communities for podcasters?');
  assert ids = array[club], 'podcasters → only Podcast Club, got ' || coalesce(ids::text, 'none');

  select * into r from public.discover_communities_anywhere(seeker, 'podcasting');
  assert r.place_id = club and r.matched_on = 'about' and r.member_count = 1
         and not r.is_member, 'podcasting matched on what it is: ' || row_to_json(r)::text;

  -- An exact name ranks as a name match.
  select * into r from public.discover_communities_anywhere(seeker, 'Podcast Club');
  assert r.matched_on = 'name', 'name match: ' || row_to_json(r)::text;

  -- Located places are not "anywhere" communities…
  select array_agg(place_id) into ids
    from public.discover_communities_anywhere(seeker, 'San Jose State University');
  assert ids is null, 'placeless-only by default: ' || coalesce(ids::text, '');
  -- …but a named lookup can reach them from anywhere.
  select array_agg(place_id) into ids
    from public.discover_communities_anywhere(seeker, 'San Jose State University',
                                              p_placeless_only => false);
  assert ids = array[sjsu], 'named lookup reaches SJSU: ' || coalesce(ids::text, 'none');

  -- A member's hobby is not the community's topic: Mic Check Crew (whose member is a
  -- "Podcaster") is never a podcast community. Podcast Club, about podcasts, is.
  select array_agg(place_id) into ids
    from public.discover_communities_anywhere(seeker, 'podcaster', p_placeless_only => false);
  assert ids = array[club], 'member claim qualified a community: ' || coalesce(ids::text, 'none');

  -- is_member is the caller's own view.
  select * into r from public.discover_communities_anywhere(host, 'podcast');
  assert r.place_id = club and r.is_member, 'host sees it as theirs';

  -- Nothing to search with → nothing, not everything.
  select array_agg(place_id) into ids from public.discover_communities_anywhere(seeker, '  ');
  assert ids is null, 'empty ask returns nothing';

  -- HQ: the starter may set it; a stranger may not; the pin is both-or-neither.
  j := public.set_community_hq_for(seeker, club, 'Orlando, FL', 28.54, -81.38);
  assert j->>'status' = 'not_eligible', 'stranger refused ' || j::text;
  j := public.set_community_hq_for(host, club, 'Orlando, FL', 28.54, null);
  assert j->>'status' = 'invalid', 'half a pin refused ' || j::text;
  j := public.set_community_hq_for(host, club, 'Orlando, FL', 28.54, -81.38);
  assert j->>'status' = 'saved', 'starter saves HQ ' || j::text;
  assert (select hq_city = 'Orlando, FL' and hq_lat = 28.54 and lat is null
            from public.places where id = club), 'HQ stored, place stays placeless';

  -- HQ never makes it "near": still found anywhere, still absent from the radius read.
  select * into r from public.discover_communities_anywhere(seeker, 'podcast');
  assert r.place_id = club and r.hq_city = 'Orlando, FL', 'HQ rides along for display';

  raise notice 'communities_anywhere_and_hq: all checks passed';
end;
$$;

rollback;
