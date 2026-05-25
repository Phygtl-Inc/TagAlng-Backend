---
name: tagalng-backend
description: >-
  Builds TagAlng backend on Supabase Postgres, PostGIS/H3, pgvector, Cloud Run,
  and Vertex AI. Use for TagAlng, Tagalong, Phygtl, Social Agentics, block unlock,
  identity claims, waitlist, atlas ticker, fellows matching, RLS, cohorts.yaml,
  or any backend/API/migration work in this repo.
---

# TagAlng Backend

## Product (one line)

Airbnb Experiences for your block: **Identity × Vicinity × Activity**, with threads that persist after events. **For all adults** — mom/Lake Nona is GTM wedge only, not a platform limit.

## Non-negotiables (enforce in SQL/RLS/API)

| Rule | Implementation |
|------|----------------|
| Triplet | Identity + block + user-hosted activity — drop one → wrong product |
| AI is plumbing | Extract, match, moderate, LDE — **never** generate events or autonomous outreach |
| Privacy as code | RLS, audit_log, inclusive-only filters, 422 on exclude params |
| Never store | race, exact age, sex, street-level address |
| Mutual disclosure | faith, sobriety, LGBTQ+ → `disclosure: mutual` only |
| cohorts.yaml | Single source of truth — 9 cohorts + 8 sport sub-types |
| URL contract | `?cohort=parents,sports&sub=basketball&ref=POST_SLUG` end-to-end |
| Auth deferred | Phone OTP (Twilio) only on RSVP / refine / host |
| Block machine | `waitlist → racing → live \| day_zero` — thresholds config (e.g. 20+5) |
| Naming | `users`, `hosts`, `fellows` — not mom-specific schema |

## Stack (locked)

| Layer | Pick |
|-------|------|
| DB | Supabase Postgres `us-east` — PostGIS, pgvector, Realtime, Auth, Storage |
| Workers | Cloud Run — identity worker, Sanity webhooks, LDE batch |
| AI | Vertex Gemini Flash (extract, mod), Pro (host assist, LDE), text-embedding-005 |
| Geo | Google Maps geocode → H3 block id (res 10/11) |
| Analytics | Events → Postgres; Phase 3 nightly → BigQuery |
| Safety | reCAPTCHA Enterprise (waitlist + OTP paths) |

**Why Supabase:** one plane for geo + vectors + RLS + realtime + auth; vanilla Postgres exit to Cloud SQL.

Surfaces (Vercel) are not backend-owned — they read/write the same DB.

## Core loop (Phase 2+)

```
cover input → Gemini Flash → identity_claims + embeddings
→ scene activation (rules + centroid, no runtime LLM v1)
→ fellows + events on block → RSVP → auto-thread → IRL → thread persists
```

## Five AI jobs

1. Identity extraction (P2) — Flash → `user_identity_claims` + pgvector  
2. Scene activation (P2) — rules + nearest-centroid  
3. Host-prompt assist (P2) — Pro phrases event; host decides content  
4. Thread moderation (P3) — Flash → human queue, no auto-delete  
5. LDE (P3) — weekly HDBSCAN batch, ops manual activation  

**Not before MVP:** ADK agents, on-device LLM, generative recommenders, Vertex Vector Search.

## Fast-build order

Copy checklist per task; ship smallest vertical slice.

```
Phase 1 foundation:
- [ ] Supabase project + extensions (postgis, vector)
- [ ] Migrations: blocks, block_state, waitlist_signups, users stub, audit_log, cohorts mirror
- [ ] RLS policies + service role for workers only
- [ ] Geocode API: zip/address → h3_block_id
- [ ] Waitlist insert + reCAPTCHA verify
- [ ] Atlas ticker: NOTIFY → Realtime channel per block
- [ ] Analytics event sink (Phase 1 events)

Phase 2 activation:
- [ ] user_identity_claims + pgvector index
- [ ] Cloud Run identity worker (Flash + embed)
- [ ] Scene router (no LLM at runtime)
- [ ] events, rsvps, threads (broadcast tier)
- [ ] Fellows query: identity_overlap × block × time
- [ ] Block state transitions + 20+5 enforcement
- [ ] OTP hook on RSVP/host/refine

Phase 3 MVP:
- [ ] Realtime presence, thread tiers, FCM
- [ ] Moderation queue + LDE report job
- [ ] BigQuery ETL + investor RLS views
```

## Schema sketch (names only — extend in migrations)

- `blocks` — h3_id, geometry, cluster_id, state  
- `users` — auth uid, nickname, home_block_id, verified_at  
- `user_identity_claims` — tone, concept, confidence, disclosure, embedding  
- `events` — host_id, block_id, scene_id, starts_at (user-created only)  
- `rsvps`, `threads`, `thread_messages`  
- `waitlist_signups` — cohorts[], candidate_block_id  
- `audit_log` — sensitive reads / policy violations  
- `analytics_events` — name, props (hashed where spec requires)  

## North Star

**Recurring weekly fellows per LIVE block** — instrument `event_checkin`, `connection_made`, `friendship_compound` (derived).

## Canonical docs (read when unsure)

| Doc | Path |
|-----|------|
| CTO spec (ship list) | `Tagalong-CTO-Spec.pdf` |
| R&D kickoff | `TagAlng-RD-Kickoff.pdf` |
| Agentic manifesto (post-MVP) | `tagalong-agentic-spec.pdf` |
| Architecture | `Tagalong-Architecture-Diagram.svg` |

## Execution defaults

- Prefer **Supabase migrations + RLS** over app-only checks  
- Prefer **RPC / Edge Function / Cloud Run** for Gemini and webhooks — not client-side secrets  
- Minimize scope: one migration + one policy + one test path per PR  
- Do not commit secrets; use env for Supabase, GCP, Twilio, Maps, reCAPTCHA  

## Detail

- Phase ship lists + investor events: [phases.md](phases.md)  
- Invariants + negative space: [invariants.md](invariants.md)  
- Ticket-style breakdown: [tickets.md](tickets.md)
