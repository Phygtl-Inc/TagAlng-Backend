# TagAlng PWA — v0.1 backend handoff

**Audience:** PWA / frontend developers integrating against **tagalng-dev**.  
**Last updated:** 2026-05-29  
**Scope:** Events, request-to-join (RTJ), thread activity log, nudges, peers atlas, identity claim editor, storage for avatars/event covers. **No chat** in v0.1 — only `thread_events` (append-only activity).

For **auth + identity intake** (cover → claims), see also [`docs/FRONTEND_API.md`](./FRONTEND_API.md).

---

## 1. What we shipped (summary)

| Area | Status on dev | Notes |
|------|----------------|-------|
| Phone OTP auth | Live | Test number `+15550000000` / OTP `000000` |
| Home block assignment | Live | `get_blocks_near_zip` (ZIP) → `assign_home_block` (GPS or `p_block_id` + `p_home_zip`) |
| Identity intake (cover) | Live | Cloud Run worker, not Supabase RPC |
| Identity claim editor | Live | label, disclosure, dismiss RPCs |
| Cluster events list | Live | Anon + signed-in; 14-day window |
| Create / update / cancel event | Live | Host only; phone-verified to create |
| Request to join | Live | Phone-verified requester; host decides |
| Thread activity | Live | Auto-written on RTJ; read via RPC |
| Nudges | Live | 5/day, 7-day pair cooldown |
| Peers on map | Live | Anon = blurred placeholders |
| Peer profile card | Live | Anon blurred; auth shows public claims |
| Event reports | Live | `report_event` → `event_reports` table |
| AI event from description | Stub | Returns `wire_to_aki_pipeline` |
| Storage: avatars, event-covers | Live | Public read; owner/host write |
| i18n event copy | Partial | `title_translations` / `description_translations` on `events`; user `locale` on `users` |

**Product rules (unchanged):**

- Supabase Postgres is source of truth; **RLS** enforces privacy.
- **Never** store race, exact age, sex, or street-level address in the client payload or DB.
- **Do not** generate events via AI in v0.1 UI — host creates events; `create_event_from_description` is a placeholder.
- Launch wedge is Lake Nona (`cluster_id = 'lake-nona'`) but schema is cluster-scoped.

---

## 2. Environments

| Item | Value |
|------|--------|
| Supabase project | **tagalng-dev** |
| API URL | `https://rjlcyvwogmfmngemhbmn.supabase.co` |
| Identity worker | `https://tagalng-identity-worker-975128128744.us-east1.run.app` |
| Anon key | Supabase Dashboard → Project Settings → API → `anon` `public` (do not commit) |

**Migrations applied on dev (v0.1 slice):**

| Migration | Contents |
|-----------|----------|
| `20260529000000_phase3_events_rtj_nudges.sql` | Tables: `events`, `event_requests`, `nudges`, `thread_events`; core RPCs |
| `20260529120000_storage_avatars_event_covers.sql` | Buckets `avatars`, `event-covers` + RLS |
| `20260529130000_phase3_remaining_rpcs.sql` | `event_reports`, peers, host RPCs, `get_my_nudges`, `report_event` |

Earlier phases (blocks, users, identity claims, waitlist, etc.) were already on dev before this slice.

---

## 3. How to call Supabase from the PWA

Use `@supabase/supabase-js` with the **anon** key. Every REST/RPC request needs:

```http
apikey: <SUPABASE_ANON_KEY>
Authorization: Bearer <access_token_or_anon_key>
Content-Type: application/json
```

- **Visitor / anon:** `Authorization: Bearer <anon_key>` (same value as `apikey`).
- **Signed-in:** `Authorization: Bearer <user access_token>` from `supabase.auth.getSession()`.

If you see `No API key found in request`, the `apikey` header is missing or `anon_key` is empty in Postman/env.

```ts
const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// RPC example
const { data, error } = await supabase.rpc('get_cluster_events', {
  p_cluster_id: 'lake-nona',
  p_locale: 'en',
});
```

PostgREST URL pattern (for debugging):  
`POST {SUPABASE_URL}/rest/v1/rpc/{function_name}`

---

## 4. Auth & gating matrix

