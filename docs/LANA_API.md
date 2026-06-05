# Lana API (profile intake + event draft)

**Lana** is TagAlng’s conversational agent. She knows **what TagAlng is**, **who the user is on their block**, and **this chat’s history**.

| Purpose | On complete |
|---------|-------------|
| `profile_intake` | Saves **`user_identity_claims`** |
| `event_draft` | Returns **`event_draft`**; optionally **`create_event`** when `publish: true` |

**Team overview (share this):** [`LANA.md`](./LANA.md)

## When to call (signup order)

1. Phone OTP → Supabase session  
2. `assign_home_block` (GPS or ZIP pick)  
3. **Lana** (`lana-worker`)  
4. `get_my_identity_claims` for profile UI  

## Base URL

Deploy `services/lana-worker` to Cloud Run (separate from identity-worker). Example env var:

`lana_worker_url` = `https://tagalng-lana-worker-s5gmxb6whq-ue.a.run.app` (tagalng-dev)

## Auth

All requests:

```http
Authorization: Bearer <supabase_access_token>
Content-Type: application/json
```

Use the user JWT from OTP verify — **not** the anon key.

---

## 1. Start session

```http
POST /lana/sessions
```

**Body**

```json
{ "purpose": "profile_intake" }
```

**Response**

```json
{
  "session_id": "uuid",
  "purpose": "profile_intake",
  "status": "active",
  "assistant_message": "Hey — so glad you're here on your block...",
  "ready_to_complete": false,
  "ui": {
    "bucket": null,
    "focus_phrase": null,
    "highlights": []
  }
}
```

Lana opens with a warm welcome. Show `assistant_message` in the chat UI.

---

## 2. Send a message

```http
POST /lana/sessions/{session_id}/messages
```

**Body**

```json
{ "message": "I'm Italian, living in the USA, love pizza and family dinners." }
```

**Response**

```json
{
  "session_id": "uuid",
  "status": "continue",
  "assistant_message": "\"Latino mom\" — which corner? Mexican, Cuban, Puerto Rican...?",
  "ready_to_complete": false,
  "message_count": 3,
  "ui": {
    "bucket": "heritage",
    "focus_phrase": "Latino mom",
    "highlights": [
      { "text": "Latino mom", "bucket": "heritage" }
    ]
  }
}
```

### Frontend: per-turn highlights

| Field | Use |
|-------|-----|
| `ui.bucket` | Pill label: `HERITAGE · LANA ASKS` |
| `ui.focus_phrase` | Gold/italic quote in the question |
| `ui.highlights` | All phrases to color from the user's last message |

**Bucket → color** (app theme): `heritage` yellow, `stage` coral, `vicinity` green, `faith` blue, `activity` teal, `interest` pink, `general` gray.

| `status` | Meaning |
|----------|---------|
| `continue` | Lana will keep chatting; ask follow-ups |
| `ready_to_complete` | Enough for profile — show **“That’s me”** button |

Lana asks **1–2 questions per turn** (e.g. after heritage → family, kids, what they want on the block). Tone is warm and encouraging, not a form.

---

## 3. Complete profile

When `ready_to_complete` is true (or user taps **That’s me**):

```http
POST /lana/sessions/{session_id}/complete
```

**Body**

```json
{ "force": false }
```

Set `"force": true` only if the user insists they’re done before Lana sets `ready_to_complete` (needs at least one user message, or use after 2+ turns).

**Response**

```json
{
  "session_id": "uuid",
  "status": "completed",
  "assistant_message": "Your profile threads are ready...",
  "mapped_summary": "Latino mom, two toddlers, new to Lake Nona. Christian family, active runner and food lover.",
  "spans": [
    { "text": "Latino mom", "bucket": "heritage", "claim_concept": "latino_heritage" },
    { "text": "two toddlers", "bucket": "stage", "claim_concept": "parent_toddler_years" }
  ],
  "claims": [
    {
      "concept": "latino_heritage",
      "label": "Latino heritage",
      "confidence": 0.96,
      "disclosure": "public",
      "synonyms": ["Hispanic / Latina", "Latinoamericana"],
      "source_quote": "Latino mom",
      "bucket": "heritage"
    }
  ],
  "threads_found": 5
}
```

### Frontend: profile-built screen

| Field | Use |
|-------|-----|
| `mapped_summary` + `spans` | Colored “Mapped you” sentence |
| `claims[].label` | Card title |
| `claims[].confidence` | `96% match` |
| `claims[].source_quote` | `From 'Latino mom'` |
| `claims[].synonyms` | `≈` tags |
| `claims[].bucket` | Card left border color |

Claims are stored in Postgres (`source_quote`, `bucket` columns) with embeddings. Reload via `get_my_identity_claims` (includes `source_quote`, `bucket`).

---

## Event draft (host an event)

