-- ─────────────────────────────────────────────────────────────────────────────
-- LANA nightly-simulation accounts  (p1-sim … p6-sim @ phygtl.dev)
-- ─────────────────────────────────────────────────────────────────────────────
-- 6 stable Supabase identities the nightly simulation cron authenticates as.
-- Email accounts, pre-confirmed (email_confirmed_at set), each with a home_block_id.
--
-- Mirrors the test-identity pattern in seed.sql (direct auth.users + auth.identities
-- insert) but for the EMAIL provider added in 20260716120000_email_auth.sql.
--
-- Re-runnable (fixed UUIDs + on-conflict). DEV / STAGING ONLY — do NOT run on prod
-- without review. The emails are Supabase-identity placeholders, not real inboxes.
--
-- HOW THE CRON GETS A TOKEN (no hardcoded JWTs — they expire in ~1h):
--   each run calls auth.signInWithPassword(email, <SIM_PASSWORD>) with the anon key,
--   OR the service role mints one via admin.generateLink / admin.createSession.
--   Set the password below before running, store it as a cron secret, never commit it.
--
-- EDIT BEFORE RUNNING:
--   • :sim_password   — the shared sim password (psql -v sim_password=...)
--   • home_block_id   — currently all Block A; split across blocks A/B if the
--                       sim needs cross-block coverage (see LANA_SIMULATION_v1.md
--                       once that doc exists — it is NOT in the repo yet).
-- ─────────────────────────────────────────────────────────────────────────────

\set sim_password 'CHANGE_ME_sim_password'

