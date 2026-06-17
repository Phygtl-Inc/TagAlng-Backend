---
name: tagalng-backend
description: >-
  Builds TagAlng backend on Supabase Postgres, PostGIS/H3, pgvector, Cloud Run,
  and Vertex AI. Use for TagAlng, Tagalong, Phygtl, Social Agentics, block unlock,
  identity claims, waitlist, atlas ticker, fellows matching, RLS, cohorts.yaml,
  PWA visitor vs signed-in v0.1 visual TPR, events, request-to-join, nudges,
  thread_events, or any backend/API/migration work in this repo.
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

## Core loop (Phase 2+ target — not all shipped)

```
cover input → Gemini Flash → identity_claims + embeddings
→ scene activation (rules + centroid, no runtime LLM v1)     [NOT BUILT]
→ fellows + events on block → RTJ → thread_events → IRL      [PARTIAL: see architecture-alignment.md]
```

**v0.1 shipped today:** identity intake, blocks (GPS/ZIP), events, RTJ, nudges, peers map, `thread_events` (no chat). **Do not** treat `Tagalong-Architecture-Diagram.svg` as a checklist of live APIs.

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
- `events` — host_id, block_id, starts_at (user-created only)  
- `event_requests` — RTJ (replaces RSVP in v0.1)  
- `thread_events` — system activity log (no `thread_messages` in v0.1)  
- `nudges`, `event_reports`  
- `waitlist_signups` — cohorts[], candidate_block_id  
- `audit_log` — sensitive reads / policy violations  
- `analytics_events` — name, props (hashed where spec requires)  

## North Star

**Recurring weekly fellows per LIVE block** — instrument `event_checkin`, `connection_made`, `friendship_compound` (derived).

## PWA v0.1 visual TPR (read for surface → API mapping)

**Diagram:** `docs/pwa/tagalng_pwa_visitor_vs_signedin_v8.svg` — visitor (read-only) vs **7 auth triggers** vs signed-in (RTJ, nudge, Threads activity log, host). **Not chat in v0.1.**

**Full agent reference:** [pwa-v01-visual-tpr.md](pwa-v01-visual-tpr.md)

Ground truth with live app: https://app.tagalng.com/ · backend migrations: `Azjit_Backend_Brief_v01.pdf`.

## Canonical docs (read when unsure)

| Doc | Path |
|-----|------|
| PWA visitor vs signed-in (v8) | `docs/pwa/tagalng_pwa_visitor_vs_signedin_v8.svg` + [pwa-v01-visual-tpr.md](pwa-v01-visual-tpr.md) |
| v0.1 backend brief (SQL/RPC) | `Azjit_Backend_Brief_v01.pdf` |
| CTO spec (ship list) | `Tagalong-CTO-Spec.pdf` |
| R&D kickoff | `TagAlng-RD-Kickoff.pdf` |
| Agentic manifesto (post-MVP) | `tagalong-agentic-spec.pdf` |
| Architecture (target) | `Tagalong-Architecture-Diagram.svg` |
| Diagram vs shipped (v0.1) | [architecture-alignment.md](architecture-alignment.md) |

## Execution defaults

- Prefer **Supabase migrations + RLS** over app-only checks  
- Prefer **RPC / Edge Function / Cloud Run** for Gemini and webhooks — not client-side secrets  
- Minimize scope: one migration + one policy + one test path per PR  
- Do not commit secrets; use env for Supabase, GCP, Twilio, Maps, reCAPTCHA  

## Lana Layer 1 routing (non-negotiable)

**Use AI (Vertex Flash via `discovery_slots.ai_parse_discovery_turn`) for open-ended user intent.**  
Classify by meaning: find neighbors, heritage filters, swap/meet/tip, block browse — infinite phrasing.

| Do | Don't |
|----|-------|
| Strengthen `_SYSTEM` prompt in `discovery_slots.py` when routing misfires | Add regex word lists (`italian`, `brazilian`, …) to route intents |
| Let `enrich_slots` trust AI `linear_intent` + `attr_filter` when confidence met | Override AI with `phrase_linear_intent` for discovery/swap/tip |
| Keep `phrase_linear_intent` for **policy-only** cases: profile ack, remove-claim, change name, block log vs marketplace | Use `find a?` / `need a?` tip patterns that steal "find italian moms" |

**Regex is allowed only for:** ZIP extraction, block-log vs marketplace browse disambiguation (`is_block_activity_browse`), structural acks (`ok that's me`), claim-edit safety (`remove`/`delete`).

When user says "find italian dads" → AI sets `discovery.find_by_attrs` + `attr_filter: "italian dads"`, **not** `looking.tip`.

**Neighbor questions (preview phase):** "show Kashaf's claims" → `discovery.show_peer_profile` + `get_peer_profile` RPC. "How is 100% match?" → `discovery.explain_peer_match` — never re-list `format_peer_matches`. Do not use bare `show me` regex for peer routing.

**Signals (swap/meet/tip):** AI classifies `goal=save_signal` + `signal_intent` in `discovery_slots` — not phrase regex. Lock `signal_draft` during category/stage confirm (don't let AI re-classify "food" as `tip_share`). After save, always surface existing block-log matches for that intent family, not only `matches_created` on this row.

Files: `services/lana-worker/app/discovery_slots.py` (router), `layer1_intents.py` (catalog + enrich), `discovery_route.py` (handlers).

## Detail

- Phase ship lists + investor events: [phases.md](phases.md)  
- Invariants + negative space: [invariants.md](invariants.md)  
- Ticket-style breakdown: [tickets.md](tickets.md)  
- Architecture diagram vs tagalng-dev reality: [architecture-alignment.md](architecture-alignment.md)
