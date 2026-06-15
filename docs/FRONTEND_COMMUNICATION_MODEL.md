# TagAlng — Frontend Integration: Communication Model (Features 1–5)

This is the frontend contract for the neighbor-to-neighbor communication model:
**discover → connect → chat (under nicknames) → reveal names → meet up / swap items**,
plus safety (block & report).

Everything is exposed as **Supabase RPC functions**. There is no separate REST layer to
learn — you call each function by name with the logged-in user's session.

---

## 0. How to call everything

Every endpoint below is a Postgres function reachable two ways:

**Supabase JS client (recommended):**
```ts
const { data, error } = await supabase.rpc('send_nudge', {
  p_recipient_id: otherUserId,
  p_context_message: 'Hi! Saw we both have toddlers',
});
```

**Raw REST (if needed):** `POST {SUPABASE_URL}/rest/v1/rpc/{function_name}` with headers
`apikey`, `Authorization: Bearer <user JWT>`, `Content-Type: application/json`, and the
`p_`-prefixed params as the JSON body.

### Auth
- The user must be **signed in** (a valid Supabase session). The function reads `auth.uid()` from the JWT.
- Comms actions also require the user to be **non-anonymous and phone-verified** — otherwise you get `phone_not_verified` / `anonymous_user_comms_blocked` (403).

### Error shape
On failure, `error` is populated (JS) / HTTP is 4xx (REST). Business errors look like:
```json
{ "code": "P0001", "message": "blocked" }
```
Handle the `message` string (e.g. `blocked`, `not_thread_member`, `unmask_already_pending`). All listed below per endpoint.

### Return shape
- **Scalar** functions return a bare value (a UUID string, or a tier string like `"acquaintance"`).
- **Table** functions return an **array of rows**.
- A couple return a small **JSON object** (noted inline).

### Action vs Read
- 🟢 **ACTION** = mutates state (send/accept/block/etc.). Call on user gesture.
- 🔵 **READ** = query for rendering a screen.

### The tier ladder (the backbone)
```
stranger → nudge → acquaintance → direct → irl_peer
```
- `acquaintance` = chatting under **nicknames** (shielded).
- `direct` = **real names** revealed (both consented).
- `irl_peer` = met in person.
- `blocked` is a separate state (not a tier) — handled by block/unblock.

Use 🔵 `get_relationship_tier` anytime to know what to render for a given person.

---

## 1. Discover & connect — Nudge and Intro

> **Story:** AK sees AM in Discover. She taps **Nudge** and sends a friendly opener. AM gets
> it, taps **Accept** — and instantly a shielded (nickname-only) chat opens between them.
> Separately, Lana might suggest AK formally **introduce** herself to AM with a "why you two
> match" note; if AM accepts that intro, the same thing happens.

### 🟢 `send_nudge` — wave at a neighbor (first contact)
```ts
supabase.rpc('send_nudge', { p_recipient_id, p_context_message })
```
| Param | Type | Required | Notes |
|---|---|---|---|
| `p_recipient_id` | uuid | ✅ | the other user |
| `p_context_message` | text | optional | a short opener |
**Returns:** `nudge_id` (uuid string).
**Errors:** `recipient_not_on_block`, `invalid_recipient`, `nudge_rate_limit_daily` (5/day), `phone_not_verified`.

### 🟢 `accept_nudge` — accept a wave → become acquaintances + open chat
```ts
supabase.rpc('accept_nudge', { p_nudge_id })
```
**Returns:** the new tier, `"acquaintance"`. **Side effect:** a `shielded` chat thread is opened (fetch it via `get_my_threads`).
**Errors:** `nudge_not_found_or_already_handled`.

### 🟢 `decline_nudge`
```ts
supabase.rpc('decline_nudge', { p_nudge_id })
```
**Returns:** none. **Errors:** `nudge_not_found_or_already_handled`.

### 🔵 `get_my_nudges` — nudge inbox
```ts
supabase.rpc('get_my_nudges', { p_direction: 'received' }) // or 'sent'
```
**Returns:** array of `{ id, other_user_id, nickname, avatar_url, sent_at, status, context_message, shared_count }`.

