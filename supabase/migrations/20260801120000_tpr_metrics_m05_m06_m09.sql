-- TPR v5.0 Backend Requests — unblock investor metrics M05, M06 (block state
-- history) and M09 (referral attribution). Pure backend; no frontend dependency
-- beyond signup passing a referral code (already in auth signup metadata).
--
-- M05 · Block-Unlock Velocity / M06 · Cluster Activation Curve
--   The blocks table stores only the current state. Create block_state_changes +
--   a trigger so every transition is recorded. No backfill possible — both metrics
--   start accumulating from this migration's deploy.
--
-- M09 · K-Factor
--   Add users.referred_by, attributed at signup from auth metadata. The prior
--   waitlist_signups.inbound_ref proxy measures a different (pre-verification)
--   population and is kept as-is; true K-factor is added as a new view field.

-- ===========================================================================
-- M05 / M06 · block_state_changes + trigger
-- ===========================================================================

create table if not exists public.block_state_changes (
  id bigint generated always as identity primary key,
  block_id text not null references public.blocks (id) on delete cascade,
  old_state public.block_state,
  new_state public.block_state not null,
  changed_at timestamptz not null default now()
);

comment on table public.block_state_changes is
  'Append-only history of block.state transitions (M05/M06). Written by trigger on '
  'public.blocks. old_state is null for the birth row. No backfill — starts at deploy.';

create index if not exists block_state_changes_block_idx
  on public.block_state_changes (block_id, changed_at);
create index if not exists block_state_changes_new_state_idx
  on public.block_state_changes (new_state, changed_at);

-- Internal/analytics table: default-deny for client roles (read via service_role).
alter table public.block_state_changes enable row level security;
drop policy if exists "block_state_changes_no_client" on public.block_state_changes;
create policy "block_state_changes_no_client"
  on public.block_state_changes for all
  to authenticated
  using (false) with check (false);

create or replace function public.log_block_state_change()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'INSERT' then
    -- Birth row: records the state a block was created in (old_state null).
    insert into public.block_state_changes (block_id, old_state, new_state, changed_at)
    values (new.id, null, new.state, coalesce(new.created_at, now()));
  elsif tg_op = 'UPDATE' and new.state is distinct from old.state then
    insert into public.block_state_changes (block_id, old_state, new_state, changed_at)
    values (new.id, old.state, new.state, now());
  end if;
  return new;
end;
$$;

revoke execute on function public.log_block_state_change() from public, anon, authenticated;

drop trigger if exists trg_log_block_state_change on public.blocks;
create trigger trg_log_block_state_change
after insert or update of state on public.blocks
for each row execute function public.log_block_state_change();

-- ===========================================================================
-- M09 · users.referred_by + signup attribution
-- ===========================================================================

alter table public.users
  add column if not exists referred_by uuid references public.users (id) on delete set null;

alter table public.users
  drop constraint if exists users_referred_by_not_self;
alter table public.users
  add constraint users_referred_by_not_self
    check (referred_by is null or referred_by <> id);

create index if not exists users_referred_by_idx
  on public.users (referred_by)
  where referred_by is not null;

comment on column public.users.referred_by is
  'Inviter user id, attributed at signup from auth metadata (M09 K-Factor). '
  'Null when no referral code present or the code did not resolve to an existing user.';

-- Extend the new-user trigger to attribute referrals. The inviter id arrives in
-- auth signup metadata (referred_by, or legacy referral_code). Non-uuid codes are
-- ignored until the Phase-2 referrals table exists; self-referral is rejected; and
-- the referrer must already exist (FK would otherwise abort the whole signup).
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_phone text;
  v_email text;
  v_ref_raw text;
  v_ref uuid;
begin
  v_phone := nullif(trim(coalesce(new.phone::text, '')), '');
  v_email := nullif(trim(lower(coalesce(new.email::text, ''))), '');

  v_ref_raw := coalesce(
    new.raw_user_meta_data->>'referred_by',
    new.raw_user_meta_data->>'referral_code'
  );
  begin
    v_ref := nullif(trim(v_ref_raw), '')::uuid;
  exception when others then
    v_ref := null;
  end;
  if v_ref is not null
     and (v_ref = new.id
          or not exists (select 1 from public.users u where u.id = v_ref)) then
    v_ref := null;
  end if;

  insert into public.users (id, phone, email, referred_by)
  values (new.id, v_phone, v_email, v_ref)
  on conflict (id) do update
    set updated_at = now(),
        -- Keep an existing attribution; only fill it if still null.
        referred_by = coalesce(users.referred_by, excluded.referred_by);

  return new;
end;
$$;

