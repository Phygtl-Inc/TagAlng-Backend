-- TagAlng dev seed (tagalng-dev / local supabase db reset)
-- Re-runnable: fixed UUIDs. Never run on production.

delete from public.nudges
where sender_id in (
  'a0000001-0001-4000-8000-000000000001'::uuid,
  'a0000002-0002-4000-8000-000000000002'::uuid,
  'a0000003-0003-4000-8000-000000000003'::uuid
);

delete from public.event_requests
where event_id in (
  'eeee0001-0001-4000-8000-000000000001'::uuid,
  'eeee0002-0002-4000-8000-000000000002'::uuid,
  'eeee0003-0003-4000-8000-000000000003'::uuid,
  'eeee0004-0004-4000-8000-000000000004'::uuid
);

delete from public.thread_events
where event_id in (
  'eeee0001-0001-4000-8000-000000000001'::uuid,
  'eeee0002-0002-4000-8000-000000000002'::uuid,
  'eeee0003-0003-4000-8000-000000000003'::uuid,
  'eeee0004-0004-4000-8000-000000000004'::uuid
);

delete from public.events
where id in (
  'eeee0001-0001-4000-8000-000000000001'::uuid,
  'eeee0002-0002-4000-8000-000000000002'::uuid,
  'eeee0003-0003-4000-8000-000000000003'::uuid,
  'eeee0004-0004-4000-8000-000000000004'::uuid
);

delete from public.user_identity_claims
where user_id in (
  'a0000001-0001-4000-8000-000000000001'::uuid,
  'a0000002-0002-4000-8000-000000000002'::uuid,
  'a0000003-0003-4000-8000-000000000003'::uuid
);

insert into auth.users (
  instance_id, id, aud, role, email, encrypted_password, email_confirmed_at,
  raw_app_meta_data, raw_user_meta_data, created_at, updated_at, phone, phone_confirmed_at, is_anonymous
)
values
  ('00000000-0000-0000-0000-000000000000', 'a0000001-0001-4000-8000-000000000001', 'authenticated', 'authenticated', null, null, null, '{"provider":"phone","providers":["phone"]}'::jsonb, '{"seed_role":"host"}'::jsonb, now(), now(), '+15550100001', now(), false),
  ('00000000-0000-0000-0000-000000000000', 'a0000002-0002-4000-8000-000000000002', 'authenticated', 'authenticated', null, null, null, '{"provider":"phone","providers":["phone"]}'::jsonb, '{"seed_role":"guest"}'::jsonb, now(), now(), '+15550100002', now(), false),
  ('00000000-0000-0000-0000-000000000000', 'a0000003-0003-4000-8000-000000000003', 'authenticated', 'authenticated', null, null, null, '{"provider":"phone","providers":["phone"]}'::jsonb, '{"seed_role":"peer"}'::jsonb, now(), now(), '+15550100003', now(), false)
on conflict (id) do update set phone = excluded.phone, phone_confirmed_at = excluded.phone_confirmed_at, updated_at = now();

insert into auth.identities (id, user_id, provider_id, identity_data, provider, last_sign_in_at, created_at, updated_at)
values
  ('a0000001-0001-4000-8000-000000000001', 'a0000001-0001-4000-8000-000000000001', '+15550100001', jsonb_build_object('sub', 'a0000001-0001-4000-8000-000000000001', 'phone', '+15550100001'), 'phone', now(), now(), now()),
  ('a0000002-0002-4000-8000-000000000002', 'a0000002-0002-4000-8000-000000000002', '+15550100002', jsonb_build_object('sub', 'a0000002-0002-4000-8000-000000000002', 'phone', '+15550100002'), 'phone', now(), now(), now()),
  ('a0000003-0003-4000-8000-000000000003', 'a0000003-0003-4000-8000-000000000003', '+15550100003', jsonb_build_object('sub', 'a0000003-0003-4000-8000-000000000003', 'phone', '+15550100003'), 'phone', now(), now(), now())
on conflict (id) do nothing;

insert into public.users (id, phone, nickname, home_block_id, home_zip, phone_verified_at, locale)
values
  ('a0000001-0001-4000-8000-000000000001', '+15550100001', 'Marina', '8a2a1072b59ffff', '32827', now(), 'en'),
  ('a0000002-0002-4000-8000-000000000002', '+15550100002', 'Beatriz', '8a2a1072b59ffff', '32827', now(), 'en'),
  ('a0000003-0003-4000-8000-000000000003', '+15550100003', 'Carla', '8a2a1072b5affff', '32828', now(), 'en')
