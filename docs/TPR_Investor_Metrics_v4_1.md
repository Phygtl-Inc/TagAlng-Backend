# TPR v4.1 · Lana Investor Dashboard — 10 Metrics

**Status:** Revised after schema review. Proxy metrics removed.
**Owner:** Data & AI
**Date:** June 2026

---

## Core principle

A metric must calculate exactly what its definition says. If the required data does not exist, the metric is marked NOT CALCULABLE or NOT IMPLEMENTED. Proxies are only considered when a technical limitation makes faithful instrumentation impossible — in those cases the proxy must be explicitly declared alongside its impact on the original definition.

---

## 1. Executive Summary

| # | Metric | Source table | Status |
|---|---|---|---|
| 01 | The Magic Number | — | NOT CALCULABLE |
| 02 | Connection Velocity | users + user_relationships | NOT CALCULABLE (pending clarification) |
| 03 | Friendship Compound | event_requests | NOT CALCULABLE (pending clarification) |
| 04 | Block Density at LIVE | users + blocks | CALCULABLE |
| 05 | Block-Unlock Velocity | — | NOT IMPLEMENTED |
| 06 | Cluster Activation Curve | — | NOT IMPLEMENTED |
| 07 | Combine-Filter Lift | — | NOT CALCULABLE (pending clarification) |
| 08 | Identity-Claim Accuracy | user_identity_claims | CALCULABLE |
| 09 | K-Factor | — | NOT IMPLEMENTED |
| 10 | Safety / Trust Floor | — | NOT CALCULABLE |

> Only M04 and M08 can be calculated today with full fidelity to their original definitions. All others require either infrastructure that does not exist (NOT IMPLEMENTED) or clarification from the backend lead before a faithful calculation can be written (NOT CALCULABLE).

---

## 2. Metric Definitions

---

### M01 · The Magic Number

**STATUS: NOT CALCULABLE**

**Original definition (R&D Kickoff):**
Recurring weekly fellows per LIVE block — moms who encountered the same other mom more than once in the same week, per LIVE block.

**Why it cannot be calculated today:**
Calculating recurring fellows requires pair-level encounter data — which specific moms met each other, and how many times, within a 7-day window. `local_signals` captures what a mom is looking for (intent), not who she actually met. No table in the current schema records pair-level co-attendance or repeated encounters.

**Question for Backend:**
Is there a table that records when two specific moms met each other (co-attended the same event, had a confirmed IRL interaction)? If not, what would be the correct table to use to calculate recurring encounters between pairs of moms within the same block?

---

### M02 · Connection Velocity

**STATUS: NOT CALCULABLE — Pending Backend clarification**

**Original definition (R&D Kickoff):**
Median days from signup to first connection with another mom.

**Why it cannot be calculated today:**
`user_relationships` exists and has `last_transition_at`, which could represent when two moms first connected. However it is unclear: (a) whether `last_transition_at` records the first connection or the most recent transition, (b) which tier value represents the first real social connection, and (c) whether `user_relationships` is the correct table or if another table better captures the first connection event.

**Question for Backend:**
Does `user_relationships.last_transition_at` record when two moms first connected? Which tier value represents the minimum threshold for a real connection? Is there a `created_at` on `user_relationships` that would give the exact moment the first connection was established?

---

### M03 · Friendship Compound

**STATUS: NOT CALCULABLE — Pending Backend clarification**

**Original definition (R&D Kickoff):**
% of moms who attended a second event with someone they met at a first event.

**Why it cannot be calculated today:**
This requires co-attendance data — which moms were at the same event together, and whether any pair appeared at two or more events together. `event_requests` may contain this if it records attendance per event per user, but it is not confirmed whether: (a) `event_requests` has sufficient data to identify co-attending pairs, (b) status values like `attended` are populated, or (c) there is a better table for this calculation.

**Question for Backend:**
Does `event_requests` record confirmed attendance (not just requests) per event per user? What status value means the mom actually attended? Is there another table that records co-attendance between pairs of moms at the same event?

---

### M04 · Block Density at LIVE

**STATUS: CALCULABLE — Real data**

**Source:** users + blocks

**Definition:**
Average number of verified users per LIVE block, where verified = `phone_verified_at IS NOT NULL` and `home_block_id` is set.

**Rationale:**
Measures product adoption density. Uses the users table directly — verified moms who completed onboarding (phone confirmed + home block assigned). Waitlist counts are excluded because they measure acquisition intent, not actual adoption.

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

---

### M05 · Block-Unlock Velocity

**STATUS: NOT IMPLEMENTED — Infrastructure required**

**Original definition (R&D Kickoff):**
Median days a block spent in racing state before transitioning to live.

**What infrastructure is needed:**
The `blocks` table only stores current state. There is no history of state transitions.
- Create `block_state_changes` table: `block_id`, `old_state`, `new_state`, `changed_at`
- Add a trigger on `blocks.state` that writes to this table on every update
- Note: backfill is not possible — metric starts from the moment the table is created

---

### M06 · Cluster Activation Curve

