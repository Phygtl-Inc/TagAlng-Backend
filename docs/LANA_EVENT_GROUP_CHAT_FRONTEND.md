# Lana — Event Group Chat · Frontend Integration

How the per-event group chat works and the exact RPCs the FE calls. This is **not** a
lana-worker (`/lana/...`) feature — group chat is plain Supabase: tables + RPCs the
client calls with `supabase.rpc(...)`, same as the existing 1:1 thread screen
(`src/app/(protected)/threads/[id]/conversation.tsx`).

> **Backend status: done.** Thread creation, membership, send/read are all live
> (migrations `20260618130000_peer_chat_shielded.sql` + `20260621120000_event_group_chat.sql`).
> **Frontend status: not built.** There is no group-chat screen yet — this doc is the
> contract to build it against.

---

## 1. How it works (no FE action needed for any of this)

Group chat is wired to the event lifecycle by DB triggers — the FE never creates threads
or adds members:

- **One group thread per event, auto-created on publish.** When an event row is inserted
  with `status = 'open'`, a trigger creates a `chat_threads` row with
  `kind = 'group_event'`, `event_id = <event>`, and adds the **host** as the first member.
  One thread per event (unique index on `event_id where kind='group_event'`).
- **Membership follows RSVP, automatically.** A trigger on `event_requests`:
  - request becomes **`approved`** → that user is added to `chat_thread_members`
    (re-approval clears a previous `left_at`).
  - request becomes **`cancelled` / `declined`** → that member's `left_at` is set (removed).

So: host publishes → thread exists with host in it. Host approves a join request → that
neighbour is in the chat. The FE just reads/writes; it never manages membership.

- **Lana is not in group threads** (no assistant messages).
- **Blocking is read-side** for groups: a blocked sender's messages are hidden per-viewer
  (RLS), but posting is never rejected on a group thread.

---

## 2. RPCs the FE uses

All are `security definer`, granted to **`authenticated`** only (a signed-in, **verified**
user — see Gotchas). Call them with the Supabase client: `supabase.rpc(name, params)`.

### `get_my_group_threads()` — list the user's event chats

No params. Returns one row per group thread the caller belongs to (active membership),
newest activity first:

| Column                 | Type          | Notes                                            |
|------------------------|---------------|--------------------------------------------------|
| `thread_id`            | uuid          | pass to the other RPCs                            |
| `event_id`             | uuid          | the event this chat belongs to                    |
| `event_title`          | text          | header                                            |
| `last_message_at`      | timestamptz   | for sorting / preview timestamp                   |
| `last_message_preview` | text          | last message body (`''` if deleted)               |
| `unread_count`         | int           | messages after the caller's `last_read_at`        |
| `member_count`         | int           | active members                                    |

### `get_thread_messages(p_thread_id, p_limit?, p_before?)` — paginated history

| Param          | Type        | Default | Notes                                       |
|----------------|-------------|---------|---------------------------------------------|
| `p_thread_id`  | uuid        | —       | the group thread                             |
| `p_limit`      | int         | 50      | clamped to 1..100                            |
| `p_before`     | timestamptz | null    | cursor — pass the oldest loaded `sent_at` to page back |

Returns **newest-first** (reverse for display):

| Column      | Type        | Notes                                  |
|-------------|-------------|----------------------------------------|
| `id`        | uuid        | message id                              |
| `sender_id` | uuid        | who sent it                             |
| `kind`      | text        | `'text'`                                |
| `content`   | text        | `''` when deleted                       |
| `reply_to`  | uuid \| null| threaded reply target                   |
| `sent_at`   | timestamptz | order key / cursor                      |
| `deleted`   | boolean     | render as "message deleted"             |
| `is_mine`   | boolean     | right-align / styling                   |

> Sender names/avatars are **not** in this payload — resolve them via the profile
> summary RPC (`get_profile_summary` / `_authed`) or batch by `sender_id`, same as the
> meet view does for the host.

### `send_message(p_thread_id, p_content, p_client_dedupe_key, p_reply_to?)` — post

| Param                 | Type        | Notes                                                    |
|-----------------------|-------------|----------------------------------------------------------|
| `p_thread_id`         | uuid        | the group thread                                          |
| `p_content`           | text        | 1..8000 chars (else `empty_message` / `message_too_long`) |
| `p_client_dedupe_key` | uuid        | client-generated; resend-safe (returns the existing id)   |
| `p_reply_to`          | uuid \| null| optional reply target (must be in the thread)             |

Returns the new message `id` (uuid). Errors (`P0001`): `not_thread_member`,
`empty_message`, `message_too_long`, `reply_to_not_in_thread`, and the verification gate
(below).

### `mark_thread_read(p_thread_id, p_up_to_message_id?)` — clear unread

Sets the caller's `last_read_at`. Pass the latest visible message id, or omit to mark read
up to now. Call it on open and after new messages render (the 1:1 screen calls it on mount).

---

## 3. Calling pattern (matches the existing 1:1 screen)

