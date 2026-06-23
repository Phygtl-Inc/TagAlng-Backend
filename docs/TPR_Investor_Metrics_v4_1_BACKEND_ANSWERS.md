# TPR v4.1 · Backend Answers to Investor-Metric Questions

**Responding to:** [TPR_Investor_Metrics_v4_1.md](TPR_Investor_Metrics_v4_1.md) — "Questions for Backend"
**Owner:** Backend
**Date:** 2026-06-23
**Method:** Answers verified against the live Supabase migrations and the Lana worker code (file:line references throughout).

---

## TL;DR

The schema is richer than v4.1 assumes. Three "NOT CALCULABLE" verdicts change once you account for tables the doc didn't reference:

- **`relationship_tier_events`** — append-only pairwise transition history (fixes M02).
- **`irl_confirmations`** + **`promote_irl_from_attendance()`** — pair-level IRL data (helps M01).
- **`moderation_reports`** — the *active* safety-report flow (fixes the M10 numerator).

The doc's central correct instinct: **`analytics_events` has zero writes anywhere** in the migrations or the worker. The original `investor_metrics` view (`20260603000009_investor_metrics_view (1).sql`) depends on it, so M01/M02/M03/M05/M06/M10 in that view return NULL/empty. v4.1 was right to strip those proxies.

### Corrected status table

| # | v4.1 status | Corrected status | Reason |
|---|---|---|---|
| M01 | NOT CALCULABLE | Calculable, near-zero data | `irl_confirmations` + `relationship_tier_events` exist; co-attendance blocked on unwritten `attended` |
| **M02** | NOT CALCULABLE | **CALCULABLE today** | `relationship_tier_events` gives first-connection time + history |
| M03 | NOT CALCULABLE | NOT CALCULABLE (confirmed) | `attended` is never written; check-ins live in `thread_events` |
| M04 | CALCULABLE | CALCULABLE ✓ | non-live home blocks handled correctly |
| M07 | NOT CALCULABLE | NOT CALCULABLE (confirmed) | no user-level cohort store; engagement undefined |
| M08 | CALCULABLE | CALCULABLE **with caveat** | `updated_at` proxy is tripped by AI re-upserts, not just user edits |
| **M10** | NOT CALCULABLE | **Numerator now available** | `moderation_reports` is the active flow; only the session denominator is missing |
| M09 | NOT IMPLEMENTED | Partial | waitlist-level referral rate already works via `waitlist_signups.inbound_ref` |

---

## M01 · The Magic Number — recurring pair encounters

**Q1: Is there a table recording when two specific moms met / co-attended / confirmed IRL?**
Yes, three:

- **`irl_confirmations`** — `20260622120000_irl_promotion.sql:12`. Manual mutual "we met in real life" confirmations, pair-keyed (`user_low`, `user_high`, `confirmed_by`, `confirmed_at`). This is "confirmed IRL interaction."
- **`relationship_tier_events`** — `20260613120000_social_graph_lana_tools.sql:46`. Every pairwise tier promotion with `trigger_event` and `created_at`.
- **Co-attendance is derivable** from `event_requests`, exactly as `promote_irl_from_attendance()` does it (`20260622120000_irl_promotion.sql:174-185`): self-join attendees of the same `event_id` (plus the host) into pairs.

**Q2: Intended data source for recurring encounters within a block?**
Use `relationship_tier_events` / `irl_confirmations` for confirmed pair interactions, joined to a block. The co-attendance path depends on `event_requests.status = 'attended'`, which **no code path writes today** (see M03). So the metric is calculable in principle (count pairs with ≥2 interactions in a 7-day window per block) but will be near-empty until check-in is instrumented.

**Verdict:** Upgrade from "NOT CALCULABLE" to **"calculable, near-zero data until check-in is instrumented."**

---

## M02 · Connection Velocity — the doc is wrong, this IS calculable

**Q3: Does `user_relationships.last_transition_at` record the first connection?**
No — it's the **most recent** transition. The table stores only `tier`, `last_transition_at`, `last_trigger` (`20260613120000_social_graph_lana_tools.sql:18-26`); it is overwritten on each promotion.

