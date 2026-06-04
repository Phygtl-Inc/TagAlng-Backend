# Frontend API guide (Postman + React Native)

How to test **tagalng-dev** end-to-end: Supabase Auth → home block → identity intake → read claims.

**v0.1 events / RTJ / nudges / peers:** see [`PWA_V01_BACKEND_HANDOFF.md`](./PWA_V01_BACKEND_HANDOFF.md).

Use the Postman files in this folder together with the **deployed** identity worker and hosted Supabase.

---

## What talks to what

| System | Base URL | Used for |
|--------|----------|----------|
| **Supabase** | `https://rjlcyvwogmfmngemhbmn.supabase.co` | Phone OTP, RPCs (`get_blocks_near_zip`, `assign_home_block`, `get_my_identity_claims`, events/RTJ) |
| **Identity worker** | `https://tagalng-identity-worker-975128128744.us-east1.run.app` | `POST /identity/intake`, `POST /identity/extract` (legacy one/two-shot intake) |
| **Lana worker** | `https://tagalng-lana-worker-s5gmxb6whq-ue.a.run.app` | **Profile:** `purpose: profile_intake` · **Host event:** `purpose: event_draft` → `create_event` on complete — see [`LANA_API.md`](./LANA_API.md) |

The mobile app uses the **same split**: Supabase client for auth + Postgres RPCs; `fetch` to **Lana worker** for profile intake and **host event** chat, or identity worker for legacy cover text.

---

## Dev phone OTP (no real SMS yet)

Hosted **tagalng-dev** uses **test phones** in Supabase Dashboard → Auth → Phone:

| Phone | OTP | Use |
|-------|-----|-----|
| `+15550000000` | `000000` | Generic Postman flow |
| `+15550100001` | `000000` | Seed host **Marina** (after `seed.sql`) |
| `+15550100002` | `000000` | Seed guest **Beatriz** |
| `+15550100003` | `000000` | Seed peer **Carla** |

Real SMS: Supabase Dashboard → Auth → Phone → Twilio credentials. Seed data: [`SEED_DEV_DATA.md`](./SEED_DEV_DATA.md).

---

## Postman setup (5 minutes)

1. **Import** (Postman → Import):
   - `docs/postman/TagAlng-Full-Flow.postman_collection.json`
   - `docs/postman/TagAlng-tagalng-dev.postman_environment.json`
2. Select environment **TagAlng — tagalng-dev**.
3. Set **`anon_key`**: Supabase Dashboard → Project Settings → API → `anon` `public`.
4. Confirm **`identity_worker_url`** is the Cloud Run URL (already set in the env file).
5. Run requests **in order**:
   - **Profile signup:** **A → B → C → D**
   - **Host event (Lana):** **A → B → C → F** (skip D unless you also want profile claims)
6. **ZIP path:** use **C0** → pick `block_id` → **C1b** instead of **C1** (GPS).

---

## Run order (Postman)

| Step | Request | What you get |
|------|---------|----------------|
| **A1** | Supabase health | Confirms `anon_key` works |
| **A2** | `GET {{identity_worker_url}}/health` | Worker + Vertex up |
| **B1** | Send OTP | SMS path (test number) |
| **B2** | Verify OTP | `access_token` saved to env |
| **C0** | `get_blocks_near_zip` | List blocks for ZIP (e.g. `32827`) |
| **C1** | `assign_home_block` (GPS) | Auto nearest block |
| **C1b** | `assign_home_block` (ZIP pick) | `p_block_id` + `p_home_zip` |
| **C2** | `get_my_profile` | Confirms `home_block_id`, `home_zip` |
| **A3** | `GET {{lana_worker_url}}/health` | Lana worker up |
| **D1** | `POST /lana/sessions` | Lana opening → saves `lana_session_id` |
| **D2–D3** | `POST .../messages` | Chat turns → `lana_messages` in DB |
| **D4** | `POST .../complete` | → `user_identity_claims` |
| **D5** | `get_my_identity_claims` | Verify claims in Supabase |
| **D6** | `get_lana_session_messages` | Verify chat in Supabase |
| **F0** | `get_event_purposes` | Purpose chip ids for host UI |
| **F1** | `POST /lana/sessions` `{ "purpose": "event_draft" }` | Saves `lana_event_session_id` |
| **F2** | `POST .../messages` | `ui.highlights` + `event_draft` (title, venue, cohort_tags) |
| **F2b** | Follow-up message | Only if **F2** not `ready_to_complete` |
| **F3** | `POST .../complete` `{ "publish": true }` | → `create_event` → saves `event_id` |
| **F4** | `get_cluster_events` | Verify published event |
| *(alt E)* | `POST /identity/intake` | Legacy folder **E** in Postman |

