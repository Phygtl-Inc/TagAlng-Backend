-- ============================================================
-- TagAlng · Test Seed Data for Investor Dashboard
-- Environment: tagalng-dev (Supabase)
-- File: scripts/seed_test_data.sql
-- Owner: Data & AI
--
-- Purpose: Populates analytics_events with test data so the
--          investor_metrics VIEW returns meaningful numbers
--          before Day Zero instrumentation is complete.
--
-- Metrics covered by this seed:
--   M01 · The Magic Number       (event_checkin)
--   M03 · Friendship compound    (event_checkin)
--   M05 · Block-unlock velocity  (block_state_changed)
--   M09 · K-factor               (waitlist_signups)
--
-- Metrics NOT covered (intentionally return NULL or real data):
--   M02 · Connection velocity    NULL — requires real user_ids (JOIN users)
--   M04 · Block density at LIVE  Real data — uses users table directly
--   M06 · Cluster activation     NULL — requires days_from_open + cluster_id
--                                        (engineering event pending, see TPR P2)
--   M07 · Combine-filter lift    Real data — uses user_identity_claims + event_requests
--   M08 · Identity-claim acc.    Real data — uses user_identity_claims
--   M10 · Safety/trust floor     NULL — requires app_opened event (TPR P4)
--   M11–M13 · Marketplace        0 — populates from Day Zero via Lana
--
-- Cleanup:
--   DELETE FROM analytics_events WHERE properties->>'is_test' = 'true';
-- ============================================================


-- ── STEP 1: Ensure LIVE blocks exist ─────────────────────────
INSERT INTO blocks (id, cluster_id, state, display_name)
VALUES
  ('lake-nona-block-01', 'lake-nona', 'live',     'Moss Park · Block 01'),
  ('lake-nona-block-02', 'lake-nona', 'live',     'Foxtail · Block 02'),
  ('lake-nona-block-03', 'lake-nona', 'racing',   'Holy Family · Block 03'),
  ('lake-nona-block-04', 'lake-nona', 'waitlist', 'Solis · Block 04')
ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state;


-- ── M01 + M03 · event_checkin ────────────────────────────────
-- Populates: The Magic Number (M01) and Friendship compound (M03)
-- Both metrics JOIN blocks on block_id and filter state = 'live'
-- repeat_fellow = true contributes to both metrics
-- repeat_fellow = false contributes only to the M03 denominator

INSERT INTO analytics_events (event_name, session_id, properties, created_at)
VALUES
  -- LIVE block 01 · repeat fellows (count toward M01)
  ('event_checkin','test-s-001','{"is_test":true,"block_id":"lake-nona-block-01","repeat_fellow":true,"fellow_count":3,"irl_match_triggered":false}', NOW() - INTERVAL '1 day'),
  ('event_checkin','test-s-002','{"is_test":true,"block_id":"lake-nona-block-01","repeat_fellow":true,"fellow_count":2,"irl_match_triggered":true}',  NOW() - INTERVAL '2 days'),
  ('event_checkin','test-s-003','{"is_test":true,"block_id":"lake-nona-block-01","repeat_fellow":true,"fellow_count":4,"irl_match_triggered":false}', NOW() - INTERVAL '3 days'),
  ('event_checkin','test-s-008','{"is_test":true,"block_id":"lake-nona-block-01","repeat_fellow":true,"fellow_count":2,"irl_match_triggered":false}', NOW() - INTERVAL '6 days'),
  ('event_checkin','test-s-010','{"is_test":true,"block_id":"lake-nona-block-01","repeat_fellow":true,"fellow_count":5,"irl_match_triggered":true}',  NOW() - INTERVAL '1 day'),

  -- LIVE block 01 · first-time fellows (count toward M03 denominator only)
  ('event_checkin','test-s-006','{"is_test":true,"block_id":"lake-nona-block-01","repeat_fellow":false,"fellow_count":2,"irl_match_triggered":false}',NOW() - INTERVAL '5 days'),

  -- LIVE block 02 · repeat fellows (count toward M01)
  ('event_checkin','test-s-004','{"is_test":true,"block_id":"lake-nona-block-02","repeat_fellow":true,"fellow_count":2,"irl_match_triggered":true}',  NOW() - INTERVAL '1 day'),
  ('event_checkin','test-s-007','{"is_test":true,"block_id":"lake-nona-block-02","repeat_fellow":true,"fellow_count":3,"irl_match_triggered":true}',  NOW() - INTERVAL '2 days'),

  -- LIVE block 02 · first-time fellows (count toward M03 denominator only)
  ('event_checkin','test-s-005','{"is_test":true,"block_id":"lake-nona-block-02","repeat_fellow":false,"fellow_count":1,"irl_match_triggered":false}',NOW() - INTERVAL '4 days'),
  ('event_checkin','test-s-009','{"is_test":true,"block_id":"lake-nona-block-02","repeat_fellow":false,"fellow_count":1,"irl_match_triggered":false}',NOW() - INTERVAL '3 days');


