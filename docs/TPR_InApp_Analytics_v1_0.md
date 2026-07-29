# TPR v1.0 — In-App Analytics Metrics

**Product:** Lana · Phygtl, Inc.
**Platform:** Amplitude (App ID: 831022)
**Dashboard:** Market Metrics — Overview
**Owner:** Data & AI
**Date:** July 23, 2026
**Status:** Active

---

> ⚠️ **Data scope — production only.**
> All metrics in this dashboard are filtered exclusively to the production domain **`get.lana.help`**. Events from `localhost`, `lana-pwa.vercel.app`, or any dev/staging environment are excluded from all metrics.
> **Data cutoff date: June 23, 2026** — first confirmed day of production data.

---

## 1. Dashboard at a Glance

| # | Metric | Category | Description | Status | Caveat |
|---|---|---|---|---|---|
| M01 | DAU — Daily Active Users | Engagement | Unique users active on a given day | ✅ Active | — |
| M02 | WAU — Weekly Active Users | Engagement | Unique users active in last 7 days | ✅ Active | — |
| M03 | MAU — Monthly Active Users | Engagement | Unique users active in last 30 days | ✅ Active | — |
| M04 | Stickiness (DAU/MAU) | Engagement | % of monthly users active on an average day | ✅ Active | Manual calc |
| M05 | New Users | Growth | First-time users in the period | ✅ Active | — |
| M06 | Total Sessions | Engagement | Total sessions initiated by production users | ✅ Active | — |
| M07 | Avg Session Duration | Engagement | Average time per session | ⚠️ Investigating | SDK bug — inflated |
| M08 | Sessions / User | Engagement | Average sessions per active user | ✅ Active | Manual calc |
| M09 | N-Day Retention | Retention | % of users returning exactly on day N | ✅ Active | — |
| M10 | Business Funnel — Organizer Journey | Conversion | flow_start → event_hosted conversion | ⚠️ Partial | Guest steps missing |
| M11 | Business Events per Day | Product | Daily volume of core business events | ⚠️ Investigating | Server-side events excluded |

---

## 2. Metric Definitions

---

### M01 · DAU — Daily Active Users

**Status: ✅ Active**
**Category:** Engagement

**Business definition:**
Number of unique users who performed any active interaction with the product on a given day.

**Technical definition:**
Distinct `user_id` count that fired at least one `_active` event in a 24-hour window, filtered to `get.lana.help`. Amplitude metric: `uniques`. Each user counted once per day regardless of event volume.

**Notes:**
- DAU is not a sum of events — a user active 10 times in a day still counts as 1 DAU.
- Avg DAU in the executive table = sum of daily DAUs ÷ number of days with data. Zero-user days are excluded from the denominator to avoid distortion.

**Known limitation:**
Server-side events (`sessionId = -1`) do not carry a page domain and are excluded from this metric.

---

### M02 · WAU — Weekly Active Users

**Status: ✅ Active**
**Category:** Engagement

**Business definition:**
Total unique users who performed any active interaction with the product in the last 7 days.

**Technical definition:**
Distinct deduplicated `user_id` count that fired at least one `_active` event within a 7-day rolling window, filtered to `get.lana.help`. Amplitude metric: `uniques`.

**Notes:**
- WAU ≠ sum of 7 DAUs. A user active on 3 different days in the week counts as 1 WAU but as 3 in the DAU sum.
- Rolling window — updates daily.

---

### M03 · MAU — Monthly Active Users

**Status: ✅ Active**
**Category:** Engagement

**Business definition:**
Total unique users who performed any active interaction with the product in the last 30 days.

**Technical definition:**
Distinct deduplicated `user_id` count that fired at least one `_active` event within a 30-day rolling window, filtered to `get.lana.help`. Amplitude metric: `uniques`.

**Notes:**
- MAU ≠ sum of DAUs. A user active on 15 different days counts as 1 MAU.
- Rolling window — updates daily.

---

### M04 · Stickiness (DAU/MAU)

**Status: ✅ Active**
**Category:** Engagement

**Business definition:**
Percentage of monthly active users who were also active on an average day. Measures how essential the product is — the higher the stickiness, the more recurrent the usage.

**Formula adopted:**
```
Stickiness = Avg DAU ÷ MAU
```