Optional **D4**: one-shot `POST /identity/extract` if cover text is very detailed (may skip clarify).

---

## Environment variables (Postman)

| Variable | Example / notes |
|----------|------------------|
| `supabase_url` | `https://rjlcyvwogmfmngemhbmn.supabase.co` |
| `anon_key` | **You must paste** from Dashboard |
| `identity_worker_url` | `https://tagalng-identity-worker-975128128744.us-east1.run.app` |
| `test_phone` | `+15550000000` |
| `test_otp` | `000000` |
| `access_token` | Filled by B2 |
| `cover_text` | Short vague text for D1 (triggers follow-ups) |
| `clarifications_json` | JSON array for D2 (see below) |
| `test_zip` | `32827` (Lake Nona) for **C0** |
| `picked_block_id` | Filled from **C0** response (first row) for **C1b** |
| `lana_worker_url` | Lana Cloud Run URL (set in env file) |
| `lana_event_session_id` | Filled by **F1** |
| `lana_event_message` | Brunch host story for **F2** (see env file) |
| `event_id` | Filled by **F3** when `published: true` |

---

## Host event via Lana (summary)

Same Lana endpoints as profile intake; different `purpose` and response shape. Full spec: [`LANA_API.md` — Event draft](./LANA_API.md#event-draft-host-an-event).

1. **Prerequisites:** OTP (**B**), `assign_home_block` (**C1** or **C1b**).
2. `POST {{lana_worker_url}}/lana/sessions` → `{ "purpose": "event_draft" }`.
3. `POST .../sessions/{id}/messages` → user describes event; read `event_draft` + `ui.highlights`.
4. `POST .../sessions/{id}/complete` → `{ "force": false, "publish": true }` → `event_id`, `event_draft`, `published`.
5. Purpose chips: `get_event_purposes()` RPC; Lana suggests ids in `event_draft.cohort_tags`.

If complete returns `keep_chatting_or_set_force_true`, send **F2b** or set `"force": true`. If `phone_not_verified`, use `"publish": false` and call `create_event` manually after OTP.

---

## React Native (same calls as Postman)

### 1. Supabase client

```ts
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(
  'https://rjlcyvwogmfmngemhbmn.supabase.co',
  ANON_KEY, // from env / EAS secrets
);
```

### 2. Sign in (test phone)

```ts
await supabase.auth.signInWithOtp({ phone: '+15550000000' });
// user enters 000000
await supabase.auth.verifyOtp({
  phone: '+15550000000',
  token: '000000',
  type: 'sms',
});
const { data: { session } } = await supabase.auth.getSession();
const accessToken = session!.access_token;
```

### 3. Assign home block (required before identity)

```ts
const { data, error } = await supabase.rpc('assign_home_block', {
  p_lat: 28.3685,
  p_lng: -81.2762,
});
// or p_block_id: 'existing-block-id'
```

### 4. Identity intake (Cloud Run)

```ts
const IDENTITY_URL =
  'https://tagalng-identity-worker-975128128744.us-east1.run.app';

// First message
const res1 = await fetch(`${IDENTITY_URL}/identity/intake`, {
  method: 'POST',
  headers: {
    Authorization: `Bearer ${accessToken}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    cover_text: "I'm new in Lake Nona. Catholic family.",
  }),
});
const intake1 = await res1.json();

