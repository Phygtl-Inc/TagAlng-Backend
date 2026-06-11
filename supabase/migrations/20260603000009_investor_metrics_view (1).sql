-- ============================================================
-- VIEW: investor_metrics
-- Purpose: Consolidates the 10 R&D Kickoff investor metrics
--          into a single query row.
--          Connected to Looker Studio (TagAlng Main Metrics).
--
-- Owner: Data & AI
-- Date: June 2026
-- Connection: postgres user (service role) — bypasses RLS
--
-- Note on NULL metrics:
--   M02 returns NULL until connection_made is instrumented.
--   M05, M06 return NULL until block_state_changed is instrumented.
--   M10 returns NULL until app_opened is instrumented.
--   NULL = no data to calculate. This is correct and intentional.
-- ============================================================

DROP VIEW IF EXISTS public.investor_metrics;

CREATE VIEW public.investor_metrics AS
SELECT

  -- ── M01 · The Magic Number ──────────────────────────────────────
  -- Recurring weekly fellows per LIVE block
  (
    SELECT ROUND(
      COUNT(DISTINCT ae.session_id)::numeric /
      NULLIF((SELECT COUNT(*) FROM blocks WHERE state = 'live'), 0),
    1)
    FROM analytics_events ae
    JOIN blocks b ON b.id = (ae.properties->>'block_id')
    WHERE ae.event_name = 'event_checkin'
      AND (ae.properties->>'repeat_fellow')::bool = true
      AND ae.created_at >= NOW() - INTERVAL '7 days'
      AND b.state = 'live'
  )                                               AS magic_number,

  -- ── M02 · Connection velocity ───────────────────────────────────
  -- Signup to first connection (median days)
  -- Note: anonymous users will not produce a result here — JOIN with users
  -- only resolves for verified users with a phone_verified_at timestamp.
  (
    SELECT ROUND(CAST(
      PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(DAY FROM (ae.created_at - u.created_at))::int
      ) AS numeric), 1)
    FROM analytics_events ae
    JOIN users u ON u.id = ae.user_id
    WHERE ae.event_name = 'connection_made'
      AND (ae.properties->>'connection_number')::int = 1
  )                                               AS connection_velocity_days,

  -- ── M03 · Friendship compound ───────────────────────────────────
  -- % check-ins with repeat fellow in LIVE blocks
  (
    SELECT ROUND(
      COUNT(CASE WHEN (ae.properties->>'repeat_fellow')::bool = true THEN 1 END)
      * 100.0 / NULLIF(COUNT(*), 0),
    1)
    FROM analytics_events ae
    JOIN blocks b ON b.id = (ae.properties->>'block_id')
    WHERE ae.event_name = 'event_checkin'
      AND b.state = 'live'
  )                                               AS friendship_compound_pct,

  -- ── M04 · Block density at LIVE ─────────────────────────────────
  -- Verified moms per LIVE block
  (
    SELECT ROUND(AVG(verified_count)::numeric, 1)
    FROM (
      SELECT b.id, COUNT(u.id) AS verified_count
      FROM blocks b
      LEFT JOIN users u
        ON u.home_block_id = b.id
       AND u.phone_verified_at IS NOT NULL
      WHERE b.state = 'live'
      GROUP BY b.id
    ) sub
  )                                               AS avg_density_live_blocks,

  -- ── M05 · Block-unlock velocity ─────────────────────────────────
  -- Days from racing to live (median)
  (
    SELECT ROUND(CAST(
      PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY (properties->>'days_from_racing')::int
      ) AS numeric), 1)
    FROM analytics_events
    WHERE event_name = 'block_state_changed'
      AND properties->>'new_state' = 'live'
  )                                               AS unlock_velocity_days,

  -- ── M06 · Cluster activation curve ─────────────────────────────
  -- Days to first 3 LIVE blocks per cluster
  (
    SELECT ROUND(AVG(days_to_3_live)::numeric, 1)
    FROM (
      SELECT
        properties->>'cluster_id'            AS cluster_id,
        (properties->>'days_from_open')::int AS days_to_3_live
      FROM (
        SELECT properties, created_at,
          ROW_NUMBER() OVER (
            PARTITION BY properties->>'cluster_id'
            ORDER BY created_at
          ) AS block_rank
        FROM analytics_events
        WHERE event_name = 'block_state_changed'
          AND properties->>'new_state' = 'live'
      ) ranked
      WHERE block_rank = 3
    ) per_cluster
  )                                               AS cluster_activation_curve,

  -- ── M07 · Combine-filter lift ───────────────────────────────────
  -- Multi-cohort vs single-cohort engagement ratio
  (
    WITH user_cohort_counts AS (
      SELECT user_id, COUNT(DISTINCT concept) AS cohort_count
      FROM user_identity_claims
      WHERE dismissed_at IS NULL
      GROUP BY user_id
    ),
    user_engagement AS (
      SELECT requester_id AS user_id, COUNT(*) AS engagement_score
      FROM event_requests
      WHERE status IN ('attended', 'approved')
      GROUP BY requester_id
    ),
    grouped AS (
      SELECT
        CASE WHEN ucc.cohort_count >= 2 THEN 'multi' ELSE 'single' END AS group_type,
        COALESCE(ue.engagement_score, 0) AS engagement
      FROM user_cohort_counts ucc
      LEFT JOIN user_engagement ue ON ue.user_id = ucc.user_id
    )
    SELECT ROUND(
      MAX(CASE WHEN group_type = 'multi' THEN avg_eng END) /
      NULLIF(MAX(CASE WHEN group_type = 'single' THEN avg_eng END), 0),
    2)
    FROM (
      SELECT group_type, AVG(engagement) AS avg_eng
      FROM grouped
      GROUP BY group_type
    ) sub
  )                                               AS combine_filter_lift,

  -- ── M08 · Identity-claim accuracy ──────────────────────────────
  -- % claims not dismissed/edited after 7 days
  (
    SELECT ROUND(
      COUNT(CASE
        WHEN dismissed_at IS NULL
         AND updated_at = created_at
        THEN 1 END)
      * 100.0 / NULLIF(COUNT(*), 0),
    1)
    FROM user_identity_claims
    WHERE created_at <= NOW() - INTERVAL '7 days'
  )                                               AS claim_accuracy_pct,

  -- ── M09 · K-factor ──────────────────────────────────────────────
  -- % waitlist signups via referral link (Phase 1)
  -- Full K-factor requires referral_sent event (Phase 2)
  (
    SELECT ROUND(
      COUNT(CASE WHEN inbound_ref IS NOT NULL THEN 1 END)
      * 100.0 / NULLIF(COUNT(*), 0),
    1)
    FROM waitlist_signups
  )                                               AS referral_rate_pct,

  -- ── M10 · Safety/trust floor ────────────────────────────────────
  -- Safety reports per 1,000 app sessions
  (
    SELECT ROUND(
      (SELECT COUNT(*) FROM event_reports) * 1000.0 /
      NULLIF(
        (SELECT COUNT(*) FROM analytics_events
         WHERE event_name = 'app_opened'), 0
      ),
    2)
  )                                               AS safety_trust_floor;