### 🔵 `get_my_intros` — pending intro inbox
```ts
supabase.rpc('get_my_intros', { p_direction: 'all' }) // or 'sent' | 'received'
```
**Returns:** array of `{ id, other_user_id, nickname, avatar_url, created_at, expires_at, status, match_reason, shared_dimensions, direction }` where `status = 'proposed'` and not expired.
**Lana:** ask "show my pending intros" → `ui_intent: show_pending_intros`, `pending_intros[]`, `active_intent: social.list_intros`.

### 🟢 `propose_intro` — Lana-style "let me introduce you" (follow-up after a nudge)
```ts
supabase.rpc('propose_intro', { p_candidate_id, p_match_reason, p_shared_dimensions })
```
| Param | Type | Required | Notes |
|---|---|---|---|
| `p_candidate_id` | uuid | ✅ | the other user |
| `p_match_reason` | text | ✅ | 10–280 chars ("you both run mornings") |
| `p_shared_dimensions` | text[] | optional | e.g. `["running","toddlers"]` |
| `p_match_score` | real | optional | |
| `p_joint_moment_id` | uuid | optional | |
**Returns:** `intro_id` (uuid). **Prerequisite:** you must already be at `nudge` tier with them.
**Errors:** `tier_too_low_send_nudge_first`, `candidate_consent_missing`, `match_reason_too_short`, `duplicate_intro_recent`, `candidate_not_on_block`.

### 🟢 `accept_intro` — accept an intro → acquaintance + open chat
```ts
supabase.rpc('accept_intro', { p_intro_id })
```
**Returns:** `"acquaintance"`. **Side effect:** opens a shielded chat (same as accepting a nudge).
**Errors:** `intro_not_found_or_expired`.

### 🟢 `decline_intro`
```ts
supabase.rpc('decline_intro', { p_intro_id })
```
**Returns:** none.

### 🔵 `get_relationship_tier` — what tier am I at with this person?
```ts
supabase.rpc('get_relationship_tier', { p_other_user_id })
```
**Returns:** a tier string: `"stranger" | "nudge" | "acquaintance" | "direct" | "irl_peer"`.
⚠️ Pass the **other** person's id — passing your own id returns `"stranger"`.

---

## 2. Chat (shielded & direct)

