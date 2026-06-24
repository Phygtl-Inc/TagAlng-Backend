# TPR v5.0 — Investor Metrics
**Product:** TagAlng · Lana
**Owner:** Data & AI
**Date:** June 2026
**Status:** Active

---

> **Core Principle:** A metric must calculate exactly what its definition states. If the required data does not exist, the metric is marked NOT CALCULABLE or NOT IMPLEMENTED. Proxies are only used when technically unavoidable and must be explicitly declared alongside their impact on the original definition.

---

## 1. Executive Summary

| # | Metric | v4.1 Status | Backend Answer | TPR v5 Status | Calculation Method |
|---|---|---|---|---|---|
| M01 | The Magic Number | NOT CALCULABLE | Calculable (near-zero) | ❌ NOT CALCULABLE | No faithful path. `irl_confirmations` and `relationship_tier_events` exist but do not capture weekly co-encounters per block. `attended` is never written. Fast path available — see M01 definition. |
| M02 | Connection Velocity | NOT CALCULABLE | CALCULABLE | ✅ CALCULABLE — exact | `relationship_tier_events`: `MIN(created_at)` per pair where `to_tier IN ('acquaintance','direct','irl_peer')`, joined to `users.created_at`. No proxy needed. |
| M03 | Friendship Compound | NOT CALCULABLE | NOT CALCULABLE | ❌ NOT CALCULABLE | `event_requests.status = 'attended'` is never written. `approved` ≠ attended. No co-attendance signal exists today. |
| M04 | Block Density at LIVE | CALCULABLE | CALCULABLE ✓ | ✅ CALCULABLE — exact | `users` + `blocks`: verified users (`phone_verified_at IS NOT NULL`) per `blocks.state = 'live'`. No proxy needed. |
| M05 | Block-Unlock Velocity | NOT IMPLEMENTED | NOT IMPLEMENTED | ⬜ NOT IMPLEMENTED | Requires `block_state_changes` table + trigger. No backfill possible. |
| M06 | Cluster Activation Curve | NOT IMPLEMENTED | NOT IMPLEMENTED | ⬜ NOT IMPLEMENTED | Same dependency as M05. Once `block_state_changes` exists, derivable via `cluster_id`. |
| M07 | Combine-Filter Lift | NOT CALCULABLE | NOT CALCULABLE | ❌ NOT CALCULABLE | Declared cohorts not stored at user level (`waitlist_signups` only, no `user_id`). Engagement definition undefined. |
| M08 | Identity-Claim Accuracy | CALCULABLE | CALCULABLE w/ caveat | ⚠️ PROXY — declared, defective | `updated_at = created_at` edit proxy is polluted by AI re-upserts — undercounts accurate claims. Directionally valid (conservative). Fix requires `last_user_edit_at`. |
| M09 | K-Factor | NOT IMPLEMENTED | "partial" (waitlist) | ⬜ NOT IMPLEMENTED | `waitlist_signups.inbound_ref` measures pre-signup referral rate, not verified-user-to-verified-user density. Populations incompatible. Definition not met. |
| M10 | Safety / Trust Floor | NOT CALCULABLE | Numerator available | ⚠️ PROXY — interim, declared | Correct numerator: `moderation_reports` (not `event_reports`). Denominator (sessions) unavailable. Interim: reports per 1,000 verified users, explicitly declared. Upgrade path: instrument `app_opened` in `analytics_events`. |

**Legend:**
- ✅ CALCULABLE — exact: faithful to original definition, no proxy
- ⚠️ PROXY — declared: calculable with declared limitation
- ❌ NOT CALCULABLE: required data does not exist
- ⬜ NOT IMPLEMENTED: requires new infrastructure

---

## 2. Backend Requests

The following items are required from Backend (and where noted, Frontend) to unblock the remaining metrics. Prioritized by impact on investor-visible metrics.