| Action | Auth | Phone verified (`users.phone_verified_at`) |
|--------|------|---------------------------------------------|
| `get_cluster_events` | Anon OK | — |
| `get_cluster_peers`, `get_peer_profile` | Anon OK (blurred) | — |
| `assign_home_block`, profile, claims read | Signed-in | — |
| `create_event` | Signed-in | **Required** |
| `request_to_join_event` | Signed-in | **Required** |
| RTJ host actions (`decide_event_request`) | Signed-in host | — |
| `send_nudge`, `get_my_nudges` | Signed-in | — |
| Identity intake (worker) | Bearer access token | — |
| Storage upload `avatars` | Signed-in | — |
| Storage upload `event-covers` | Signed-in host of event | — |

`phone_verified_at` is set by a DB trigger when `auth.users.phone_confirmed_at` updates after OTP verify. If RTJ insert fails in dev, confirm B2 verify ran and the user row has `phone_verified_at` set.

---

## 5. Recommended user flows (PWA)

### 5.1 Visitor (unsigned)

1. Load cluster map / list: `get_cluster_events({ p_cluster_id: 'lake-nona', p_locale })`
2. Teaser peers: `get_cluster_peers({ p_cluster_id: 'lake-nona' })` → rows with `is_blurred: true`, null ids
3. Optional peer tap: `get_peer_profile({ p_user_id })` → blurred shell when anon
4. Prompt sign-in for RTJ, host, nudge, identity

### 5.2 Sign-in → block → identity (existing)

1. `signInWithOtp` / `verifyOtp` (test phone above)
2. `assign_home_block({ p_lat, p_lng })` or `{ p_block_id }`
3. `POST /identity/intake` on identity worker (see `FRONTEND_API.md`)
4. `get_my_identity_claims()` for claim stack UI

### 5.3 Signed-in — host an event

1. `create_event({ p_fields })` → returns `event_id` (uuid)
2. Optional cover image: upload to Storage (below), then `update_event` with `cover_image_url`
3. Manage requests: host calls `decide_event_request`
4. Activity feed: `get_thread_events({ p_event_id })`
5. Cancel: `cancel_event({ p_event_id })`

### 5.4 Signed-in — request to join

1. Pick event from `get_cluster_events`
2. `request_to_join_event({ p_event_id, p_message })` → `request_id`
3. Wait for host; poll or subscribe (Realtime not wired yet — poll `get_thread_events` or requests if you add a list RPC later)
4. Cancel own request: `cancel_event_request({ p_request_id })`

**RTJ needs two accounts in testing:** one user hosts (`create_event`), another requests (`request_to_join_event`), host approves (`decide_event_request`).

### 5.5 Nudges

1. `send_nudge({ p_recipient_id })` — max **5 per sender per day**, **1 per pair per 7 days**
2. Inbox: `get_my_nudges({ p_direction: 'received' })` or `'sent'`

### 5.6 Identity claim editor

- `update_identity_claim_label({ p_claim_id, p_label, p_synonyms })`
- `update_identity_claim_disclosure({ p_claim_id, p_disclosure })` — enum: **`public` | `mutual` | `private`** (DB today; AI spec may say `always`/`never` later)
- `dismiss_identity_claim({ p_claim_id })`

---

## 6. RPC reference (v0.1)

All bodies are JSON. Errors use Postgres `P0001` with message = machine code (see [`docs/error_codes.md`](./error_codes.md)).

### Events & cluster

#### `get_cluster_events(p_cluster_id, p_window?, p_locale?)`

- **Grant:** `anon`, `authenticated`
- **Defaults:** `p_window = '14 days'`, `p_locale = 'en'`
- **Returns:** open events in cluster, `starts_at` within window, ordered by start time
- **Fields:** `id`, `host_id`, `title`, `description`, `starts_at`, `ends_at`, `location` (geography), `venue_name`, `cohort_tags`, `max_attendees`, `status`
- **i18n:** prefers `title_translations->>p_locale` / `description_translations->>p_locale` when present

#### `create_event(p_fields jsonb)` → `uuid`

- **Grant:** `authenticated` (+ phone verified via RLS on insert)
- **`p_fields` keys:**

| Key | Required | Notes |
|-----|----------|--------|
| `lat`, `lng` | Yes | Stored as PostGIS point (no street in DB) |
| `title` | Yes | 1–80 chars |
| `description` | No | ≤500 chars |
| `cluster_id` | No | default `lake-nona` |
| `block_id` | No | H3 block id string |
| `starts_at` | No | default now + 7 days |
| `ends_at` | No | |
| `venue_name` | No | |
| `cohort_tags` | No | string array |
| `max_attendees` | No | 1–200 |
| `cover_image_url` | No | public URL after storage upload |

