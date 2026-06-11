# TPR v3.0 · TagAlng Investor Dashboard — Metrics & Instrumentation

**Status:** Draft · Pending engineering review  
**Owner:** Data & AI  
**Date:** June 2026  
**Scope:** TagAlng · tagalng-dev · Supabase · investor_metrics VIEW
**Frontend stack:** Next.js 15 App Router · Lana frontend (frontend events: Abdullah · backend events: Asjid)  

---

## 1. EXECUTIVE SUMMARY

The investor dashboard surfaces 10 investor metrics. This table shows each metric, its definition, current data status, and the engineering event required — if any — before the metric shows real data.

| # | Metric | Definition | Data today | Engineering event needed |
|---|---|---|---|---|
| 01 | The Magic Number | Recurring weekly fellows per LIVE block | Test data | `event_checkin` with `repeat_fellow`, `block_id` |
| 02 | Connection velocity | Signup → first connection (median days) | NULL | `connection_made` with `connection_number` |
| 03 | Friendship compound | % check-ins with repeat fellow in LIVE blocks | Test data | `event_checkin` with `repeat_fellow`, `block_id` |
| 04 | Block density at LIVE | Verified moms per LIVE block | Real data | None |
| 05 | Block-unlock velocity | Days from racing → live (median) | NULL | `block_state_changed` with `days_from_racing` |
| 06 | Cluster activation curve | Days to first 3 LIVE blocks per cluster | NULL | `block_state_changed` with `days_from_open`, `cluster_id` |
| 07 | Combine-filter lift | Multi-cohort vs single-cohort engagement ratio | Real data | None |
| 08 | Identity-claim accuracy | % claims not dismissed/edited after 7 days | Real data | None |
| 09 | K-factor | % waitlist signups via referral link | Real data | None (Phase 2 for full K-factor) |
| 10 | Safety/trust floor | Safety reports per 1,000 app sessions | NULL | `app_opened` with `platform`, `app_version` |

---

## 2. ENGINEERING EVENTS REQUIRED

**Important:** The old PWA (`app.tagalng.com`) is frozen — no new features. All frontend instrumentation must be implemented in the new Next.js 15 Lana app.


The following events must be instrumented before the corresponding metrics show real data. Priority order is based on number of metrics unblocked.

### P1 — `event_checkin` · unblocks M01 and M03

Fired from the app on every confirmed check-in.

**Required fields in `properties`:**

| Field | Type | Description |
|---|---|---|
| `repeat_fellow` | bool | True if at least one fellow attended a previous event with this user |
| `fellow_count` | int | Number of fellows present at this check-in |
| `irl_match_triggered` | bool | True if this check-in unlocked a real name reveal |
| `block_id` | text | H3 block ID — must match a record in the `blocks` table |

**Surface:** App (Next.js 15 · Lana frontend) · fired on check-in confirmation — implementer: Abdullah

---

### P2 — `block_state_changed` · unblocks M05 and M06

Fired from the backend on every `blocks.state` transition.

**Required fields in `properties`:**

| Field | Type | Description | Required for |
|---|---|---|---|
| `block_id` | text | H3 block ID | M05 and M06 |
| `old_state` | text | Previous state (waitlist/racing/live/day_zero) | M05 and M06 |
| `new_state` | text | New state | M05 and M06 |
| `verified_mom_count` | int | Users with phone_verified_at + home_block_id at time of change | M05 |
| `days_from_racing` | int | Days since block entered racing state (0 if not applicable) | M05 |
| `days_from_open` | int | Days since cluster was opened | M06 |
| `cluster_id` | text | Cluster identifier (e.g. lake-nona) | M06 |

**Surface:** Backend · fired on every write to `blocks.state`

---

### P3 — `connection_made` · unblocks M02

Fired from the backend when two moms complete a mutual check-in (first real-name unlock).

**Required fields in `properties`:**

| Field | Type | Description |
|---|---|---|
| `connection_number` | int | How many connections this user has total (filter to 1 for first connection) |
| `block_id` | text | H3 block ID where the connection was made |
| `event_id` | uuid | Activity that generated the connection |

**Surface:** Backend · fired automatically on mutual check-in confirmation  
**Note:** The `user_id` column on `analytics_events` must be populated — the VIEW JOINs with the `users` table to calculate days since signup.

---

### P4 — `app_opened` · unblocks M10

Fired every time a user opens the app.

**Required fields in `properties`:**

| Field | Type | Description |
|---|---|---|
| `platform` | text | `pwa` / `ios` / `android` |
| `app_version` | text | App version string |

**Surface:** App · fired on app launch  
**Note:** Firebase will handle this automatically once integrated (Phase 2). For Day Zero, a manual `logEvent` call is needed.

---

## 3. DASHBOARD LAYOUT

```
Row 1: The Magic Number · Connection velocity · Friendship compound
       Block density at LIVE · Block-unlock velocity

Row 2: Cluster activation curve · Combine-filter lift
       Identity-claim accuracy · Safety/trust floor · K-factor


```