-- 1. auth.users — confirmed email accounts -----------------------------------
insert into auth.users (
  instance_id, id, aud, role, email, encrypted_password, email_confirmed_at,
  raw_app_meta_data, raw_user_meta_data, created_at, updated_at, is_anonymous
)
values
  ('00000000-0000-0000-0000-000000000000', '51000001-0001-4000-8000-000000000001', 'authenticated', 'authenticated', 'p1-sim@phygtl.dev', extensions.crypt(:'sim_password', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}'::jsonb, '{"seed_role":"sim","sim_persona":"p1"}'::jsonb, now(), now(), false),
  ('00000000-0000-0000-0000-000000000000', '51000002-0002-4000-8000-000000000002', 'authenticated', 'authenticated', 'p2-sim@phygtl.dev', extensions.crypt(:'sim_password', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}'::jsonb, '{"seed_role":"sim","sim_persona":"p2"}'::jsonb, now(), now(), false),
  ('00000000-0000-0000-0000-000000000000', '51000003-0003-4000-8000-000000000003', 'authenticated', 'authenticated', 'p3-sim@phygtl.dev', extensions.crypt(:'sim_password', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}'::jsonb, '{"seed_role":"sim","sim_persona":"p3"}'::jsonb, now(), now(), false),
  ('00000000-0000-0000-0000-000000000000', '51000004-0004-4000-8000-000000000004', 'authenticated', 'authenticated', 'p4-sim@phygtl.dev', extensions.crypt(:'sim_password', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}'::jsonb, '{"seed_role":"sim","sim_persona":"p4"}'::jsonb, now(), now(), false),
  ('00000000-0000-0000-0000-000000000000', '51000005-0005-4000-8000-000000000005', 'authenticated', 'authenticated', 'p5-sim@phygtl.dev', extensions.crypt(:'sim_password', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}'::jsonb, '{"seed_role":"sim","sim_persona":"p5"}'::jsonb, now(), now(), false),
  ('00000000-0000-0000-0000-000000000000', '51000006-0006-4000-8000-000000000006', 'authenticated', 'authenticated', 'p6-sim@phygtl.dev', extensions.crypt(:'sim_password', extensions.gen_salt('bf')), now(), '{"provider":"email","providers":["email"]}'::jsonb, '{"seed_role":"sim","sim_persona":"p6"}'::jsonb, now(), now(), false)
on conflict (id) do update set
  email              = excluded.email,
  encrypted_password = excluded.encrypted_password,
  email_confirmed_at = coalesce(auth.users.email_confirmed_at, excluded.email_confirmed_at),
  raw_app_meta_data  = excluded.raw_app_meta_data,
  raw_user_meta_data = excluded.raw_user_meta_data,
  updated_at         = now();

-- 2. auth.identities — email provider rows -----------------------------------
--    For the email provider GoTrue uses provider_id = user id (the sub).
insert into auth.identities (id, user_id, provider_id, identity_data, provider, last_sign_in_at, created_at, updated_at)
values
  ('51000001-0001-4000-8000-000000000001', '51000001-0001-4000-8000-000000000001', '51000001-0001-4000-8000-000000000001', jsonb_build_object('sub','51000001-0001-4000-8000-000000000001','email','p1-sim@phygtl.dev','email_verified',true), 'email', now(), now(), now()),
  ('51000002-0002-4000-8000-000000000002', '51000002-0002-4000-8000-000000000002', '51000002-0002-4000-8000-000000000002', jsonb_build_object('sub','51000002-0002-4000-8000-000000000002','email','p2-sim@phygtl.dev','email_verified',true), 'email', now(), now(), now()),
  ('51000003-0003-4000-8000-000000000003', '51000003-0003-4000-8000-000000000003', '51000003-0003-4000-8000-000000000003', jsonb_build_object('sub','51000003-0003-4000-8000-000000000003','email','p3-sim@phygtl.dev','email_verified',true), 'email', now(), now(), now()),
  ('51000004-0004-4000-8000-000000000004', '51000004-0004-4000-8000-000000000004', '51000004-0004-4000-8000-000000000004', jsonb_build_object('sub','51000004-0004-4000-8000-000000000004','email','p4-sim@phygtl.dev','email_verified',true), 'email', now(), now(), now()),
  ('51000005-0005-4000-8000-000000000005', '51000005-0005-4000-8000-000000000005', '51000005-0005-4000-8000-000000000005', jsonb_build_object('sub','51000005-0005-4000-8000-000000000005','email','p5-sim@phygtl.dev','email_verified',true), 'email', now(), now(), now()),
  ('51000006-0006-4000-8000-000000000006', '51000006-0006-4000-8000-000000000006', '51000006-0006-4000-8000-000000000006', jsonb_build_object('sub','51000006-0006-4000-8000-000000000006','email','p6-sim@phygtl.dev','email_verified',true), 'email', now(), now(), now())
on conflict (id) do nothing;

-- 3. public.users — home_block_id + verified mirror --------------------------
--    handle_new_user() already created bare rows on the auth.users insert; this
--    sets home_block_id and email_verified_at (the auth gate flag). All 6 default
--    to Block A — edit home_block_id per row to split across blocks.
insert into public.users (id, email, email_verified_at, nickname, home_block_id, locale)
values
  ('51000001-0001-4000-8000-000000000001', 'p1-sim@phygtl.dev', now(), 'Sim P1', '8a2a1072b59ffff', 'en'),
  ('51000002-0002-4000-8000-000000000002', 'p2-sim@phygtl.dev', now(), 'Sim P2', '8a2a1072b59ffff', 'en'),
  ('51000003-0003-4000-8000-000000000003', 'p3-sim@phygtl.dev', now(), 'Sim P3', '8a2a1072b59ffff', 'en'),
  ('51000004-0004-4000-8000-000000000004', 'p4-sim@phygtl.dev', now(), 'Sim P4', '8a2a1072b59ffff', 'en'),
  ('51000005-0005-4000-8000-000000000005', 'p5-sim@phygtl.dev', now(), 'Sim P5', '8a2a1072b59ffff', 'en'),
  ('51000006-0006-4000-8000-000000000006', 'p6-sim@phygtl.dev', now(), 'Sim P6', '8a2a1072b59ffff', 'en')
on conflict (id) do update set
  email             = excluded.email,
  email_verified_at = excluded.email_verified_at,
  nickname          = excluded.nickname,
  home_block_id     = excluded.home_block_id,
  locale            = excluded.locale,
  updated_at        = now();

-- Verify ----------------------------------------------------------------------
select u.email, u.email_confirmed_at is not null as confirmed, pu.home_block_id
from auth.users u join public.users pu on pu.id = u.id
where u.email like 'p%-sim@phygtl.dev' order by u.email;