-- ── M05 · block_state_changed ────────────────────────────────
-- Populates: Block-unlock velocity (M05)
-- days_from_racing = days the block spent in racing before going live
-- NOTE: days_from_open and cluster_id are NOT included here —
--       they are required for M06 (Cluster activation curve) but
--       that engineering event is pending. See TPR Section 2, P2.

INSERT INTO analytics_events (event_name, session_id, properties, created_at)
VALUES
  -- block 01: racing → live after 8 days
  ('block_state_changed','test-b-001',
   '{"is_test":true,"block_id":"lake-nona-block-01","old_state":"racing","new_state":"live","verified_mom_count":22,"days_from_racing":8}',
   NOW() - INTERVAL '10 days'),

  -- block 02: racing → live after 12 days
  ('block_state_changed','test-b-002',
   '{"is_test":true,"block_id":"lake-nona-block-02","old_state":"racing","new_state":"live","verified_mom_count":28,"days_from_racing":12}',
   NOW() - INTERVAL '7 days'),

  -- earlier transitions (waitlist → racing) — contribute to audit trail
  ('block_state_changed','test-b-003',
   '{"is_test":true,"block_id":"lake-nona-block-01","old_state":"waitlist","new_state":"racing","verified_mom_count":15,"days_from_racing":0}',
   NOW() - INTERVAL '18 days'),
  ('block_state_changed','test-b-004',
   '{"is_test":true,"block_id":"lake-nona-block-02","old_state":"waitlist","new_state":"racing","verified_mom_count":12,"days_from_racing":0}',
   NOW() - INTERVAL '19 days'),
  ('block_state_changed','test-b-005',
   '{"is_test":true,"block_id":"lake-nona-block-03","old_state":"waitlist","new_state":"racing","verified_mom_count":18,"days_from_racing":0}',
   NOW() - INTERVAL '5 days');


-- ── M09 · K-factor ───────────────────────────────────────────
-- Populates: K-factor (M09) via waitlist_signups.inbound_ref
-- 5 of 8 signups have inbound_ref → referral rate = 62.5%

INSERT INTO waitlist_signups
  (phone, city, declared_cohorts, candidate_block_id, inbound_ref, recaptcha_verified)
VALUES
  (NULL,'Lake Nona',ARRAY['parents','runner'],   'lake-nona-block-01','reads-post-why-lake-nona', true),
  (NULL,'Lake Nona',ARRAY['faith','parents'],    'lake-nona-block-01','reads-post-morning-run',   true),
  (NULL,'Lake Nona',ARRAY['sports'],             'lake-nona-block-02','reads-post-why-lake-nona', true),
  (NULL,'Lake Nona',ARRAY['parents'],            'lake-nona-block-02', NULL,                      true),
  (NULL,'Lake Nona',ARRAY['newcomer','parents'], 'lake-nona-block-01','reads-post-foxtail-coffee',true),
  (NULL,'Lake Nona',ARRAY['creative'],           'lake-nona-block-03', NULL,                      true),
  (NULL,'Lake Nona',ARRAY['runner','sports'],    'lake-nona-block-02','reads-post-morning-run',   true),
  (NULL,'Lake Nona',ARRAY['sober','parents'],    'lake-nona-block-01', NULL,                      true);


-- ── VERIFICATION ─────────────────────────────────────────────
SELECT
  event_name,
  COUNT(*)     AS count,
  'TEST DATA'  AS data_type
FROM analytics_events
WHERE properties->>'is_test' = 'true'
GROUP BY event_name
ORDER BY event_name;
