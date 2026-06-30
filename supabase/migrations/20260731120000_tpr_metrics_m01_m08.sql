-- TPR v5.0 Backend Requests — unblock investor metrics M01 (fast path) and M08 (exact).
--
-- M01 · The Magic Number (fast path)
--   Add irl_confirmations.event_id so a confirmed "we met IRL" can be linked to the
--   event that originated it. Pairs sharing >= 2 confirmed event_ids within the same
--   block in a 7-day window then satisfy the recurring-fellows definition WITHOUT full
--   check-in instrumentation.
--
-- M08 · Identity-Claim Accuracy (exact)
--   Add user_identity_claims.last_user_edit_at, populated ONLY by the user-initiated
--   edit RPCs below. AI re-upserts (claims_persist.upsert_claims / replace_all_claims)
--   never touch this column, so it is a clean signal — unlike updated_at, which the
--   AI re-upsert path pollutes on every turn.

-- ---------------------------------------------------------------------------
-- M01 · irl_confirmations.event_id
-- ---------------------------------------------------------------------------

alter table public.irl_confirmations
  add column if not exists event_id uuid references public.events (id) on delete set null;

comment on column public.irl_confirmations.event_id is
  'Event that originated this IRL confirmation, when known (M01 fast path). '
  'Nullable: spontaneous confirmations not tied to an event leave it null.';

create index if not exists irl_confirmations_event_id_idx
  on public.irl_confirmations (event_id)
  where event_id is not null;

-- Populate event_id on the manual mutual-confirm path. Adding p_event_id changes the
-- signature, so this is a NEW function object — the prior confirm_irl_met(uuid) must be
-- dropped or it lingers as an overload and PostgREST resolves single-arg calls to the
-- stale (non-event-aware) version. With p_event_id defaulted, the existing 1-arg call
-- site (PostgREST passes only p_other_user_id) still resolves to this function.
drop function if exists public.confirm_irl_met(uuid);

create or replace function public.confirm_irl_met(
  p_other_user_id uuid,
  p_event_id uuid default null
)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_tier public.relationship_tier;
  v_both boolean;
begin
  perform public._require_verified_neighbor_comms();

  if p_other_user_id is null or p_other_user_id = v_me then
    raise exception 'invalid_other_user' using errcode = 'P0001';
  end if;

  v_tier := public.get_relationship_tier(p_other_user_id);
  if v_tier <> 'direct' then
    raise exception 'must_be_direct_to_confirm_irl' using errcode = 'P0001';
  end if;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_other_user_id);

  insert into public.irl_confirmations (user_low, user_high, confirmed_by, event_id)
  values (v_low, v_high, v_me, p_event_id)
  on conflict (user_low, user_high, confirmed_by) do update
    -- Keep the first non-null originating event; never blank an existing one.
    set event_id = coalesce(irl_confirmations.event_id, excluded.event_id);

  select count(distinct confirmed_by) >= 2 into v_both
  from public.irl_confirmations
  where user_low = v_low and user_high = v_high;

  if v_both then
    perform public._promote_pair_to_irl(v_me, p_other_user_id, p_event_id);
  end if;

  return public.get_relationship_tier(p_other_user_id);
end;
$$;

-- Re-grant: CREATE OR REPLACE with a new signature is a new function object.
revoke all on function public.confirm_irl_met(uuid, uuid) from public, anon;
grant execute on function public.confirm_irl_met(uuid, uuid) to authenticated;

-- ---------------------------------------------------------------------------
-- M08 · user_identity_claims.last_user_edit_at
-- ---------------------------------------------------------------------------

alter table public.user_identity_claims
  add column if not exists last_user_edit_at timestamptz;

comment on column public.user_identity_claims.last_user_edit_at is
  'Set only by user-initiated edit RPCs (label / disclosure / dismiss). '
  'AI re-upserts never write it — clean signal for M08 Identity-Claim Accuracy, '
  'unlike updated_at which the AI re-upsert path pollutes.';

create or replace function public.update_identity_claim_label(
  p_claim_id uuid,
  p_label text,
  p_synonyms text[] default null
)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.user_identity_claims
  set label = p_label,
      synonyms = coalesce(p_synonyms, synonyms),
      updated_at = now(),
      last_user_edit_at = now()
  where id = p_claim_id
    and user_id = auth.uid()
    and dismissed_at is null;
$$;

create or replace function public.update_identity_claim_disclosure(
  p_claim_id uuid,
  p_disclosure public.claim_disclosure
)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.user_identity_claims
  set disclosure = p_disclosure,
      updated_at = now(),
      last_user_edit_at = now()
  where id = p_claim_id
    and user_id = auth.uid()
    and dismissed_at is null;
$$;

create or replace function public.dismiss_identity_claim(p_claim_id uuid)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.user_identity_claims
  set dismissed_at = now(),
      updated_at = now(),
      last_user_edit_at = now()
  where id = p_claim_id
    and user_id = auth.uid()
    and dismissed_at is null;
$$;

-- ---------------------------------------------------------------------------
-- investor_metrics view — repoint M01 (fast path) and M08 (exact) at the new
-- columns. All other metrics reproduced verbatim from
-- 20260603000009_investor_metrics_view (1).sql (a view cannot be partially
-- altered; the whole SELECT is restated).
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS public.investor_metrics;

CREATE VIEW public.investor_metrics AS
SELECT

  -- ── M01 · The Magic Number (fast path) ──────────────────────────
  -- Recurring weekly fellows per LIVE block. Fast path (TPR v5.0): a pair are
  -- recurring fellows when they share >= 2 confirmed event_ids in the same LIVE
  -- block within a 7-day window. confirmed_at is the confirmation time (encounter
  -- time is not stored); event_id links the confirmation to its originating event.
  (
    SELECT ROUND(
      COUNT(*)::numeric /
      NULLIF((SELECT COUNT(*) FROM blocks WHERE state = 'live'), 0),
    1)
    FROM (
      SELECT ic.user_low, ic.user_high, e.block_id
      FROM irl_confirmations ic
      JOIN events e ON e.id = ic.event_id
      JOIN blocks b ON b.id = e.block_id AND b.state = 'live'
      WHERE ic.event_id IS NOT NULL
        AND ic.confirmed_at >= NOW() - INTERVAL '7 days'
      GROUP BY ic.user_low, ic.user_high, e.block_id
      HAVING COUNT(DISTINCT ic.event_id) >= 2
    ) recurring_pairs
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

  -- ── M08 · Identity-claim accuracy (exact) ──────────────────────
  -- % of claims (older than 7 days) the user never edited or dismissed.
  -- Exact (TPR v5.0): keys off last_user_edit_at, which only user-initiated edit
  -- RPCs write — replacing the updated_at proxy that AI re-upserts polluted.
  (
    SELECT ROUND(
      COUNT(CASE WHEN last_user_edit_at IS NULL THEN 1 END)
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