**Q4: Is there a `created_at` on `user_relationships`?**
No — and it isn't needed. **`relationship_tier_events` is append-only** with `from_tier`, `to_tier`, `trigger_event`, `created_at` per transition. First connection = `MIN(created_at)` per pair where `to_tier` reaches the threshold; join the earlier party's `users.created_at` for days-to-first-connection.

**Q5: Which tier is the minimum real connection?**
Ladder: `stranger → nudge → acquaintance → direct → irl_peer` (`20260613120000_social_graph_lana_tools.sql:5`). `nudge` is a one-way ping; the first *mutual* state is **`acquaintance`**, and `direct` is mutual-unmask. Recommend **`acquaintance`** as the floor (product call — data supports either).

**Verdict:** **CALCULABLE today** via `relationship_tier_events`. Proposed SQL:

```sql
-- M02: median days from signup to first real (acquaintance+) connection
WITH first_conn AS (
  SELECT
    e.user_low, e.user_high,
    MIN(e.created_at) AS connected_at
  FROM public.relationship_tier_events e
  WHERE e.to_tier IN ('acquaintance', 'direct', 'irl_peer')
  GROUP BY e.user_low, e.user_high
),
per_user AS (
  -- attribute the connection to each member, measured from THEIR signup
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

---

## M03 · Friendship Compound

**Q6: Does `event_requests` record confirmed attendance? What status means attended?**
Enum is `('pending','approved','declined','cancelled','attended')` (`20260529000000_phase3_events_rtj_nudges.sql:127`). **`'attended'` is never written** by any RPC, trigger, or worker code — it appears only inside `status IN ('approved','attended')` *read* filters. The only reachable terminal status from host approval is `'approved'`. Actual presence is tracked separately as **`thread_events.event_type = 'check_in'`** (`20260610120000_profile_dashboard.sql:112-115`), per-event, **not per-pair**.

**Q7: Is there a co-attendance table?**
None dedicated, but pairs are derivable from `event_requests` by `event_id` (same pattern as the IRL cron). "Second event with someone from a first event" = pairs sharing ≥2 events.

**Verdict:** **NOT CALCULABLE faithfully** until check-in writes back an attendance signal. `approved` is a weak proxy (approval ≠ showed up).

---

## M04 · Block Density at LIVE — confirmed CALCULABLE

**Q13: Can a verified user's `home_block_id` point to a non-live block?**
Yes. `home_block_id` references `blocks(id)` with no state constraint (`20260525110005_phase2_users_auth_geo.sql:19`). The existing M04 SQL handles this correctly — `b.state = 'live'` is on the blocks side of the LEFT JOIN, so non-live home blocks are excluded. **No change needed.**

---

## M07 · Combine-Filter Lift

**Q8: Where are declared cohorts stored?**
Only at signup, on **`waitlist_signups.declared_cohorts text[]`** (`20260525100002_phase1_core.sql:31`), validated against the `cohorts` reference table (mirror of `cohorts.yaml`). **There is no per-verified-user cohort column on `users` and no `user_cohorts` table.** `waitlist_signups` is keyed by phone/block, not `user_id`, so it doesn't cleanly join to a verified user's engagement.

**Q9: Declared cohorts vs AI-extracted claims?**
Different things, as the doc says. The old view used `COUNT(DISTINCT concept)` from `user_identity_claims` (`20260603000009_investor_metrics_view (1).sql:118-119`) — that is AI-extracted claims, **not** declared cohorts.

**Q10: Engagement definition?**
Undefined in schema. The old view used `event_requests` count where `status IN ('attended','approved')` — and since `attended` is never written, that collapses to approved-request count.

**Verdict:** **NOT CALCULABLE** as a declared-cohort metric without (a) a user-level cohort store and (b) an agreed engagement definition. Confirmed.

---

## M08 · Identity-Claim Accuracy — CALCULABLE with a caveat

**Q14: Is `updated_at = created_at` a reliable "unedited" proxy?**
**No — this is a real bug in M08.** `user_identity_claims` has a `set_updated_at` BEFORE-UPDATE trigger (`20260525120007_phase2_identity_claims.sql:32-34`) that bumps `updated_at` on **any** UPDATE. The Lana worker's `upsert_claims()` does an **in-place UPDATE when a claim of the same `(user_id, concept)` already exists** (`services/lana-worker/app/claims_persist.py:500-502`) — i.e. whenever the AI re-extracts/refines the same concept across onboarding turns. So `updated_at ≠ created_at` is set by a **system process, not a user edit**, making M08 *undercount* accurate claims.

Note: `replace_all_claims()` uses delete+insert, which resets `updated_at = created_at` — so reliability even depends on which write path ran.

**Recommendation:** add an explicit `last_user_edit_at` (or a source-of-change flag) and gate M08 on that, instead of the `updated_at = created_at` proxy which conflates AI refinement with user rejection.

---

## M09 · K-Factor — partial today

The waitlist-level referral rate **already works**: `waitlist_signups.inbound_ref` (`20260525100002_phase1_core.sql:34`) is populated when a referral link is present, and the old view computed referral rate from it (`20260603000009_investor_metrics_view (1).sql:165-171`). True user→user K-factor still needs `users.referred_by` (or a referrals table), as the doc states. So: **partial now, full K-factor needs the referral column.**

---

## M10 · Safety / Trust Floor — the doc is looking at the wrong table

**Q11: When will `event_reports` get real data?**
`event_reports` (`20260529130000_phase3_remaining_rpcs.sql:4`) reports **events**, via `report_event()` (granted to authenticated, `20260529130000_phase3_remaining_rpcs.sql:389`). It works, but it is "this event is sketchy," not interpersonal safety.

**The active safety-report flow is `moderation_reports`** (`20260620120000_block_and_report.sql:35`), written by **`report_message()`** (granted to authenticated, `20260620120000_block_and_report.sql:405`), with real safety categories (`harassment`, `threat`, `sexual`, `csam`, `self_harm`, `spam`, `off_platform_ask`, `other`). **This is the correct numerator for "safety reports," and v4.1 doesn't mention it.**

**Q12: Does `user_blocks` capture all safety signals?**
No. Three distinct lanes:
- **Reports** → `moderation_reports`
- **Blocks** → `user_blocks` (`20260618130000_peer_chat_shielded.sql:13`)
- **Moderator actions** → `moderation_actions` (`20260620120000_block_and_report.sql`)

`user_blocks` is a weaker signal than a report; do not equate them.

**Denominator:** still genuinely missing — `app_opened` is never instrumented and `analytics_events` has zero inserts. "Per 1,000 sessions" is uncomputable today. But a **numerator-only** trust signal (reports per verified user, or per active thread) is available now:

```sql
-- M10 (interim): safety reports per 1,000 verified users
SELECT ROUND(
  (SELECT COUNT(*) FROM public.moderation_reports) * 1000.0
  / NULLIF((SELECT COUNT(*) FROM public.users WHERE phone_verified_at IS NOT NULL), 0),
2);
```

---

## What backend needs to do to unblock the rest

1. **Instrument check-in** to write an attendance signal (set `event_requests.status = 'attended'`, or surface `thread_events` check-ins as pair co-attendance). Unblocks M01 (real data) and M03.
2. **Add `users.referred_by`** (uuid, nullable) populated at signup. Unblocks full M09.
3. **Add `block_state_changes`** history (table + trigger on `blocks.state`). Unblocks M05/M06 (no backfill — starts from creation).
4. **Add a session signal** (instrument `app_opened` into `analytics_events`, or a sessions table). Unblocks the M10 denominator.
5. **Add `user_identity_claims.last_user_edit_at`** so M08 distinguishes user edits from AI re-upserts.
6. **Decide M07 semantics**: add a user-level declared-cohort store and define "engagement."

---

*Backend · Phygtl, Inc. · Internal document · Not for external distribution*