#### `update_event(p_event_id, p_fields)` → void

- Host only (RLS). Partial update via provided keys in `p_fields`.

#### `cancel_event(p_event_id)` → void

- Sets `status = 'cancelled'`.

#### `create_event_from_description(p_text)` → uuid

- **Stub:** always raises `wire_to_aki_pipeline`. Do not ship in UI until Aki pipeline is wired.

### Request to join

#### `request_to_join_event(p_event_id, p_message?)` → `uuid`

- Requester ≠ host; event must be `open`
- Unique per `(event_id, requester_id)` — duplicate → `request_already_exists`

#### `decide_event_request(p_request_id, p_decision)` → void

- `p_decision`: `'approved'` | `'declined'`
- Host only (RLS)
- Writes `thread_events` via trigger

#### `cancel_event_request(p_request_id)` → void

- Requester sets status `cancelled`

### Thread (activity log, not chat)

#### `get_thread_events(p_event_id)`

- **Returns:** rows from `thread_events` (newest first)
- **Visible to:** event host or approved/attended requester
- **`event_type` values:** `request_sent`, `request_approved`, `request_declined`, `request_cancelled`, `request_attended`, `request_changed`, `host_update`, `event_updated`, `event_cancelled`, `check_in`, `reminder`
- **`payload`:** jsonb (e.g. requester_id, message, status transitions)

### Peers

#### `get_cluster_peers(p_cluster_id)`

- **Grant:** `anon`, `authenticated`
- **Anon:** up to 10 placeholder rows (`user_id` null, `is_blurred: true`)
- **Auth:** real users in cluster (excludes self), `match_score`, `total_threads`, `is_blurred: false`

#### `get_peer_profile(p_user_id)` → `jsonb`

- **Anon:** blurred shell, empty claims
- **Auth:** `nickname`, `avatar_url`, `public_claims[]`, `shared_claim_count`, `upcoming_shared_events[]`
- **Note:** peer claims filtered to `disclosure = 'public'` in DB

### Nudges

#### `send_nudge(p_recipient_id)` → `uuid`

#### `get_my_nudges(p_direction?)` → table

- `p_direction`: `'received'` (default) | `'sent'`
- Columns: `id`, `other_user_id`, `nickname`, `avatar_url`, `sent_at`, `shared_count` (0 placeholder in v0.1)

### Reports

#### `report_event(p_event_id, p_reason)` → `uuid`

- Inserts `event_reports` (status `open`). Slack/webhook not wired yet.

### Profile / block (phase 2 — still required)

| RPC | Notes |
|-----|--------|
| `get_blocks_near_zip({ p_zip })` | Nearby blocks for ZIP picker |
| `assign_home_block({ p_lat, p_lng })` or `{ p_block_id, p_home_zip }` | Saves `home_block_id` + optional `home_zip` |
| `get_my_profile()` | jsonb profile + block |
| `get_my_identity_claims()` | claim stack for editor |

---

## 7. Data model (tables you care about)

### `events`

- `status`: `open` | `cancelled` | `completed`
- `location`: geography point (4326) — use lat/lng from RPC responses; do not reverse-geocode to street in product UI for v0.1
- Public **select** on `open` events; host sees all statuses; approved attendees see their events

### `event_requests`

- `status`: `pending` | `approved` | `declined` | `cancelled` | `attended`
- `decided_at` set automatically when host approves/declines

### `thread_events`

- Append-only log per event; populated by triggers on `event_requests`

### `nudges`

- Pairwise; rate limits enforced on insert

### `event_reports`

- Moderation queue; reporter can read own rows; `founder_role = 'internal'` can read all

### `users` (extensions in v0.1)

- `profile_photo_url`, `phone_verified_at`, `locale` (`en`|`pt`|`es`), `founder_role` optional

---

## 8. Storage (images)

| Bucket | Path pattern | Who can write |
|--------|----------------|---------------|
| `avatars` | `{user_id}/{filename}` | Owner (`auth.uid()`) |
| `event-covers` | `{event_id}/{filename}` | Event host only |