> **Story:** Now connected, AK opens the chat with "BlueJay" (AM's nickname), sees Lana's
> welcome line, and types "Nice to meet you!". AM gets it in real time, reads it, replies.

### 🔵 `get_my_threads` — the 1:1 chat list
```ts
supabase.rpc('get_my_threads')
```
**Returns:** array of `{ thread_id, kind, other_user_id, other_nickname, other_avatar_url, tier, last_message_at, last_message_preview, unread_count }`.
`kind` is `"shielded"` (nicknames) or `"direct"` (real names). Render the name based on `tier`.

### 🟢 `send_message`
```ts
supabase.rpc('send_message', { p_thread_id, p_content, p_client_dedupe_key, p_reply_to })
```
| Param | Type | Required | Notes |
|---|---|---|---|
| `p_thread_id` | uuid | ✅ | |
| `p_content` | text | ✅ | 1–8000 chars |
| `p_client_dedupe_key` | uuid | ✅ | **generate a fresh UUID per message** — makes retries safe (idempotent) |
| `p_reply_to` | uuid | optional | message being replied to |
**Returns:** `message_id` (uuid). Re-sending with the same `p_client_dedupe_key` returns the same id (no duplicate).
**Errors:** `not_thread_member`, `blocked`, `empty_message`, `message_too_long`, `reply_to_not_in_thread`.

### 🔵 `get_thread_messages` — history (newest first, paginated)
```ts
supabase.rpc('get_thread_messages', { p_thread_id, p_limit: 50, p_before: null })
```
| Param | Type | Notes |
|---|---|---|
| `p_thread_id` | uuid | |
| `p_limit` | int | default 50, max 100 |
| `p_before` | timestamptz | pass the oldest loaded `sent_at` to page back |
**Returns:** array of `{ id, sender_id, kind, content, reply_to, sent_at, deleted, is_mine }`.
`sender_id` is `null` for Lana/system messages (`kind` = `"lana"`/`"system"`). `deleted=true` → render a "message deleted" placeholder.

### 🟢 `mark_thread_read`
```ts
supabase.rpc('mark_thread_read', { p_thread_id, p_up_to_message_id })
```
`p_up_to_message_id` optional (defaults to now). **Returns:** none. Resets `unread_count`.

### 🟢 `delete_message`
```ts
supabase.rpc('delete_message', { p_message_id, p_kind: 'for_everyone' }) // or 'for_me'
```
`for_everyone` allowed only by the sender within 1 hour. **Returns:** none.
**Errors:** `not_message_sender`, `delete_window_expired`, `invalid_delete_kind`.

---

## 3. Unmask — reveal real names (acquaintance → direct)

> **Story:** After chatting a while, AK taps "Ask to unmask". AM gets a request and accepts.
> Their real names appear, the chat upgrades to **direct**, and Lana posts "You're connected
> directly now."

### 🟢 `propose_unmask`
```ts
supabase.rpc('propose_unmask', { p_other_user_id })
```
**Returns:** `request_id` (uuid). **Prerequisite:** must be `acquaintance`.
**Errors:** `must_be_acquaintance_to_unmask`, `unmask_already_pending`, `unmask_cooldown` (48h after a decline), `blocked`.

### 🟢 `accept_unmask` — both consented → direct
```ts
supabase.rpc('accept_unmask', { p_request_id })
```
**Returns:** `"direct"`. **Side effect:** the existing chat thread flips `shielded → direct`; a Lana system message is posted. Now `get_my_threads`/profiles show **real names**.
**Errors:** `unmask_not_found_or_expired`.

### 🟢 `decline_unmask`
```ts
supabase.rpc('decline_unmask', { p_request_id })
```
**Returns:** none. Starts a 48h cooldown.

---

## 4. Group chats & meeting in real life

> **Story:** AK hosts a "Park playdate". A group chat is created automatically with AK in it.
> AM asks to join; AK approves; AM is auto-added to the group. They chat as a group. Later,
> AK & AM (already `direct`) confirm they met in person → they become `irl_peer`.

### Event lifecycle (existing RPCs — group chat is auto-wired on top)
| Endpoint | Type | Params | Returns |
|---|---|---|---|
| `create_event` | 🟢 | `{ p_fields: { title, lat, lng, starts_at, ends_at?, venue_name?, cohort_tags?, max_attendees? } }` | `event_id` (uuid) — **a group chat is auto-created** |
| `request_to_join_event` | 🟢 | `{ p_event_id, p_message }` | `request_id` (uuid) |
| `decide_event_request` | 🟢 | `{ p_request_id, p_decision: 'approved' \| 'declined' }` | none — on `approved`, requester is **auto-added** to the group chat |
| `cancel_event_request` | 🟢 | `{ p_request_id }` | none — requester is removed from the group chat |

> Group threads use the **same** `send_message` / `get_thread_messages` / `mark_thread_read`
> endpoints as 1:1 chats. Lana posts no welcome in groups. Blocking in a group only hides the
> blocked person's messages (you can still post).

### 🔵 `get_my_group_threads` — the group chat list
```ts
supabase.rpc('get_my_group_threads')
```
**Returns:** array of `{ thread_id, event_id, event_title, last_message_at, last_message_preview, unread_count, member_count }`.

### 🟢 `confirm_irl_met` — "we met in person" (mutual; direct → irl_peer)
```ts
supabase.rpc('confirm_irl_met', { p_other_user_id })
```
**Returns:** the current tier — `"direct"` if only you've confirmed so far, `"irl_peer"` once **both** have.
**Prerequisite:** must be `direct`. **Errors:** `must_be_direct_to_confirm_irl`.
*(There is also an automatic path: co-attending an event then 24h passes — handled server-side, no FE call.)*

---

## 5. Marketplace (free / swap — no selling)

> **Story:** AM lists a "Toddler bike" to swap. AK browses, sees it, taps Message → an inquiry
> chat opens. They agree on a time/place; both confirm the handoff; both confirm the swap
> happened. Optionally both tap "Stay in touch" → if they were strangers, they become
> acquaintances with a fresh chat.

### 🟢 `list_marketplace_item`
```ts
supabase.rpc('list_marketplace_item', { p_title, p_description, p_intent_type: 'free', p_category, p_photos })
```
| Param | Type | Notes |
|---|---|---|
| `p_title` | text | ✅ 1–80 chars |
| `p_description` | text | optional, ≤500 |
| `p_intent_type` | text | `'free'` or `'swap'` only (no selling) |
| `p_category` | text | optional |
| `p_photos` | jsonb | optional, default `[]` |
| `p_block_id` | text | optional, defaults to your home block |
**Returns:** `item_id` (uuid). **Errors:** `selling_not_allowed_v01`, `title_required`.

### 🔵 `get_marketplace_items` — browse active listings on your block
```ts
supabase.rpc('get_marketplace_items', { p_block_id: null, p_limit: 50 })
```
**Returns:** array of `{ item_id, seller_id, seller_nickname, title, description, category, intent_type, photos, created_at }`.

### 🟢 `create_inquiry` — message a seller (opens an inquiry chat)
```ts
supabase.rpc('create_inquiry', { p_item_id, p_opening_text })
```
**Returns (JSON object):** `{ "inquiry_id": "...", "thread_id": "..." }`. Use `thread_id` with the normal chat endpoints.
**Errors:** `item_not_active`, `cannot_inquire_own_item`, `inquiry_already_open`, `blocked`.

### 🟢 `confirm_handoff` — agree on time + place (both sides)
```ts
supabase.rpc('confirm_handoff', { p_inquiry_id, p_when, p_where, p_lat, p_lng })
```
| Param | Type | Notes |
|---|---|---|
| `p_when` | timestamptz (ISO) | meetup time |
| `p_where` | text | place |
| `p_lat` / `p_lng` | float | optional |
**Returns:** `"one_side"` (waiting on the other) or `"committed"` (both agreed).
**Errors:** `inquiry_not_open`, `inquiry_not_found`.

### 🟢 `confirm_completion` — "the swap happened" (both sides)
```ts
supabase.rpc('confirm_completion', { p_inquiry_id })
```
**Returns:** `"one_side"` or `"completed"` (item becomes `sold`). **Errors:** `inquiry_not_committed`.

### 🟢 `mark_acquaintance_from_inquiry` — optional "Stay in touch" (both sides)
```ts
supabase.rpc('mark_acquaintance_from_inquiry', { p_inquiry_id, p_consent: true })
```
**Returns (JSON object):**
- `{ "status": "recorded_waiting_other" }` — you opted in, waiting on the other
- `{ "status": "both_consented_promoted", "new_chat_id": "..." }` — both opted in → promoted to `acquaintance` + new shielded chat
- `{ "status": "already_connected" }` — you were already connected (no change)
- `{ "status": "skipped" }` — you passed (`p_consent:false`)
**Errors:** `inquiry_not_completed`. *(Completion alone never connects you — only this mutual opt-in does.)*

### 🟢 `close_inquiry`
```ts
supabase.rpc('close_inquiry', { p_inquiry_id, p_reason })
```
**Returns:** none. **Errors:** `inquiry_already_finalized`.

### 🔵 `get_my_inquiries` — your buying/selling conversations
```ts
supabase.rpc('get_my_inquiries')
```
**Returns:** array of `{ inquiry_id, thread_id, item_id, item_title, role, other_user_id, other_nickname, status, last_message_at, created_at }`. `role` is `"buyer"` or `"seller"`.

---

## 6. Safety — block & report (works everywhere)

> **Story:** AM feels uncomfortable. She blocks AK — their chat archives, AK can no longer
> message her, and AK disappears from her lists. She can also report a specific message.

### 🟢 `block_user`
```ts
supabase.rpc('block_user', { p_blocked_user_id, p_reason_category, p_reason })
```
`p_reason_category`: `'harassment'|'threat'|'sexual'|'self_harm'|'csam'|'discomfort'|'spam'|'other'` (optional).
**Returns:** none. **Side effects:** cancels pending nudges/unmasks, archives the shared chat. After this, `send_message` between them returns `blocked`.

### 🟢 `unblock_user`
```ts
supabase.rpc('unblock_user', { p_blocked_user_id })
```
**Returns:** the restored tier (e.g. `"acquaintance"`). If they were `direct`, they're restored to `acquaintance` and the chat **re-shields** (names hidden again). **Errors:** `not_blocked`, `unblock_requires_support` (IRL-peer safety blocks).

### 🟢 `report_message`
```ts
supabase.rpc('report_message', { p_category, p_message_id, p_thread_id, p_target_user_id, p_description })
```
`p_category`: `'harassment'|'spam'|'sexual'|'self_harm'|'threat'|'off_platform_ask'|'csam'|'other'`. Provide `p_message_id` (preferred) or `p_target_user_id`.
**Returns:** `report_id` (uuid). The target never knows they were reported. **Errors:** `cannot_report_self`, `report_rate_limit`, `report_target_required`.

---

## 7. Realtime (live updates)

Subscribe to Postgres changes; RLS guarantees you only receive your own rows.

```ts
// New / changed messages in a thread you're viewing
supabase.channel('thread:' + threadId)
  .on('postgres_changes',
      { event: '*', schema: 'public', table: 'messages', filter: `thread_id=eq.${threadId}` },
      payload => { /* append / update bubble */ })
  .subscribe();
```

Useful tables to subscribe to:
| Table | Watch for |
|---|---|
| `messages` (filter `thread_id`) | incoming messages, deletions |
| `chat_threads` | new threads, `kind` flip (unmask), archive (block) |
| `unmask_requests` | incoming unmask proposals / acceptances |
| `inquiries` | inquiry status changes (committed/completed) |
| `marketplace_items` | listing status (e.g. `sold`) |
| `moderation_actions` (your own) | you got suspended mid-session |

> Realtime is best-effort for UX; the RPCs above are the source of truth. After reconnecting, refetch with the 🔵 read endpoints.

---

## 8. End-to-end happy path (ties it together)

```
1. get_relationship_tier(AM)               → "stranger"
2. send_nudge(AM, "Hi!")                    → nudge_id            [AK]
3. accept_nudge(nudge_id)                   → "acquaintance"      [AM]  (+ shielded chat opens)
4. get_my_threads()                         → [ { thread_id, kind:"shielded", other_nickname } ]
5. send_message(thread_id, "Hey!", uuid())  → message_id          [AK]
6. get_thread_messages(thread_id)           → [...]               [AM]
7. mark_thread_read(thread_id)              →                     [AM]
8. propose_unmask(AM)                        → request_id         [AK]
9. accept_unmask(request_id)                → "direct"            [AM]  (chat flips to direct)
10. confirm_irl_met(AM) x2                   → "irl_peer"         (after they meet)
```

Marketplace and group chats branch off the same primitives (`create_inquiry`/`create_event` →
a thread → the same chat endpoints).

---

## Quick reference — all action vs read endpoints

**🟢 Actions:** `send_nudge`, `accept_nudge`, `decline_nudge`, `propose_intro`, `accept_intro`,
`decline_intro`, `send_message`, `mark_thread_read`, `delete_message`, `propose_unmask`,
`accept_unmask`, `decline_unmask`, `block_user`, `unblock_user`, `report_message`,
`create_event`, `request_to_join_event`, `decide_event_request`, `cancel_event_request`,
`confirm_irl_met`, `list_marketplace_item`, `create_inquiry`, `confirm_handoff`,
`confirm_completion`, `mark_acquaintance_from_inquiry`, `close_inquiry`.

**🔵 Reads:** `get_relationship_tier`, `get_my_nudges`, `get_my_threads`, `get_thread_messages`,
`get_my_group_threads`, `get_marketplace_items`, `get_my_inquiries`.