Calculated manually from the Avg DAU and MAU values on the dashboard.

**Notes:**
Amplitude's native `pct_dau` uses a 30-day rolling window. This dashboard uses the market-standard formula (Avg DAU ÷ MAU) to eliminate single-day distortions and align with external benchmarks.

**Benchmarks:**

| Range | Classification | Reference |
|---|---|---|
| < 10% | Low | Sporadic usage |
| 10–25% | Medium | Typical SaaS |
| 25–50% | Good | Collaboration tools (e.g. Slack) |
| > 50% | Excellent | Social/daily-use apps |

**Known limitation:**
Manual calculation — not auto-updated. Recalculate when Avg DAU or MAU values change.

---

### M05 · New Users

**Status: ✅ Active**
**Category:** Growth

**Business definition:**
Number of users who interacted with the product for the first time in the analyzed period.

**Technical definition:**
Distinct `user_id` count that fired their first `_new` event in the project within the period. Filter applied as a **user segment** (not event property filter) because `_new` does not carry `[Amplitude] Page Domain`. Segment condition: `[Amplitude] Page Domain = get.lana.help`.

**Notes:**
- Current value: 80 new production users since Jun 23, 2026.
- New Users ≤ MAU. Every new user is also an active user, but not every active user is new.

---

### M06 · Total Sessions

**Status: ✅ Active**
**Category:** Engagement

**Business definition:**
Total number of sessions initiated by users in the analyzed period.

**Technical definition:**
Count of unique `session_id` values initiated by production-segment users. A session begins with `[Amplitude] Start Session` and ends with `[Amplitude] End Session` or after 30 minutes of inactivity (Amplitude default timeout). Chart type: `sessions / sessionType: totalSessions`. Segment: `get.lana.help`.

**Notes:**
- Session count is reliable. The limitation below applies to session *duration*, not count.

**Known limitation:**
Abnormally long sessions exist in the data caused by `[Amplitude] End Session` not firing correctly on tab close. This affects Avg Session Duration but **not** Total Session count.

---

### M07 · Avg Session Duration

**Status: ⚠️ Under Investigation**
**Category:** Engagement

**Business definition:**
Average time a user spends in an active product session.

**Technical definition:**
Arithmetic mean of all session durations in the period: `session_end_timestamp - session_start_timestamp` in seconds. Chart type: `sessions / sessionType: average`. Segment: `get.lana.help`.

**Notes:**
Currently **omitted from the executive table** until the SDK issue is resolved.

**Known limitation:**
⚠️ The SDK is not firing `[Amplitude] End Session` correctly when the user closes the tab without navigation (`visibilitychange` / `beforeunload` not captured). This inflates average duration significantly.

**Engineering action required:**
- Verify `defaultTracking.sessions = true` in the Amplitude SDK configuration.
- Confirm that `[Amplitude] End Session` fires via `visibilitychange` or `beforeunload`.
- Consider setting an explicit session timeout (default is 30 minutes of inactivity).

---

### M08 · Sessions / User

**Status: ✅ Active**
**Category:** Engagement

**Business definition:**
Average number of sessions each active user initiated in the period.

**Formula adopted:**
```
Sessions / User = Total Sessions ÷ MAU
```

Calculated manually from the Total Sessions and MAU values on the dashboard, both filtered to the production domain.

**Notes:**
The Amplitude sessions-per-user chart (`sessionType: peruser`) shows average sessions per user **per day** — different from this metric which covers the full period. The manual calculation (Total ÷ MAU) is more representative for monthly analysis.

**Known limitation:**
Manual calculation — not auto-updated.

---

### M09 · N-Day Retention

**Status: ✅ Active**
**Category:** Retention

**Business definition:**
Percentage of users who return to the product exactly N days after their first interaction.

**Technical definition:**
For each cohort of users who performed the entry event on day D, measures the proportion who also performed the return event on exactly day D+N.
- Entry event: `_active`
- Return event: `_active`
- Method: N-Day (`nday`)
- Segment: `get.lana.help`
- Period: Jun 23, 2026 onwards

**How to read the chart:**
- Day 0 = 100% (all users who entered that day)
- Day 1 = % who returned the following day
- Day 7 = % who returned exactly 7 days later

**Benchmarks:**

| Day | Minimum benchmark |
|---|---|
| Day 1 | > 25% |
| Day 7 | > 10% |
| Day 30 | > 5% |