- Public read, max **2MB**, types: jpeg, png, webp
- After upload, set `users.profile_photo_url` or `events.cover_image_url` to the public URL

```ts
await supabase.storage.from('avatars').upload(`${userId}/avatar.webp`, file, {
  upsert: true,
  contentType: 'image/webp',
});
const { data } = supabase.storage.from('avatars').getPublicUrl(`${userId}/avatar.webp`);
```

---

## 9. Postman (QA)

Import:

1. [`docs/postman/TagAlng-tagalng-dev.postman_environment.json`](./postman/TagAlng-tagalng-dev.postman_environment.json)
2. [`docs/postman/TagAlng-v01-core.postman_collection.json`](./postman/TagAlng-v01-core.postman_collection.json) — **v0.1 events / RTJ / nudges**
3. [`docs/postman/TagAlng-Full-Flow.postman_collection.json`](./postman/TagAlng-Full-Flow.postman_collection.json) — auth + identity worker

**Required:** select environment **TagAlng — tagalng-dev** and paste **`anon_key`** from the dashboard.

**Order (v0.1 collection):** `A1` → `B1` → `B2` → `C1`–`C3` → visitor `D*` → host `F*` → second user `G*` → `H*` / `I*`

Env vars auto-filled by tests: `access_token`, `block_id`, `event_id`, `request_id`, `claim_id`, `peer_user_id`.

---

## 10. Errors

Backend raises exceptions with `errcode = 'P0001'` and `message` = stable code. Full list: [`docs/error_codes.md`](./error_codes.md).

Common ones for PWA:

| Code | When |
|------|------|
| `not_authenticated` | Missing/invalid JWT |
| `phone_not_verified` | RTJ or create event without verified phone |
| `event_not_open` | RTJ on closed/cancelled event |
| `host_cannot_request_own_event` | Self-RTJ |
| `request_already_exists` | Duplicate RTJ |
| `nudge_rate_limit_daily` | >5 nudges/day |
| `nudge_cooldown_pair` | Same pair within 7 days |
| `location_required` / `title_required` | Bad `create_event` payload |
| `peer_not_found` | Invalid `p_user_id` |
| `wire_to_aki_pipeline` | AI create-event stub |

Map UI copy via i18n; do not expose raw Postgres errors to users.

---

## 11. Not in v0.1 (do not assume)

- Chat / DMs on threads
- Realtime subscriptions (can poll RPCs for now)
- `create_event_from_description` (Aki / Vertex host-assist pipeline)
- Push notifications for nudges or RTJ decisions
- Slack/email on `report_event`
- OTP on every page (only required for host + RTJ paths)
- Auto-approve RTJ (`events.auto_approve` column exists, default `false` — not used in RPCs yet)
- Mutual disclosure logic in `get_peer_profile` (only `public` claims shown today)

---

## 12. Identity extraction (separate service)

Cover → claims is **not** a Supabase RPC. Use the identity worker:

- `POST /identity/intake` — clarify loop
- `POST /identity/extract` — one-shot

Requires `Authorization: Bearer <access_token>` and `assign_home_block` first.

**Known spec drift (Job 1):** AI target schema may use `disclosure: always|mutual|never` and `tone` as color enums; DB today uses `public|mutual|private`. Align UI labels with DB until a migration unifies vocabulary. See internal notes on cover-extraction golden fixtures.

---

## 13. Related repo docs

| Doc | Purpose |
|-----|---------|
| [`docs/FRONTEND_API.md`](./FRONTEND_API.md) | Auth, identity worker, phase 2 RPCs |
| [`docs/error_codes.md`](./error_codes.md) | Machine-readable error codes |
| [`docs/pwa/tagalng_pwa_visitor_vs_signedin_v8.svg`](./pwa/tagalng_pwa_visitor_vs_signedin_v8.svg) | UX map: visitor vs signed-in (product; not API spec) |
| `.cursor/skills/tagalng-backend/pwa-v01-visual-tpr.md` | Screen → RPC mapping from SVG |

---

## 14. Questions / contacts

- **Supabase / RPC / RLS / migrations:** backend repo owners  
- **Identity worker / Vertex:** `services/identity-worker/` + deploy scripts in repo  
- **Anon keys & Twilio:** request via backend; never commit secrets  

For implementation questions on this slice, reference migration files under `supabase/migrations/20260529*.sql` as source of truth.
