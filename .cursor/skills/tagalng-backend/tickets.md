# TagAlng backend — ticket templates

Use as copy-paste epics; split per PR (migration + RLS + one API path).

## EPIC P1-DB · Supabase foundation

- Create project `us-east`, enable `postgis`, `vector`  
- Migration `001_blocks.sql` — blocks, block_state enum, indexes on h3  
- Migration `002_waitlist.sql` — waitlist_signups, triggers for atlas count  
- Migration `003_audit_rls.sql` — audit_log, baseline RLS, service role policy  
- Seed `cohorts` from `cohorts.yaml` (when file exists in repo)  

## EPIC P1-GEO · Vicinity

- Cloud Run or Edge: geocode → H3 res 10/11  
- Store `candidate_block_id` on waitlist; never store full street in waitlist row  

## EPIC P1-WAITLIST · Signup path

- POST `/waitlist` — validate cohorts against yaml, reCAPTCHA, insert  
- Realtime channel `atlas:{block_id}` — payload `{ count, state }`  

## EPIC P1-ANALYTICS · Events

- `analytics_events` table or external sink adapter  
- Emit Phase 1 events per `05_analytics_data.md` taxonomy  

## EPIC P2-IDENTITY · Claims plane

- `user_identity_claims` + ivfflat/hnsw index  
- Cloud Run `identity-worker`: Flash extract → embed → upsert  
- POST `/identity/extract` — authenticated or session token  

## EPIC P2-SCENE · Activation

- Scene centroids table or Sanity mirror  
- POST `/scene/activate` — deterministic routing, log `scene_activated`  

## EPIC P2-SOCIAL · Events + threads

- `events`, `rsvps`, `threads`, `thread_messages`  
- Trigger: on RSVP confirm → create thread membership  
- GET `/block/:id/feed` — fellows + events for scene  

## EPIC P2-BLOCK · State machine

- RPC `transition_block_state` — enforce thresholds from config table  
- Emit `block_state_transition`, `block_unlocked`  

## EPIC P2-AUTH · OTP

- Supabase Auth + Twilio hook on RSVP/host/refine routes only  

## EPIC P3-OPS · MVP ops plane

- LDE Cloud Run job (weekly) → `lde_reports` table  
- Moderation queue + Flash classifier worker  
- BigQuery ETL job + investor views + `role_investor` RLS  

---

## PR sizing (fast build)

| Size | Contains |
|------|----------|
| S | one migration + RLS for one table |
| M | migration + RPC + single integration test |
| L | Cloud Run worker + DB + wire one client call |

Avoid L epics without S/M landing first.