| Priority | Metric | Request | Unblocks | Owner |
|---|---|---|---|---|
| 🔴 P1 | M01 (fast path) | Add `event_id` (uuid, nullable, FK to events) to `irl_confirmations`. When a mom confirms an IRL interaction that originated from a specific event, populate this field. Enables pair co-attendance derivation via self-join on shared `event_id`s within the same block — without requiring full check-in instrumentation. **This is the fastest path to unblocking M01.** | M01 (calculable without full check-in) | Backend |
| 🔴 P1 | M03, M01 (full path) | Instrument check-in to write an attendance signal. Set `event_requests.status = 'attended'` on check-in, or surface `thread_events` check-ins as pair co-attendance. Required for M03 and for M01 at full fidelity. | M01 (full fidelity), M03 (fully calculable) | Backend + Frontend |
| 🔴 P1 | M08 | Add `last_user_edit_at` column to `user_identity_claims` (timestamp, nullable). Populate only on user-initiated edits — not on AI re-upserts via `upsert_claims()` or `replace_all_claims()`. | M08 (removes defective proxy) | Backend |
| 🔴 P1 | M10 | Instrument `app_opened` event into `analytics_events` table. This provides the session denominator for M10 and removes the interim proxy entirely. Note: do not use Amplitude for this — `analytics_events` in Supabase is the correct target, keeping both dashboards independent. | M10 (exact calculation) | Frontend + Backend |
| 🟡 P2 | M05, M06 | Create `block_state_changes` table (`block_id`, `old_state`, `new_state`, `changed_at`) with a trigger on `blocks.state` that writes on every state update. No backfill possible — metric starts from creation date. | M05, M06 (both become calculable) | Backend |
| 🟡 P2 | M09 | Add `referred_by` column (uuid, nullable) to `users` table, populated during signup when a referral code is present. Option B (dedicated referrals table) can follow in Phase 2. | M09 (becomes calculable) | Backend |
| ⚪ P3 | M07 | Define and implement a user-level declared cohort store. `waitlist_signups.declared_cohorts` is not joinable to verified users. Options: (a) copy cohorts to `users` table at onboarding completion, or (b) create `user_cohorts` table. Also requires a formal definition of "engagement" for this metric from Product. | M07 (requires both schema + definition) | Backend + Product |

**Priority reference:**
- P1 — Blocks metrics with partial infrastructure today. High-impact, targeted changes.
- P2 — Blocks NOT IMPLEMENTED metrics. Requires new tables or columns.
- P3 — Requires both schema changes and product definition decisions.

---

## 3. Metric Definitions

---

### M01 · The Magic Number

**STATUS: ❌ NOT CALCULABLE**

**Source:** —
**Original definition:** Recurring weekly fellows per LIVE block — moms who encountered the same other mom more than once in the same week, per LIVE block.

**Why it cannot be calculated today:**
Calculating recurring fellows requires pair-level encounter data: which specific moms met each other, and how many times, within a 7-day window per block. While `irl_confirmations` and `relationship_tier_events` exist, they confirm that relationships exist — not the frequency or recurrence of encounters within a block in a given week. Co-attendance derivation depends on `event_requests.status = 'attended'`, which no code path writes today.

**Notes:**
- **Fast path:** Add `event_id` to `irl_confirmations`. If a confirmed IRL interaction is linked to the event that originated it, pairs sharing ≥ 2 confirmed `event_id`s within the same block in a 7-day window satisfy the definition. This avoids the full check-in instrumentation dependency and is the recommended P1 request.
- `irl_confirmations` currently has no `event_id`, and `confirmed_at` reflects when the confirmation was made — not when the encounter occurred. The fast path requires a schema addition from Backend (P1).
- **Full fidelity path:** Instrument check-in to write `event_requests.status = 'attended'`. Also required for M03.

---

### M02 · Connection Velocity

**STATUS: ✅ CALCULABLE — exact**

**Source:** `relationship_tier_events`, `users`
**Definition:** Median days from signup to first real connection with another mom.

**Rationale:**
`relationship_tier_events` is append-only with `from_tier`, `to_tier`, `trigger_event`, and `created_at` per transition. First connection = earliest event where `to_tier` reaches `acquaintance` or above (first mutual state). Days-to-connect derived by joining to `users.created_at`. No proxy needed.

**SQL:**
```sql
-- M02: median days from signup to first real (acquaintance+) connection
WITH first_conn AS (
  SELECT user_low, user_high, MIN(created_at) AS connected_at
  FROM public.relationship_tier_events
  WHERE to_tier IN ('acquaintance', 'direct', 'irl_peer')
  GROUP BY user_low, user_high
),
per_user AS (
  SELECT u.id AS user_id,
         EXTRACT(DAY FROM (fc.connected_at - u.created_at))::int AS days_to_connect
  FROM first_conn fc
  JOIN public.users u ON u.id IN (fc.user_low, fc.user_high)
)
SELECT ROUND(CAST(
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY days_to_connect) AS numeric), 1)
FROM per_user
WHERE days_to_connect >= 0;
```