**Known limitation:**
With a small user base, cohort sizes are very small and individual values can be highly volatile. Read trends, not point-in-time values.

---

### M10 · Business Funnel — Organizer Journey

**Status: ⚠️ Partial**
**Category:** Conversion

**Business definition:**
Conversion funnel measuring the progression of organizer users through the core event creation flow.

**Technical definition:**
Ordered funnel with 24-hour conversion window.

| Step | Event | Description |
|---|---|---|
| 1 | `flow_start` | User initiates the event creation flow |
| 2 | `venue_picked` | User selects the event location/format |
| 3 | `event_setup_submitted` | User finalizes the configuration |
| 4 | `event_hosted` | Event is published/hosted |
| 5 | `event_invite_shared` | Invite is shared |

- Mode: Ordered — events must occur in this sequence (other events may occur in between).
- Conversion window: 86,400 seconds (24 hours).
- Count: unique users.
- Segment: `get.lana.help`.

**Known limitation:**
Guest-side funnel steps not yet instrumented. Missing events:
- `event_viewed` — guest opens the event invite link
- `rsvp_confirmed` — guest confirms attendance

End-to-end conversion is not measurable until these events are added.

---

### M11 · Business Events per Day

**Status: ⚠️ Under Investigation**
**Category:** Product

**Business definition:**
Daily volume of occurrences of the core business events in the product.

**Technical definition:**
Total count (`totals` — not unique users) of each event per day, filtered to the production segment. Events monitored:

| Event | Description |
|---|---|
| `event_hosted` | Event created and published by organizer |
| `event_invite_shared` | Invite shared |
| `item_listed` | Item listed — under investigation |
| `signal_saved` | Signal saved by user |

Segment: `get.lana.help`.

**Notes:**
High volume of a specific event signals feature adoption. Spikes or drops >20% vs 7-day average are flagged by the Business Events Daily Digest Agent.

**Known limitation:**
⚠️ `event_hosted`, `item_listed`, and `signal_saved` return zero with the production domain filter applied. These events are likely fired **server-side** (no page domain property) and are therefore not captured by the domain filter. Engineering must investigate whether an alternative environment property is needed for these events.

---

## 3. Known Limitations & Pending Actions

| # | Limitation | Impact | Action | Status |
|---|---|---|---|---|
| 1 | Unclosed sessions (SDK bug) | Avg Session Duration inflated | Verify `defaultTracking.sessions = true`; confirm End Session fires on tab close | 🔴 Active |
| 2 | Server-side events (`sessionId = -1`) | Not captured by production domain filter | Investigate alternative environment property | 🔴 Active |
| 3 | `event_hosted`, `signal_saved`, `item_listed` returning zero with production filter | Business Events possibly incomplete | Investigate with dev team — likely server-side events | 🔴 Active |
| 4 | Guest funnel not instrumented (`event_viewed`, `rsvp_confirmed`) | End-to-end conversion not measurable | Instrumentation priority for Frontend | 🟡 Pending |
| 5 | Stickiness and Sessions/User calculated manually | Not auto-updated on dashboard refresh | Amplitude limitation for these formulas | ⚪ Accepted |

---

## 4. Technical Glossary

| Term | Definition |
|---|---|
| `_active` | Amplitude meta-event: any event not marked as inactive in the project. Aggregates UI interactions, clicks, page views and business events. |
| `_new` | Amplitude meta-event: a user's first event in the project. Used to count new users. |
| `session_id` | Unique session identifier generated by the Amplitude SDK. |
| `user_id` | Unique user identifier, defined by the application. |
| `get.lana.help` | Lana's production domain — the only domain considered in all metrics. |
| `uniques` | Amplitude metric: deduplicated count of unique users in a period. |
| `totals` | Amplitude metric: total count of event occurrences (not unique users). |
| Rolling window | Sliding window — the period moves daily, always covering the last N days. |
| N-Day Retention | Retention exactly on day N — different from rolling retention (which counts users who returned on day N or any earlier day). |
| Segment | Filter applied at the user/session level in Amplitude. |
| Event filter | Filter applied at the property level of a specific event. |

---

*Lana · Phygtl, Inc. · Internal document · Not for external distribution · v1.0 · July 23, 2026*