---

## 4. ARCHITECTURE

All metrics are consolidated in the `investor_metrics` VIEW in Supabase. Looker Studio queries this VIEW directly via PostgreSQL connection.

```
App / Backend / Lana
     |
     v
analytics_events (Supabase)     inquiry_signals (Supabase · Lana)
  event_name: text                 category, urgency, sentiment
  properties: jsonb                opt_in_followup (bool)
  user_id: uuid                    status (text)
  created_at: timestamptz
     |                                  |
     +----------------------------------+
     v
investor_metrics VIEW (Supabase)
     v
Looker Studio → TagAlng Main Metrics
```

**NULL vs 0:** The VIEW returns NULL — not 0 — for metrics where the required event has not been instrumented. NULL means "no data to calculate". 0 means "calculated and the result is zero". These are different states and must not be confused.

---

## 5. METRIC DEFINITIONS — INVESTOR METRICS (M01–M10)

---

### M01 · The Magic Number

**Definition:** Unique check-ins in the last 7 days where `repeat_fellow = true`, divided by the number of currently LIVE blocks.

**Why divided by LIVE blocks:** The metric measures density of recurring fellowships across the product's active geography — not just total volume. A product with 6 recurring fellowships in 2 LIVE blocks (3.0) is healthier than 6 recurring fellowships in 6 LIVE blocks (1.0).

**Why filtered to last 7 days:** Measures recency, not historical accumulation. Fellowships that happened weeks ago do not contribute to the current week's social vitality.

```sql
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
```

**Engineering event:** `event_checkin` with `repeat_fellow` (bool) and `block_id` (text) · P1

---

### M02 · Connection velocity

**Definition:** Median days between `users.created_at` and first `connection_made` event, filtered to `connection_number = 1`.

**Why JOIN with users:** Days since signup is calculated from the actual signup timestamp in the `users` table — not from a JSONB field. This removes backend dependency and ensures accuracy regardless of when the event is fired.

**Why filter to connection_number = 1:** Only first connections are meaningful for this metric. Including subsequent connections would lower the median artificially and not reflect the actual onboarding-to-connection journey.

```sql
SELECT ROUND(CAST(
  PERCENTILE_CONT(0.5) WITHIN GROUP (
    ORDER BY EXTRACT(DAY FROM (ae.created_at - u.created_at))::int
  ) AS numeric), 1)
FROM analytics_events ae
JOIN users u ON u.id = ae.user_id
WHERE ae.event_name = 'connection_made'
  AND (ae.properties->>'connection_number')::int = 1
```

**Engineering event:** `connection_made` with `connection_number` (int) · P3  
**Note:** `user_id` on `analytics_events` must be populated — the VIEW JOINs with `users`.  
**Current status:** Returns NULL — no test data (real connections will populate from Day Zero)

---

### M03 · Friendship compound

**Definition:** % of check-ins in LIVE blocks where `repeat_fellow = true`.

**Why restricted to LIVE blocks:** Same rationale as M01 — only check-ins in active, production blocks reflect the product working as intended.

**Why not restricted to last 7 days:** Friendship compound is a cumulative product health signal. Unlike the Magic Number (which is a weekly pulse), compound measures the overall % of check-ins that are repeat encounters since the product launched.

```sql
SELECT ROUND(
  COUNT(CASE WHEN (ae.properties->>'repeat_fellow')::bool = true THEN 1 END)
  * 100.0 / NULLIF(COUNT(*), 0),
1)
FROM analytics_events ae
JOIN blocks b ON b.id = (ae.properties->>'block_id')
WHERE ae.event_name = 'event_checkin'
  AND b.state = 'live'
```

**Engineering event:** `event_checkin` with `repeat_fellow` (bool) and `block_id` (text) · P1

---

### M04 · Block density at LIVE

**Definition:** Average number of verified users per LIVE block, where verified = `phone_verified_at IS NOT NULL` and `home_block_id` is set.

**Why uses `users` table and not `block_waitlist_counts`:** Waitlist counts measure acquisition intent. This metric measures product adoption — how many moms completed onboarding (phone verified + home block set) per LIVE block. These are different signals.

**Why `phone_verified_at IS NOT NULL`:** Phone verification is the trust gate. An unverified user is not a "verified mom" for the purposes of this metric.

```sql
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
```

**Engineering event:** None  
**Current status:** Real data

---

### M05 · Block-unlock velocity

**Definition:** Median days a block spent in `racing` state before transitioning to `live`.

**Why median and not average:** Averages are distorted by outlier blocks. Median gives the typical experience.

**Returns NULL until:** `block_state_changed` is instrumented with `days_from_racing`.

```sql
SELECT ROUND(CAST(
  PERCENTILE_CONT(0.5) WITHIN GROUP (
    ORDER BY (properties->>'days_from_racing')::int
  ) AS numeric), 1)
FROM analytics_events
WHERE event_name = 'block_state_changed'
  AND properties->>'new_state' = 'live'
```