Same session endpoints; use `"purpose": "event_draft"`. Requires phone-verified user to **publish** (`create_event`).

### Start

```json
{ "purpose": "event_draft" }
```

Response includes `event_draft` (may be empty) and `ui.highlights: []`.

### Send message

User types natural language (e.g. brunch for new moms at Lake Nona Commons — 10am…).

**Response fields**

| Field | Use |
|-------|-----|
| `ui.highlights` | Color phrases in the textarea |
| `ui.bucket` | Active bucket: `time`, `venue`, `audience`, `activity`, `constraint`, `capacity`, `purpose` |
| `ui.focus_phrase` | Phrase Lana is asking about |
| `event_draft.title` | Prefill Title field |
| `event_draft.description` | Prefill Description |
| `event_draft.venue_name` | Venue |
| `event_draft.starts_at` / `ends_at` | ISO timestamps when inferable |
| `event_draft.max_attendees` | Capacity |
| `event_draft.cohort_tags` | Suggested Purpose chip ids → `get_event_purposes()` |
| `event_draft.missing` | Blockers still unknown |
| `ready_to_complete` | Show **Publish** when true |

**Event bucket → color (suggested UI)**

| Bucket | Color role |
|--------|------------|
| time | orange |
| venue | blue |
| audience | purple |
| activity | green |
| constraint | orange |
| capacity | orange |
| purpose | teal |

Lana asks **at most 1–2 questions per turn**, only when title, time, or venue is missing.

### Complete event

```http
POST /lana/sessions/{session_id}/complete
```

```json
{ "force": false, "publish": true }
```

| Field | Default | Meaning |
|-------|---------|---------|
| `force` | false | Complete before Lana says ready (needs 2+ user turns) |
| `publish` | true | Call `create_event` with extracted draft |

**Response (event_draft)**

```json
{
  "session_id": "uuid",
  "status": "completed",
  "assistant_message": "...",
  "mapped_summary": "Sunday brunch for new moms at Lake Nona Commons…",
  "spans": [{ "text": "10am", "bucket": "time" }],
  "event_draft": {
    "title": "Sunday brunch for new moms",
    "description": "...",
    "venue_name": "Lake Nona Commons",
    "starts_at": "2026-06-08T14:00:00+00:00",
    "cohort_tags": ["coffee_stroller", "postpartum_support"],
    "max_attendees": 12
  },
  "event_id": "uuid-if-published",
  "published": true
}
```

Set `"publish": false` to return draft only; host edits Purpose chips / fields, then call Supabase `create_event` from the client.

If `phone_not_verified`, `published` is false — draft is still returned.

Purpose chip ids: `get_event_purposes()` RPC (see PWA handoff).

---

## 4. Resume session (optional)

**Worker**

```http
GET /lana/sessions/{session_id}
```

**Supabase RPC** (read-only, same JWT)

```sql
select * from get_active_lana_session('profile_intake');
select * from get_lana_session_messages('<session_id>');
```

---

## React Native example

```ts
const LANA_URL = process.env.LANA_WORKER_URL;

async function lanaFetch(path: string, token: string, body?: object) {
  const res = await fetch(`${LANA_URL}${path}`, {
    method: body ? 'POST' : 'GET',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// After assign_home_block:
const { session_id, assistant_message } = await lanaFetch('/lana/sessions', token, {
  purpose: 'profile_intake',
});

const turn = await lanaFetch(`/lana/sessions/${session_id}/messages`, token, {
  message: userText,
});

if (turn.ready_to_complete) {
  // show "That's me" button
}

const done = await lanaFetch(`/lana/sessions/${session_id}/complete`, token, { force: false });
// then supabase.rpc('get_my_identity_claims')
```

---

## Errors

| Code | `detail` | Fix |
|------|----------|-----|
| 401 | `invalid_session` | Fresh OTP token |
| 400 | `home_block_required` | Run `assign_home_block` first |
| 400 | `session_not_active` | Start a new session |
| 400 | `keep_chatting_or_set_force_true` | More chat or `force: true` |
| 400 | `event_title_required` | Event complete without a title |
| 400 | `phone_not_verified` | Publish blocked until OTP verified |
| 502 | `lana_*_failed` / `create_event_failed` | Vertex or `create_event` RPC error |

---

## What Lana knows (every turn)

1. **Product** — `prompts/tagalng_product.md` (deployed with worker)  
2. **User** — block, cluster, ZIP, existing claims  
3. **Block network (agent retrieval)** — `get_lana_block_context_for_user` (service role): neighbor public labels + upcoming open events on the cluster — see [`DATA_MODEL_CLAIMS.md`](./DATA_MODEL_CLAIMS.md)  
4. **History** — `lana_messages` for this session  

Prompts are versioned in git; change copy via redeploy.

Apply migrations through `20260611120000_event_purpose_cohorts.sql` on tagalng-dev before deploy.