**Notes:**
- Tier ladder: `stranger → nudge → acquaintance → direct → irl_peer`. `acquaintance` is the first mutual state and is used as the connection threshold.
- v4.1 listed this as NOT CALCULABLE based on `user_relationships.last_transition_at`. The correct table is `relationship_tier_events` (append-only history), confirmed by Backend.

---

### M03 · Friendship Compound

**STATUS: ❌ NOT CALCULABLE**

**Source:** —
**Definition:** % of moms who attended a second event with someone they met at a first event.

**Why it cannot be calculated today:**
This requires pair-level co-attendance data. `event_requests` has `attended` in its status enum, but no code path ever writes it — the only reachable terminal status from host approval is `approved`. Actual presence is tracked separately as `thread_events.event_type = 'check_in'` per event, not per pair.

**Notes:**
- `approved` is not a valid proxy for attendance — approval means the request was accepted, not that the mom showed up.
- Unblocked by: Backend P1 — instrument check-in to write `event_requests.status = 'attended'`.

---

### M04 · Block Density at LIVE

**STATUS: ✅ CALCULABLE — exact**

**Source:** `users`, `blocks`
**Definition:** Average number of verified users per LIVE block, where verified = `phone_verified_at IS NOT NULL` and `home_block_id` is set.

**Rationale:**
Measures product adoption density. Verified moms who completed onboarding (phone confirmed + home block assigned). Waitlist counts excluded. `home_block_id` can point to non-live blocks; the SQL handles this correctly by filtering `blocks.state = 'live'` on the blocks side of the JOIN.

**SQL:**
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

**Notes:**
- No changes from v4.1. Confirmed by Backend.

---

### M05 · Block-Unlock Velocity

**STATUS: ⬜ NOT IMPLEMENTED**

**Source:** —
**Definition:** Median days a block spent in racing state before transitioning to live.

**What infrastructure is needed:**
The `blocks` table stores only the current state. There is no history of state transitions.
- Create `block_state_changes` table: `block_id`, `old_state`, `new_state`, `changed_at`
- Add a trigger on `blocks.state` that writes to this table on every update
- No backfill possible — metric starts from the moment the table is created

---

### M06 · Cluster Activation Curve

**STATUS: ⬜ NOT IMPLEMENTED**

**Source:** —
**Definition:** Average days for a cluster to reach its first 3 simultaneously LIVE blocks.

**What infrastructure is needed:**
Same dependency as M05. Once `block_state_changes` is implemented, derivable by querying the 3rd LIVE transition per `cluster_id` ordered by `changed_at`. `cluster_id` is already on the `blocks` table — no additional schema needed beyond M05.

---

### M07 · Combine-Filter Lift

**STATUS: ❌ NOT CALCULABLE**

**Source:** —
**Definition:** Engagement ratio between moms with 2–4 declared cohorts vs moms with a single declared cohort.

**Why it cannot be calculated today:**
Two issues block this metric:
1. **No user-level cohort store:** Declared cohorts exist only in `waitlist_signups.declared_cohorts` (text[]) at signup, with no `user_id` — they cannot be joined to verified users after onboarding completion.
2. **Engagement is undefined:** Could mean RSVPs, attended events, intros accepted, sessions, or check-ins. Without a precise definition, any calculation is arbitrary.

**Notes:**
- Important distinction: declared cohorts (selected during onboarding from `cohorts.yaml`) ≠ AI-extracted identity claims (`user_identity_claims`). These are different constructs and must not be conflated.
- Unblocked by: Backend P3 — user-level cohort store + formal engagement definition from Product.

---

### M08 · Identity-Claim Accuracy

**STATUS: ⚠️ PROXY — declared, defective**

**Source:** `user_identity_claims`
**Definition:** % of identity claims at least 7 days old that have neither been dismissed nor modified since AI extraction.

**Rationale:**
The edit proxy (`updated_at = created_at`) is defective: the Lana worker's `upsert_claims()` performs an in-place UPDATE when a claim of the same `(user_id, concept)` already exists, which bumps `updated_at` via the `set_updated_at` trigger — even though no user edited the claim. This causes M08 to **undercount** accurate claims. The metric is directionally valid (conservative) but not precise.

**SQL:**
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