**STATUS: NOT IMPLEMENTED — Infrastructure required**

**Original definition (R&D Kickoff):**
Average days for a cluster to reach its first 3 simultaneously LIVE blocks.

**What infrastructure is needed:**
Same dependency as M05. Once `block_state_changes` is implemented, this metric is derivable by querying the 3rd LIVE transition per `cluster_id` ordered by `changed_at`. `cluster_id` is already on the `blocks` table — no additional schema needed.

---

### M07 · Combine-Filter Lift

**STATUS: NOT CALCULABLE — Pending Backend clarification**

**Original definition (R&D Kickoff):**
Engagement ratio between moms with 2-4 cohorts vs moms with a single cohort.

**Why it cannot be calculated today:**
Two unresolved questions block this calculation:

1. **Cohort vs Claim** — the R&D Kickoff definition uses "cohort", but the current schema has `user_identity_claims` (AI-extracted claims) and declared cohorts from onboarding. These are different things. It is not clear which one the metric refers to, or where declared cohorts are stored in the schema.

2. **Engagement is not defined** — "engagement" could mean RSVPs, attended events, intros accepted, sessions, or check-ins. Without a precise definition, any calculation would be arbitrary.

**Question for Backend:**
Where are declared cohorts stored in the schema (the cohorts a mom selects during onboarding from `cohorts.yaml`)? Is the metric referring to declared cohorts or AI-extracted identity claims? Once cohorts are located: what is the correct definition of engagement for this metric?

---

### M08 · Identity-Claim Accuracy

**STATUS: CALCULABLE — Real data**

**Source:** user_identity_claims

**Definition:**
% of identity claims at least 7 days old that have neither been dismissed nor modified since AI extraction.

**Rationale:**
Claims less than 7 days old are in their evaluation window. `updated_at = created_at` is the edit proxy: if a claim was modified after extraction, `updated_at` will differ from `created_at`. Dismissed claims are explicitly rejected by the mom.

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

---

### M09 · K-Factor

**STATUS: NOT IMPLEMENTED — Infrastructure required**

**Original definition (R&D Kickoff):**
Referral density per active user — how many new users each active user generates.

**What infrastructure is needed:**
The `users` table has no referral tracking field.
- Option A (simpler): add `referred_by` column (uuid, nullable) to the `users` table, populated during signup if a referral code is present
- Option B (more granular): create a dedicated referrals table tracking link clicks, conversions, and attribution
- Option A is sufficient for Phase 1

---

### M10 · Safety / Trust Floor

**STATUS: NOT CALCULABLE**

**Original definition (R&D Kickoff):**
Safety reports and anti-discrimination incidents per 1,000 app sessions.

**Why it cannot be calculated today:**
Two components are wrong today:

1. **Numerator** — the original definition uses formal safety reports (`event_reports`). This table currently has 0 rows — the feature exists in the schema but is not active. `user_blocks` is a different signal (a mom blocking another mom) and is not equivalent to a safety report.

2. **Denominator** — the original definition uses sessions (1k sessions). `app_opened` is not instrumented, so session count does not exist. Verified user count is not a substitute for sessions.

**Question for Backend:**
When will `event_reports` start receiving real data? Is there an active report flow that will populate it? Once `event_reports` is active and `app_opened` is instrumented, the calculation can be written faithfully.

---

## 3. Questions for Backend — Required Before Metrics Can Be Calculated

### M01 — The Magic Number
1. Is there a table that records when two specific moms met each other (co-attended the same event or had a confirmed IRL interaction)?
2. If not, what is the intended data source for calculating recurring encounters between pairs of moms within the same block?

### M02 — Connection Velocity
3. Does `user_relationships.last_transition_at` record when two moms first connected, or the most recent state change?
4. Is there a `created_at` on `user_relationships` that captures the moment the relationship was first established?
5. Which tier value represents the minimum threshold for a real social connection?

### M03 — Friendship Compound
6. Does `event_requests` record confirmed attendance per event per user? What status value means the mom actually attended (not just requested)?
7. Is there a table that records co-attendance between pairs of moms at the same event?

### M07 — Combine-Filter Lift
8. Where are declared cohorts stored in the schema (the cohorts a mom selects during onboarding from `cohorts.yaml`)?
9. Does the metric refer to declared cohorts or AI-extracted identity claims from `user_identity_claims`? These are different things.
10. What is the definition of engagement for this metric? (RSVPs, attended events, intros accepted, sessions, or check-ins?)

### M10 — Safety / Trust Floor
11. When will `event_reports` start receiving real data? Is there an active report flow that will populate it?
12. Does `user_blocks` capture all safety signals, or are reports and blocks tracked separately?

### M04 and M08 — Advisory (no blocker)
13. M04: Does `home_block_id` always correspond to a LIVE block, or can a verified user have a `home_block_id` pointing to a non-live block?
14. M08: Is `updated_at = created_at` a reliable proxy for an unedited claim, or does any system process update `updated_at` automatically?

---

*Lana · Phygtl, Inc. · Internal document · Not for external distribution*