comment on function public.handle_new_user() is
  'After auth.users insert: ensure public.users row, attribute referred_by from '
  'signup metadata. Works for phone OTP, email OTP, and email/password admins.';

revoke execute on function public.handle_new_user() from public, anon, authenticated;

-- ===========================================================================
-- investor_metrics view — repoint M05, M06 at block_state_changes and add
-- k_factor (M09). M01/M08 carry forward from 20260731120000; all other metrics
-- reproduced verbatim (a view cannot be partially altered).
-- ===========================================================================

DROP VIEW IF EXISTS public.investor_metrics;

CREATE VIEW public.investor_metrics AS
SELECT

  -- ── M01 · The Magic Number (fast path) ──────────────────────────
  -- Recurring weekly fellows per LIVE block: a pair are recurring fellows when they
  -- share >= 2 confirmed event_ids in the same LIVE block within a 7-day window.
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
  -- Median days a block spent racing before going live, from block_state_changes.
  (
    SELECT ROUND(CAST(
      PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (live_at - racing_at)) / 86400.0
      ) AS numeric), 1)
    FROM (
      SELECT block_id,
             MIN(changed_at) FILTER (WHERE new_state = 'racing') AS racing_at,
             MIN(changed_at) FILTER (WHERE new_state = 'live')   AS live_at
      FROM block_state_changes
      GROUP BY block_id
    ) per_block
    WHERE racing_at IS NOT NULL
      AND live_at IS NOT NULL
      AND live_at >= racing_at
  )                                               AS unlock_velocity_days,

  -- ── M06 · Cluster activation curve ─────────────────────────────
  -- Avg days from cluster inception (first block created) to its 3rd distinct
  -- block going LIVE, from block_state_changes.
  (
    SELECT ROUND(AVG(
      EXTRACT(EPOCH FROM (tl.third_live_at - co.opened_at)) / 86400.0
    )::numeric, 1)
    FROM (
      SELECT cluster_id, MIN(created_at) AS opened_at
      FROM blocks
      GROUP BY cluster_id
    ) co
    JOIN (
      SELECT cluster_id, block_first_live AS third_live_at
      FROM (
        SELECT cluster_id, block_first_live,
               ROW_NUMBER() OVER (
                 PARTITION BY cluster_id ORDER BY block_first_live
               ) AS live_rank
        FROM (
          SELECT bk.cluster_id, bsc.block_id, MIN(bsc.changed_at) AS block_first_live
          FROM block_state_changes bsc
          JOIN blocks bk ON bk.id = bsc.block_id
          WHERE bsc.new_state = 'live'
          GROUP BY bk.cluster_id, bsc.block_id
        ) first_live_per_block
      ) ranked
      WHERE live_rank = 3
    ) tl ON tl.cluster_id = co.cluster_id
  )                                               AS cluster_activation_curve,

  -- ── M07 · Combine-filter lift ───────────────────────────────────
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
  -- % of claims (older than 7 days) the user never edited or dismissed, keyed off
  -- last_user_edit_at (only user-initiated edit RPCs write it).
  (
    SELECT ROUND(
      COUNT(CASE WHEN last_user_edit_at IS NULL THEN 1 END)
      * 100.0 / NULLIF(COUNT(*), 0),
    1)
    FROM user_identity_claims
    WHERE created_at <= NOW() - INTERVAL '7 days'
  )                                               AS claim_accuracy_pct,

  -- ── M09 (proxy) · waitlist referral rate ───────────────────────
  -- Pre-verification proxy kept for continuity; NOT K-factor (see k_factor).
  (
    SELECT ROUND(
      COUNT(CASE WHEN inbound_ref IS NOT NULL THEN 1 END)
      * 100.0 / NULLIF(COUNT(*), 0),
    1)
    FROM waitlist_signups
  )                                               AS referral_rate_pct,

  -- ── M09 · K-Factor ──────────────────────────────────────────────
  -- Referral density: verified users who were referred, per verified user.
  (
    SELECT ROUND(
      COUNT(*) FILTER (WHERE referred_by IS NOT NULL AND phone_verified_at IS NOT NULL)::numeric
      / NULLIF(COUNT(*) FILTER (WHERE phone_verified_at IS NOT NULL), 0),
    2)
    FROM users
  )                                               AS k_factor,

  -- ── M10 · Safety/trust floor ────────────────────────────────────
  (
    SELECT ROUND(
      (SELECT COUNT(*) FROM event_reports) * 1000.0 /
      NULLIF(
        (SELECT COUNT(*) FROM analytics_events
         WHERE event_name = 'app_opened'), 0
      ),
    2)
  )                                               AS safety_trust_floor;