**Notes:**
- **PROXY DECLARED:** `updated_at = created_at` is used as the unedited-claim proxy. It undercounts accurate claims due to AI re-upserts. The true accuracy rate is equal to or higher than the reported figure.
- `replace_all_claims()` uses delete+insert, which resets `updated_at = created_at` — reliability also depends on which write path ran.
- Fix: Backend P1 — add `last_user_edit_at` and gate M08 on that instead of `updated_at`.

---

### M09 · K-Factor

**STATUS: ⬜ NOT IMPLEMENTED**

**Source:** —
**Definition:** Referral density per active user — how many new verified users each active verified user generates.

**Why Backend's "partial" verdict is rejected:**
`waitlist_signups.inbound_ref` measures pre-signup referral conversion rate — how many waitlist signups came via a referral link. This is a different metric: the population is pre-verification, there is no `user_id`, and it cannot be joined to verified user activity. There is no partial calculation available. Presenting this as K-Factor in an investor dashboard would be misleading.

**What infrastructure is needed:**
- Add `referred_by` (uuid, nullable) to the `users` table, populated at signup when a referral code is present.
- Option B (dedicated referrals table with link clicks and attribution) can follow in Phase 2.

---

### M10 · Safety / Trust Floor

**STATUS: ⚠️ PROXY — interim, declared**

**Source:** `moderation_reports`, `users`
**Definition:** Safety reports and anti-discrimination incidents per 1,000 app sessions.

**Rationale:**
The numerator is now correctly identified as `moderation_reports` (written by `report_message()`), which contains real safety categories (`harassment`, `threat`, `sexual`, `csam`, `self_harm`, `spam`, `off_platform_ask`, `other`). The v4.1 doc referenced `event_reports`, which tracks sketchy events — not interpersonal safety. The denominator (sessions) is unavailable: `app_opened` is not instrumented and `analytics_events` has zero inserts.

**SQL (interim proxy):**
```sql
-- M10 (interim proxy): safety reports per 1,000 verified users
-- DECLARED PROXY: verified user count substituted for session count.
-- Upgrade path: instrument app_opened in analytics_events (Supabase).
SELECT ROUND(
  (SELECT COUNT(*) FROM public.moderation_reports) * 1000.0
  / NULLIF((SELECT COUNT(*) FROM public.users WHERE phone_verified_at IS NOT NULL), 0),
2);
```

**Notes:**
- **PROXY DECLARED:** Verified user count is used as the denominator in place of sessions. This is a different metric from the original definition and must be labeled as such in any dashboard.
- Correct numerator: `moderation_reports` — **not** `event_reports` (v4.1 referenced the wrong table).
- Three distinct safety lanes exist: reports → `moderation_reports`; blocks → `user_blocks`; moderator actions → `moderation_actions`. Do not conflate them.
- **Upgrade path (no external dependency):** Instrument `app_opened` into `analytics_events` (already exists in Supabase). Once populated, the exact denominator is available without Amplitude integration or plan upgrade.

---

## 4. Changes from v4.1

| # | Metric | v4.1 | v5.0 | Reason |
|---|---|---|---|---|
| M02 | Connection Velocity | NOT CALCULABLE | ✅ CALCULABLE — exact | `relationship_tier_events` (append-only) confirmed by Backend. `MIN(created_at)` per pair is a faithful calculation. `user_relationships.last_transition_at` (v4.1 assumption) was the wrong field. |
| M08 | Identity-Claim Accuracy | CALCULABLE | ⚠️ PROXY — declared, defective | AI re-upserts via `upsert_claims()` bump `updated_at`, causing undercounting. Proxy is declared. Fix requires `last_user_edit_at`. |
| M09 | K-Factor | NOT IMPLEMENTED | ⬜ NOT IMPLEMENTED (confirmed) | Backend's "partial" verdict via `waitlist_signups` rejected. Populations incompatible with definition. Not partial — not calculable. |
| M10 | Safety / Trust Floor | NOT CALCULABLE | ⚠️ PROXY — interim, declared | Correct numerator identified: `moderation_reports` (not `event_reports`). Interim proxy: reports per 1,000 verified users, explicitly declared. |
| M01 | The Magic Number | NOT CALCULABLE | ❌ NOT CALCULABLE (fast path documented) | Backend's "calculable near-zero" verdict rejected. However, a fast path is now documented: add `event_id` to `irl_confirmations` to enable pair co-attendance derivation without full check-in instrumentation. |

---

*Lana · Phygtl, Inc. · Internal document · Not for external distribution · v5.0 · June 2026*
