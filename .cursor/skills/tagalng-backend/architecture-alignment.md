# Architecture diagram vs shipped backend (tagalng-dev)

**Diagram:** `Tagalong-Architecture-Diagram.svg` at repo root = **target** system.  
**Shipped v0.1:** this repo + tagalng-dev migrations. **Do not** assume every diagram box is live.

Re-check after major migrations. Last aligned: 2026-06-02.

---

## Spine (aligned)

| Flow | Status |
|------|--------|
| PWA/RN → Supabase (auth, RPCs, storage) | Live |
| PWA/RN → identity-worker (`/identity/intake`, `/identity/extract`) | Live |
| identity-worker → Vertex Gemini Flash → `user_identity_claims` | Live |
| lana-worker → multi-turn chat → `lana_sessions` / `lana_messages` → complete → claims | Live (Cloud Run tagalng-dev) |
| User-created events only (no AI-generated rows) | Enforced; `create_event_from_description` stub |
| RLS + `audit_log` table | Live |
| pgvector on claims | Live |
| PostGIS block centroids | Live (placeholder H3 block ids) |

---

## Tier 1 · Surfaces (not in this repo)

| Diagram | Shipped | Gap |
|---------|---------|-----|
| Marketing site + waitlist + `?cohort=&ref=` | Partial | `join_waitlist`, leads tables; **TagAlng-Web** owns UI |
| App: cover → scene → fellows → RSVP → thread | Partial | See app loop below |
| Admin + investor BigQuery views | No | `apps/admin/` scaffold only |

### App loop (diagram vs v0.1)

| Step | Diagram | tagalng-dev |
|------|---------|-------------|
| Cover / identity | Yes | identity-worker + `get_my_identity_claims` |
| Scene activation | Yes | **Not built** (no scene router RPC) |
| Fellows on map | Yes | **Partial** — `get_cluster_peers` / `get_peer_profile`; no dedicated fellows RPC |
| Events | Yes | `get_cluster_events`, `create_event`, RTJ |
| RSVP | Yes | **`event_requests`** (RTJ), not `rsvps` |
| Thread | Yes | **`thread_events`** log only — **no** `thread_messages` / chat |

**PWA truth:** `docs/pwa/tagalng_pwa_visitor_vs_signedin_v8.svg` + [pwa-v01-visual-tpr.md](pwa-v01-visual-tpr.md).

---

## Tier 2 · Sanity

| Item | Status |
|------|--------|
| Studio `/studio`, webhook → ISR | **Not in backend repo** (`sanity/` target) |
| Mirror into Postgres | **Not built** |

---

## Tier 3 · Supabase pills

| Pill | Status | Notes |
|------|--------|-------|
| Postgres core tables | Live | users, blocks, claims, events, RTJ, nudges |
| PostGIS + H3 | Partial | PostGIS yes; block id = H3 text; **no** Maps geocode worker |
| ZIP → blocks | Live | `get_blocks_near_zip`, `zip_centroids` (dev seed zips) |
| GPS → block | Live | `assign_home_block`, `resolve_nearest_block_id` |
| pgvector | Live | identity-worker embeds |
| Auth phone OTP | Live | Supabase + Twilio; dev test phones |
| Realtime | Partial | `pg_notify` atlas on waitlist; RTJ/events **poll**, not Realtime subs |
| Storage | Live | `avatars`, `event-covers` |
| RLS + audit | Live | phase3b mutual disclosure on peers |
| `threads` in diagram label | Misleading | Use **`thread_events`** in v0.1 |

---

## Tier 4 · Google Cloud

| Box | Status |
|-----|--------|
| Gemini Flash (identity) | Live — identity-worker |
| Gemini Pro (host assist, LDE) | Stub / missing — `wire_to_aki_pipeline`; no LDE job |
| Cloud Run identity worker | Live |
| Cloud Run Sanity webhook | Not built |
| Cloud Run LDE batch | Not built |
| Maps → H3 geocode | Not built (ZIP centroids + GPS nearest block only) |
| FCM | Not built (diagram: Phase 3+) |
| BigQuery ETL | Not built |
| reCAPTCHA | Partial — `join_waitlist` expects verified flag; no verifier in repo |

---

## Agent rules

1. **Diagram = north star.** **Migrations + handoff docs = what exists today.**
2. Do not add `thread_messages`, scene LLM at runtime, or AI `INSERT` into `events` without explicit product sign-off.
3. Say **RTJ** / `event_requests`, not RSVP, in v0.1 API docs unless migrating schema.
4. Geo: prefer `assign_home_block` / `get_blocks_near_zip`; do not assume Google Maps worker exists.
5. Surfaces (Vercel) are separate repos — backend only documents RPCs and workers.

---

## Related

- [phases.md](phases.md) — phase deliverables  
- [SKILL.md](SKILL.md) — non-negotiables  
- [docs/PWA_V01_BACKEND_HANDOFF.md](../../../docs/PWA_V01_BACKEND_HANDOFF.md) — live RPCs  
- [docs/FRONTEND_API.md](../../../docs/FRONTEND_API.md) — integration order