```ts
import { createClient } from '@/utils/supabase/client';
const supabase = createClient();

// list
const { data: threads } = await supabase.rpc('get_my_group_threads');

// open one
const { data: messages } = await supabase.rpc('get_thread_messages', {
  p_thread_id: threadId, p_limit: 50,
});
await supabase.rpc('mark_thread_read', { p_thread_id: threadId });

// send (dedupe key makes retries safe)
const { data: msgId } = await supabase.rpc('send_message', {
  p_thread_id: threadId,
  p_content: text,
  p_client_dedupe_key: crypto.randomUUID(),
  p_reply_to: null,
});
```

## 4. Live updates

Members can `select` on `public.messages` (RLS: `messages_select_member_unblocked`), so
subscribe to Supabase Realtime for inserts on that table filtered by `thread_id`, then
append (and `mark_thread_read`). Polling `get_thread_messages` on an interval is the
simpler fallback.

## 5. Entry point

From the meet container (`src/app/(public)/meet/[id]/meet-view.tsx`) add an "Open group
chat" action that routes to the chat screen for `get_my_group_threads().thread_id`
matching this `event_id`. (Deferred in P1 precisely because this screen doesn't exist yet.)

---

## 6. Gotchas

- **Verified, non-anonymous users only.** `send_message` calls
  `_require_verified_neighbor_comms()` → raises `not_authenticated`,
  `anonymous_user_comms_blocked`, or `phone_not_verified` otherwise. (Email verification
  now satisfies that gate — see migration `20260717120000_email_verify_unlocks_gates.sql`,
  which must be applied.)
- **`is_mine` / `is_host`:** the message payload gives `is_mine`; to mark the host,
  compare `sender_id` to the event's `host_id` (from the event preview).
- **Deleted messages** come back with `content: ''` and `deleted: true` — render a tombstone.

---

## 7. Not built yet (backend gap)

**"Who Brings What" pinned panel** (stroller/coffee/snacks claims) has **no backend** —
there is no table or RPC for per-attendee item claims. It needs a new
`event_contributions` (or similar) table + claim/list RPCs before the FE can build the
pinned panel. Everything else above is ready to consume.

---

## 8. Related: 1:1 direct messages (separate flow, already built)

Group chat is the event surface. A **1:1 message between two users** is a *different*
thread kind with its own creation rules — documented here so the two don't get confused.

**A user cannot free-form DM any neighbour.** A 1:1 thread opens only on a **mutual
connection** (consent), via the internal `_open_relationship_thread(a, b)` helper
(idempotent — one thread per pair). Entry points:

| Trigger | RPC | Result |
|---------|-----|--------|
| A nudges B, **B accepts** | `accept_nudge` | shielded 1:1 thread opens |
| Lana proposes an intro, the other **accepts** | `accept_intro` | shielded 1:1 thread opens |
| Marketplace swap/item inquiry | (marketplace RPC) | `inquiry` 1:1 thread opens |

On creation both users are added and **Lana posts an opener** ("You're connected — names
stay private until you both choose to share them").

**Thread kinds (the `kind` column):**

| `kind`        | Meaning                                                            |
|---------------|-------------------------------------------------------------------|
| `shielded`    | 1:1, real names **hidden** (default after connecting)             |
| `direct`      | 1:1, names **revealed** — promoted via the mutual *unmask* flow or IRL co-attendance |
| `inquiry`     | 1:1, marketplace                                                  |
| `group_event` | the event group chat (this doc, §1–6)                             |

**Same message RPCs** as group chat — `send_message`, `get_thread_messages`,
`mark_thread_read`. List 1:1 threads via `get_my_chat_inbox()` (not
`get_my_group_threads()`).

### Privacy rules — shielded → direct (the core invariant)

A 1:1 relationship is **private by default and only de-anonymizes on mutual consent**
(ATPR invariant). The FE must honour this:

1. **On connect, the thread starts `shielded`.** Real **names and avatars are hidden** for
   both people — show a placeholder/initial, never the real identity. They can chat freely
   while still anonymous.
2. **Names are revealed only when BOTH agree (mutual unmask).** One taps "share names"
   (`propose_unmask(p_other_user_id)`); the other accepts (`accept_unmask(p_request_id)`)
   or declines (`decline_unmask(p_request_id)`). On accept, the pair promotes to
   **`direct`** and the **same thread flips `kind` shielded → direct** — now real names/
   avatars are shown. One-sided proposals never reveal anything; unanswered requests expire.
3. **Meeting IRL can also promote to `direct`.** `confirm_irl_met(p_other_user_id)` (or
   verified co-attendance) promotes the pair — but co-attending an event **alone never
   auto-reveals**; it needs the explicit confirm/proof.
4. **Render rule:** gate any real name/avatar on `kind === 'direct'`. While `shielded`,
   treat the other party as anonymous no matter what other data you have.
5. **Blocking:** 1:1 threads **reject** a send if either party blocked the other; group
   threads don't reject — blocked senders are hidden per-viewer on read.

Lana relays in shielded threads (she posted the opener); she is **not** a member of group
threads.

**FE status: already built** — unlike group chat, the 1:1 screens exist
(`src/app/(protected)/threads/[id]/conversation.tsx` + the nudge inbox). No new FE work
needed here; this section is context so the group-chat build reuses the same message RPCs
and doesn't reinvent threading.

> **DB sanity check (prod, this is live):** `chat_threads` by `kind` — `group_event` 32,
> `shielded` 2, `direct` 1, `inquiry` 1. Both flows are creating threads correctly.
