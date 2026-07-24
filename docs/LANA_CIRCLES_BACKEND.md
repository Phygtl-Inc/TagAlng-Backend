# Lana · Circles + Place Profile · Backend (Phase 1)

Implements the backend of `LANA_CIRCLES_ZIP_MASTER_v1` + the Place Profile extension
(place as a canonical entity). **Scope note:** the onion matcher (§C) is owned by a
separate developer and is NOT in this drop — everything else backend is. The word
"circle" is internal-only (§A.4 M7): user-facing copy names the place, never the term.
Persona-neutral throughout: the specs say "mom", the product is for any user.

## Deploy ordering (matters)

1. `supabase/migrations/20260906120000_circles_places_phase_a.sql` — additive only
   (tables `places`, `place_features`, `circle_affiliations`, `zip_unlock`,
   `circle_invites`, `circle_invite_redemptions`; columns on `users`,
   `user_identity_claims`, `rapport_gaps`, `events`; `recount_zip_unlock()` fn;
   events→places backfill). Nothing reads it until the worker deploys.
2. Worker deploy (all changes below). The worker reads `rapport_gaps.place_ref`
   and writes the new tables, so **migration first**.

All new tables have RLS enabled with **no client policies** — PostgREST hands out
nothing; every read goes through the worker. That is the §F disclosure guarantee
at the schema layer.

## What happens without any FE change

- **Capture (§H.1):** the per-turn extractor now also emits `circle_candidates` +
  `place_feature_candidates`; they persist as `status='suggested'` affiliations /
  parked feature notes. Never interrupts a turn.
- **§4.3 enrichment:** grounding a place queues one AI-authored question on the
  existing "By the way…" tile ("What do you enjoy most at {place}?"); the answer
  becomes a place-tagged identity claim (`user_identity_claims.place_ref`) via the
  existing record-answer path. Zero FE work.
- **Event anchoring:** publish now stamps `events.place_ref` (background, dual-write
  with legacy `events.place_id` text; historical events backfilled by the migration).

## Worker endpoints (all POST, Bearer auth — same conventions as /lana/rapport/*)

### Circles profile surface (§G)
- `POST /lana/circles/mine` → `{circles:[{id, circle_type, status, grounded,
  place_name, place_address, detail, member_count, active, added_at}]}`
  (own circles — always fully visible to the owner)
- `POST /lana/circles/add` `{circle_type, detail?, google_place_id?}` — grounding optional
- `POST /lana/circles/update` `{affiliation_id, detail?}`
- `POST /lana/circles/remove` `{affiliation_id}` — soft-delete; drops from matching immediately
- `POST /lana/circles/ground-options` `{affiliation_id, query?}` →
  `{options:[{name, address, google_place_id}]}` — 2-3 real nearby places for the
  "which spot — X, or somewhere else?" chips (`query` = "search for another")
- `POST /lana/circles/ground` `{affiliation_id, google_place_id}` →
  `{place_id, place_name, status:'confirmed'}` — **only the tapped id crosses the
  wire**; name/geo/address are fetched server-side from Google (a client can never
  mint or rename a canonical place)

### Invites + ZIP unlock (§A.2 · §D · §E)
- `POST /lana/invites/mint` `{circle_key?}` → `{token, url:/i/<token>}`
- `POST /lana/invites/redeem` `{token}` → `{ok, confirm_prompt, circle_type}` —
  records growth (`users.invited_by`, set once) + recounts the ZIP. Errors:
  `invite_not_found` (404), `invite_rate_limited` (429). **Invite ≠ membership**:
  the prompt hint is the TYPE only, never the inviter's place.
- `POST /lana/invites/self-confirm` `{token, circle_type, detail?}` — the joiner's
  own (ungrounded) affiliation, `source='invite_confirmed'`; she confirms it by
  grounding HER OWN place via ground-options → ground.
- `POST /lana/area/progress` `{zip?}` → `{zip5, state, count, threshold,
  is_founding_eligible, founding_earned, founding_area}` — recounts on read;
  powers the "7 of 10 to come alive" pill. Copy is FE-owned, warm,
  **no leaderboard anywhere** (§E.4), "Founding member" never "Founding Mom".

### Events (§5.2)
- `POST /lana/events/invite-suggestions` `{event_id}` → `{count, members:[{user_id,
  nickname}]}` — host-only; first names only (Stranger-tier safe per §F.1).

## State machine (§D)

`zip_unlock` rows are created/updated by `recount_zip_unlock(zip5)` (SQL,
service-role only), called from redeem + area-progress (read-repair; no cron).
"Verified active" (U2): verified by any method (`phone_verified_at`, which email
verify also stamps) + a Lana session ≤30d + ≥1 confirmed thing (circle or accepted
intro). `warming→open` stamps founding (U4, quality-gated) and fires one push to
all members. **Unlock gates consumption of others' supply only — no create/host/
invite path checks `unlock_state`, ever (§D.2).** Discovery filtering by state is
NOT yet wired into browse — that lands with the onion integration.

## Contract for the onion matcher (other dev)

- Candidates: `circle_affiliations` where `status='confirmed' and dismissed_at is
  null` (partial index `circle_affiliations_place_confirmed_idx` matches exactly
  this predicate). `place_ref` → `places.id`; score on FK equality.
- Shared affinities: existing `user_identity_claims` vectors, unchanged shape;
  place-tagged rows now carry `place_ref` (bonus signal: same-place affinity).
- Disclosure (§F): strip `places.*` below Direct — recommend simply not joining
  `places` for below-Direct results. Open question O7 (what "Direct with a place"
  means for multi-member places) still needs locking with Tommaso.

## Deliberate deviations from the spec docs

- No `zips`/`waitlist_invites` tables exist in this repo → `zip_unlock` +
  `circle_invites`(+redemptions) instead.
- `circle_affiliations` is place_ref-native (no legacy inline place columns, no
  §8 Phase B/C dual-write — the table is net-new). The only backfilled legacy is
  `events.place_id`.
- Embeddings are 768-dim (text-embedding-005), not the spec's 1536.
- `places.place_type` is advisory + nullable (O6): Google's type wins, else the
  first grounder's circle_type; never overwritten on re-grounding.
- `place_features` conflict policy: overwrite only at ≥ stored confidence;
  `source='owner'` immune to rapport writes (Phase 2 correction path).