if (intake1.status === 'clarify') {
  // Show intake1.questions in UI, collect answers, then:
  const res2 = await fetch(`${IDENTITY_URL}/identity/intake`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      cover_text: "I'm new in Lake Nona. Catholic family.",
      clarifications: [
        {
          question_id: intake1.questions[0].id,
          question: intake1.questions[0].prompt,
          answer: 'Two toddlers, ages 2 and 4.',
        },
      ],
    }),
  });
  const intake2 = await res2.json(); // status: "complete"
}

// Read saved threads (Supabase, not worker)
const { data: claims } = await supabase.rpc('get_my_identity_claims');
```

### 5. One-shot extract (optional)

```ts
await fetch(`${IDENTITY_URL}/identity/extract`, {
  method: 'POST',
  headers: {
    Authorization: `Bearer ${accessToken}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    cover_text: 'Long detailed cover paragraph...',
  }),
});
```

---

## Identity worker API reference

All `/identity/*` routes require:

```http
Authorization: Bearer <supabase_access_token>
Content-Type: application/json
```

### `GET /health`

No auth. Use for connectivity checks.

### `POST /identity/intake`

**Body:**

```json
{
  "cover_text": "string (8–4000 chars)",
  "clarifications": [
    {
      "question_id": "kids_ages",
      "question": "How many kids and what ages?",
      "answer": "Two toddlers, 2 and 4."
    }
  ]
}
```

**Responses:**

- `status: "clarify"` — show `questions[]` and `assistant_message`; call again with `clarifications`.
- `status: "complete"` — claims extracted, embedded, saved; `claims[]` returned.

### `POST /identity/extract`

**Body:** `{ "cover_text": "..." }`  
**Response:** `{ "user_id", "claims", "threads_found", "mode" }` — no clarify step.

### Common errors

| HTTP | Detail | Fix |
|------|--------|-----|
| 401 | `invalid_session` | New OTP login; refresh token |
| 400 | `home_block_required` | Run `assign_home_block` first |
| 502 | `vertex_permission_denied` | Backend/GCP issue — ping backend |
| 503 | `vertex_not_configured` | Worker env misconfigured |

---

## Supabase RPC reference

Headers for REST/RPC (Postman uses these):

```http
apikey: {{anon_key}}
Authorization: Bearer {{access_token}}
Content-Type: application/json
```

| RPC | Body (example) |
|-----|----------------|
| `get_blocks_near_zip` | `{ "p_zip": "32827", "p_cluster_id": "lake-nona", "p_limit": 10 }` |
| `assign_home_block` | GPS: `{ "p_lat", "p_lng" }` · ZIP: `{ "p_block_id", "p_home_zip" }` |
| `get_my_profile` | `{}` |
| `get_my_identity_claims` | `{}` |

Post URL pattern: `{{supabase_url}}/rest/v1/rpc/<function_name>`

---

## `clarifications_json` for Postman D2

Edit env var `clarifications_json` after D1 returns questions:

```json
[
  {
    "question_id": "kids_ages",
    "question": "How many kids do you have, and what ages?",
    "answer": "Two toddlers, ages 2 and 4."
  }
]
```

Use the real `id` / `prompt` from D1’s `questions` array.

---

## App env vars (EAS / `.env`)

```bash
EXPO_PUBLIC_SUPABASE_URL=https://rjlcyvwogmfmngemhbmn.supabase.co
EXPO_PUBLIC_SUPABASE_ANON_KEY=<from dashboard>
EXPO_PUBLIC_IDENTITY_WORKER_URL=https://tagalng-identity-worker-975128128744.us-east1.run.app
```

Never put **service role** or GCP keys in the app.

---

## Questions?

- Backend / GCP / Supabase: backend team  
- Product flow: Tommaso  
- This repo: `services/identity-worker/README.md`, `./scripts/deploy-identity-worker.sh`