on conflict (id) do update set
  phone = excluded.phone, nickname = excluded.nickname, home_block_id = excluded.home_block_id,
  home_zip = excluded.home_zip, phone_verified_at = excluded.phone_verified_at, locale = excluded.locale, updated_at = now();

insert into public.user_identity_claims (user_id, concept, label, tone, confidence, disclosure, synonyms)
values
  ('a0000001-0001-4000-8000-000000000001', 'parents_toddlers', 'Mom of toddlers', 'warm', 0.92, 'public', array['mom','toddler mom']),
  ('a0000001-0001-4000-8000-000000000001', 'lake_nona_local', 'Lake Nona local', null, 0.88, 'public', '{}'),
  ('a0000001-0001-4000-8000-000000000001', 'faith_community', 'Faith community', null, 0.85, 'mutual', '{}'),
  ('a0000002-0002-4000-8000-000000000002', 'parents_toddlers', 'Mom of toddlers', null, 0.90, 'public', '{}'),
  ('a0000002-0002-4000-8000-000000000002', 'new_to_area', 'New to the area', null, 0.80, 'public', '{}'),
  ('a0000003-0003-4000-8000-000000000003', 'runner', 'Morning runner', null, 0.91, 'public', array['running','jogging']),
  ('a0000003-0003-4000-8000-000000000003', 'parents_elementary', 'Elementary school parent', null, 0.87, 'public', '{}');

insert into public.events (id, host_id, cluster_id, block_id, title, description, starts_at, ends_at, location, venue_name, cohort_tags, max_attendees, status)
values
  ('eeee0001-0001-4000-8000-000000000001', 'a0000001-0001-4000-8000-000000000001', 'lake-nona', '8a2a1072b59ffff', 'Sunday brunch for new moms', 'Casual brunch at Commons. Babies welcome.', now() + interval '3 days', now() + interval '3 days' + interval '90 minutes', extensions.st_setsrid(extensions.st_makepoint(-81.2568, 28.3647), 4326)::extensions.geography, 'Lake Nona Town Center', array['parents','faith'], 12, 'open'),
  ('eeee0002-0002-4000-8000-000000000002', 'a0000001-0001-4000-8000-000000000001', 'lake-nona', '8a2a1072b59ffff', 'Friday park playdate', 'Stroller-friendly.', now() + interval '5 days', now() + interval '5 days' + interval '2 hours', extensions.st_setsrid(extensions.st_makepoint(-81.2568, 28.3647), 4326)::extensions.geography, 'Laureate Park playground', array['parents'], 8, 'open'),
  ('eeee0003-0003-4000-8000-000000000003', 'a0000003-0003-4000-8000-000000000003', 'lake-nona', '8a2a1072b5affff', 'Saturday morning run', 'Easy 3-mile loop.', now() + interval '4 days', now() + interval '4 days' + interval '1 hour', extensions.st_setsrid(extensions.st_makepoint(-81.2621, 28.3689), 4326)::extensions.geography, 'Avalon Park trail', array['runner','sports'], 15, 'open'),
  ('eeee0004-0004-4000-8000-000000000004', 'a0000001-0001-4000-8000-000000000001', 'lake-nona', '8a2a1072b59ffff', 'Coffee + stroller walk', 'Short walk then coffee.', now() + interval '7 days', null, extensions.st_setsrid(extensions.st_makepoint(-81.2568, 28.3647), 4326)::extensions.geography, 'Canvas Restaurant patio', array['parents','newcomer'], 10, 'open');

insert into public.event_requests (id, event_id, requester_id, status, message, decided_at)
values
  ('eeee0005-0001-4000-8000-000000000001', 'eeee0001-0001-4000-8000-000000000001', 'a0000002-0002-4000-8000-000000000002', 'pending', 'Would love to join with my 2yo!', null),
  ('eeee0006-0002-4000-8000-000000000002', 'eeee0002-0002-4000-8000-000000000002', 'a0000002-0002-4000-8000-000000000002', 'approved', 'First time in the neighborhood.', now() - interval '1 hour')
on conflict (event_id, requester_id) do update set status = excluded.status, message = excluded.message, decided_at = excluded.decided_at;

insert into public.nudges (id, sender_id, recipient_id, sent_at)
values ('eeee0007-0001-4000-8000-000000000001', 'a0000003-0003-4000-8000-000000000003', 'a0000002-0002-4000-8000-000000000002', now() - interval '2 hours')
on conflict do nothing;

insert into public.block_waitlist_counts (block_id, signup_count, updated_at)
values ('8a2a1072b59ffff', 14, now()), ('8a2a1072b5affff', 9, now())
on conflict (block_id) do update set signup_count = excluded.signup_count, updated_at = now();