**Engineering event:** `block_state_changed` with `days_from_racing` (int) · P2

---

### M06 · Cluster activation curve

**Definition:** Average days for a cluster to reach its first 3 simultaneously LIVE blocks.

**Why 3 blocks:** The R&D Kickoff defines this threshold as the point where network effects within a cluster begin to emerge. One or two LIVE blocks are isolated. Three LIVE blocks create the first inter-block discovery surface.

**Why average across clusters:** Generic — not hardcoded to Lake Nona. As the product expands to Winter Garden, Windermere, etc., each cluster contributes to the average.

**Returns NULL until:** `block_state_changed` is instrumented with `days_from_open` (int) and `cluster_id` (text).

```sql
SELECT ROUND(AVG(days_to_3_live)::numeric, 1)
FROM (
  SELECT
    properties->>'cluster_id' AS cluster_id,
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
```

**Engineering event:** `block_state_changed` with `days_from_open` (int) and `cluster_id` (text) · P2

---

### M07 · Combine-filter lift

**Definition:** Ratio of average engagement (RSVPs + attended events) for users with 2+ identity claims vs users with a single claim.

**Why a ratio and not a percentage:** The R&D definition is "2-4 cohort users vs single (engagement)" — a comparative metric, not a distribution. A ratio of 2.0 means multi-cohort users engage twice as much, which is the insight investors care about.

**Why `user_identity_claims` and not `scene_activated`:** Identity claims represent who the mom actually is — multi-dimensional identity. `scene_activated` events are session-level signals. The lift is about user behavior over time, not session behavior.

**Why `updated_at = created_at` is not applied here:** This metric uses claim count, not claim accuracy. Whether a claim was edited doesn't change the user's cohort count for engagement comparison purposes.

```sql
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
```

**Engineering event:** None — uses existing `user_identity_claims` and `event_requests`  
**Current status:** Real data

---

### M08 · Identity-claim accuracy

**Definition:** % of identity claims that are at least 7 days old and have neither been dismissed nor modified since extraction.

**Why `created_at <= NOW() - INTERVAL '7 days'`:** Claims less than 7 days old are in their evaluation window — the mom is still deciding whether to keep them. Including them would penalize accuracy for the wrong reason.

**Why `updated_at = created_at` as edit proxy:** Without a dedicated `edited_at` field, a change in `updated_at` relative to `created_at` is the only signal that a claim was modified. This is an approximation. A dedicated `edited_at` column would improve precision.

```sql
SELECT ROUND(
  COUNT(CASE
    WHEN dismissed_at IS NULL
     AND updated_at = created_at
    THEN 1 END)
  * 100.0 / NULLIF(COUNT(*), 0),
1)
FROM user_identity_claims
WHERE created_at <= NOW() - INTERVAL '7 days'
```

**Engineering event:** None — uses existing `user_identity_claims`  
**Current status:** Real data

---

### M09 · K-factor

**Definition (Phase 1):** % of waitlist signups that arrived via a referral link (`inbound_ref IS NOT NULL`).

**Important limitation:** This is not the true K-factor. True K-factor = invites sent per active user × referral conversion rate. This metric captures only the conversion component. Invite tracking requires a `referral_sent` event and personal referral link feature — Phase 2.

```sql
SELECT ROUND(
  COUNT(CASE WHEN inbound_ref IS NOT NULL THEN 1 END)
  * 100.0 / NULLIF(COUNT(*), 0),
1)
FROM waitlist_signups
```

**Engineering event:** None for Phase 1. `referral_sent` event required for full K-factor (Phase 2)  
**Current status:** Real data (partial)

---

### M10 · Safety/trust floor

**Definition:** Safety reports filed per 1,000 app sessions. Lower is better. A session is counted as one `app_opened` event.

**Why per 1,000 sessions and not absolute count:** An absolute count has no context. 5 reports in 100 sessions is critical. 5 reports in 10,000 sessions is negligible. Normalizing to 1,000 sessions makes the metric comparable over time and across products.

**Returns NULL until:** `app_opened` is instrumented.

```sql
SELECT ROUND(
  (SELECT COUNT(*) FROM event_reports) * 1000.0 /
  NULLIF(
    (SELECT COUNT(*) FROM analytics_events WHERE event_name = 'app_opened'),
    0
  ),
2)
```

**Engineering event:** `app_opened` with `platform` (text) and `app_version` (text) · P4  
**Note:** Firebase will handle this automatically once integrated (Phase 2).

---

## 6. TEST DATA CLEANUP

All test records in `analytics_events` are tagged with `"is_test": true` in properties. Remove before production:

```sql
DELETE FROM analytics_events
WHERE properties->>'is_test' = 'true';
```

---

*TagAlng · Phygtl, Inc. · Internal document · Not for external distribution*
