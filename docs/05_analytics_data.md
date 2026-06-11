# 05 · Analytics & Data — TagAlng

> **Status:** v0.3 · Confirmed facts only  
> **Owner:** Data & AI  
> **Updated:** June 2026  
> **Note:** Items marked ⚠️ are speculative or pending validation

---

## 1 · North Star

**Recurring weekly fellowships per LIVE block**

*Source: CTO Spec §3.1*

A recurring fellowship is counted when two or more users from the same block attend an activity and at least one has previously attended an activity with the same person.

**LIVE block** = `blocks.state IN ('live', 'day_zero')` — confirmed in schema.

⚠️ Numeric targets: not grounded in Lake Nona data. To be set by founder based on Lake Nona research dossier.

---

## 2 · Active user definition

**Active user = any user who generated at least one event in `analytics_events` on that day.**

This definition is intentionally simple for Day Zero. It will evolve as the product matures — a future definition may require a check-in or scene activation. The `app_opened` event (pending instrumentation) is the primary signal for this calculation. Note: anonymous users (created via `auth.signInAnonymously()`) may appear in events before phone verification — active user definition applies to verified users only for retention metrics.

---

## 3 · Cohort taxonomy

*Source: `cohorts.yaml` v1 — TagAlng-Backend*

**9 valid cohorts:**
`parents` · `sports` · `faith` · `sober` · `runner` · `newcomer` · `professional` · `creative` · `volunteer`

**8 sport subtypes** (parent: `sports`):
`basketball` · `soccer` · `tennis` · `pickleball` · `running` · `cycling` · `swimming` · `other`

**URL contract:** `?cohort=parents,sports&sub=basketball&ref=POST_SLUG`

**Open inconsistency:** `runner` exists as a standalone cohort and `running` as a sport subtype. Behavior when a user has both must be defined before instrumentation.

---

## 4 · Investor metrics (10)

All 10 metrics are defined in `docs/TPR_Investor_Metrics_v1_0.md` with exact SQL, required fields, and instrumentation status. The `investor_metrics` VIEW in Supabase consolidates all 10 into a single query for the Looker Studio dashboard.

| # | Metric | Real data | Pending event |
|---|---|---|---|
| 01 | The Magic Number | No | `event_checkin` |
| 02 | Connection velocity | No | `connection_made` |
| 03 | Friendship compound | No | `event_checkin` |
| 04 | Block density at LIVE | Yes | — |
| 05 | Block-unlock velocity | No | `block_state_changed` |
| 06 | Cluster activation curve | Yes | — |
| 07 | Combine-filter lift | No | `scene_activated` |
| 08 | Identity-claim accuracy | Yes | — |
| 09 | K-factor | Partial | `referral_sent` (future) |
| 10 | Safety/trust floor | Yes | — |

---

## 5 · Marketplace metrics (Lana · inquiry_signals)

Captured when moms express needs through the Lana concierge — selling items, swapping children's clothes, finding playdate partners, etc.

| Metric | SQL field | Definition |
|---|---|---|
| Total inquiries | `total_inquiries` | COUNT of all inquiry_signals |
| Inquiry opt-in % | `inquiry_opt_in_pct` | % with `opt_in_followup = true` |
| Inquiry match rate | `inquiry_match_rate_pct` | % with `status = 'matched'` |

Table: `inquiry_signals` — populated from Day Zero as moms use Lana.

---

## 6 · PWA metrics (NNU · DAU/MAU · Retention)

Standard product health metrics for the PWA page.

| Metric | Source | Status |
|---|---|---|
| NNU WoW | `users.created_at` | Ready |
| NNU MoM | `users.created_at` | Ready |
| DAU | `analytics_events` · `app_opened` | Pending `app_opened` event |
| MAU | `analytics_events` · `app_opened` | Pending `app_opened` event |
| Retention D1 | `analytics_events` · `app_opened` | Pending `app_opened` event |
| Retention D7 | `analytics_events` · `app_opened` | Pending `app_opened` event |
| Retention D30 | `analytics_events` · `app_opened` | Pending `app_opened` event |

**AI stack note:** Lana uses OpenAI (gpt-4o · gpt-4o-mini). The original identity worker uses Gemini via Vertex AI. These are separate layers — governance must track both.

**`app_opened` event spec:**
```json
{
  "event_name": "app_opened",
  "user_id": "uuid",
  "properties": {
    "platform": "pwa | ios | android",
    "app_version": "text"
  }
}
```

---

## 7 · Core events (12)

> **Frontend note:** The old PWA (`app.tagalng.com`) is frozen. All frontend events must be instrumented in the new Next.js 15 Lana app (implementer: Abdullah). Backend events: Asjid.


| # | Event | Phase | Destination | Status |
|---|---|---|---|---|
| 1 | `install` | 1 | `analytics_events` | Partial — old PWA only |
| 2 | `cohort_inbound` | 1 | `analytics_events` | Not instrumented |
| 3 | `signup_complete` | 1–2 | `analytics_events` | Pending validation |
| 4 | `identity_claims_extracted` | 2 | `analytics_events` | Partial |
| 5 | `scene_activated` | 2 | `analytics_events` | Not instrumented |
| 6 | `fellow_viewed` | 2 | `analytics_events` | Not instrumented |
| 7 | `event_rsvp` | 2 | `event_requests` + `analytics_events` | Partial |
| 8 | `event_checkin` | 2–3 | `thread_events` + `analytics_events` | Partial |
| 9 | `connection_made` | 3 | `analytics_events` | Not instrumented |
| 10 | `block_state_changed` | 1–3 | `analytics_events` | Partial |
| 11 | `cover_search` | 1 | `analytics_events` | Not instrumented |
| 12 | `identity_claim_dismissed` | 2–3 | `analytics_events` | Not instrumented |

---

## 8 · Schema gaps

Tables required that do not exist in current schema:

| Table | Purpose | Urgency |
|---|---|---|
| `ai_inference_log` | Cost, latency, prompt version per Vertex AI call | Before Day Zero |
| `prompt_versions` | Prompt versioning for identity-worker | Before Day Zero |

---

## 9 · Data we never collect

- Real-time GPS location
- Full street address (zip → H3 block ID only)
- Race or ethnicity
- Exact age
- Raw search query text (PII risk)

---

## What is still missing from this document

1. North Star numeric targets — based on Lake Nona block density
2. `02_product.md` — precise commitment ladder definition
3. Confirmed values for `inquiry_signals.category`, `status`, `urgency`

---

*TagAlng · Phygtl, Inc. · Internal document · Do not distribute externally*
