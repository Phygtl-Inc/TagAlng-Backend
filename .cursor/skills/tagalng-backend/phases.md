# TagAlng phases — backend deliverables

## Phase 1 · Pre-release

**Goal:** Signups + territory mapping (Lake Nona first cluster).

| Backend item | Notes |
|--------------|-------|
| Postgres + PostGIS + pgvector enabled | region `us-east` |
| `blocks`, `block_state`, `waitlist_signups` | geocode zip → candidate `block_id` |
| `cohorts.yaml` mirror table or seed | drift = silent failure |
| Waitlist API | phone optional, city, cohorts[], reCAPTCHA |
| Atlas ticker | LISTEN/NOTIFY → Supabase Realtime per block |
| Analytics | `install`, `cohort_inbound`, `signup_initiated`, `signup_complete`, `block_vote_count` |
| RLS + `audit_log` foundation | inclusive-only API shape from day 1 |

**Success:** ticker moving on multiple blocks; ≥2 blocks with density signal; no bot OTP spend.

---

## Phase 2 · Activation (GTM may say "Founding Moms" — backend is cohort-agnostic)

**Goal:** Prove identity → scene → fellows → RSVP → IRL.

| Backend item | Notes |
|--------------|-------|
| Identity worker (Cloud Run) | cover → Flash → claims + embedding |
| Scene activation | rules + centroid; **no** runtime LLM v1 |
| Host-prompt assist | Pro via worker; does not create event rows |
| `events`, `rsvps`, `threads` | auto-thread per event; broadcast tier only |
| Fellows endpoint | top 5–7 by overlap × block × time |
| Block machine | `waitlist → racing → live`; enforce 20+5 server-side |
| Auth | Twilio OTP at RSVP / refine / host only |
| Map data API | fellows + events for block (geo-fenced queries) |
| Analytics | `cover_search`, `scene_activated`, `cohort_combination`, `fellow_viewed`, `event_rsvp`, `event_checkin`, `connection_made`, block events |
| 5% holdout | per analytics handover |

**Not in Phase 2:** native app, FCM, presence, paid events, LDE auto-activation, generative feed.

**Success:** first block hits Day Zero (20+5); North Star measurable.

---

## Phase 3 · MVP

**Goal:** One block holds Day Zero ≥4 weeks; friendship loop real.

| Backend item | Notes |
|--------------|-------|
| FCM | thread reply, RSVP, block-unlock |
| Realtime presence | check-in scoped, opt-in, time-windowed |
| Thread tiers | broadcast → paid → checked-in |
| Refinement chips API | narrow without scene swap |
| LDE v0 | weekly HDBSCAN; **report only** |
| Moderation pipeline | Flash → queue; human decides |
| Admin RBAC | Phygtl full; `role_investor` + BQ-backed views |
| BigQuery ETL | nightly Supabase → materialized investor tables |
| New events | `identity_claim_edited`, `identity_claim_dismissed`, `friendship_compound`, `cluster_open`, `protected_attribute_redaction_attempt` |

**Success:** D14 ≥40%; investor dashboards clean; app submitted.

---

## Post-MVP

See `tagalong-agentic-spec.pdf` — ADK agents, Mem0, GNN matching, edge LLM, not backend MVP scope.
