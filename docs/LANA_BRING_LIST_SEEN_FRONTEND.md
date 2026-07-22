# Lana — Bring-List Claims ("Who Brings What") + Read Receipts · Frontend Integration

The pinned **"who brings what"** card in the event group chat (each member claims an
item), plus **seen/read receipts** ("Seen", "Seen by Maria") and **live unread dots**.
Like group chat itself, this is **not** a lana-worker (`/lana/...`) feature — it's plain
Supabase: tables + RPCs called with `supabase.rpc(...)`, plus Realtime subscriptions.

> **Backend status: done in code; needs the migration applied.** Everything below ships in
> `supabase/migrations/20260824120000_bring_claims_read_receipts.sql` (apply via
> `supabase db push` / Dashboard SQL Editor). Existing open events with a bring list are
> backfilled automatically.
> **Frontend status: not built.** This doc is the contract to build against.
> Companion doc: `LANA_EVENT_GROUP_CHAT_FRONTEND.md` (thread/message RPCs this builds on).

---

## Part A — Pinned "who brings what"

### 1. How it works (no FE action needed for any of this)

- `events.bring_items` (`text[]`, captured in the host flow) **stays the authoritative
  list**. A DB trigger mirrors it into one claimable row per item in a new table,
  `event_bring_items`. The FE never inserts rows directly.
- Editing the event's list adds/removes rows automatically — but a **claimed row is never
  auto-deleted** (it's someone's commitment).
- **Claiming is atomic** — first tap wins; a concurrent second tap gets an
  `already_claimed` error. Claims/unclaims **post a `system` message** into the event's
  group chat ("Maria is bringing Snacks"), so announcements ride the existing message
  realtime + unread machinery for free — no extra FE work to surface them.
- Visibility is **group-chat members only** (host + approved attendees), enforced by RLS.

### 2. RPCs

All `security definer`, granted to `authenticated`. Errors are `P0001` with the message
as the code, same as the other chat RPCs.

#### `get_event_bring_items(p_event_id uuid)` — the list

Returns rows ordered by `position`:

| Column               | Type          | Notes                                        |
|----------------------|---------------|----------------------------------------------|
| `id`                 | uuid          | pass to claim/unclaim                         |
| `label`              | text          | e.g. "Stroller" (1–60 chars)                  |
| `position`           | int           | display order                                 |
| `claimed_by`         | uuid \| null  | null = open                                   |
| `claimed_at`         | timestamptz \| null |                                        |
| `claimer_nickname`   | text \| null  | nickname of the claimer (never real name)     |
| `claimer_avatar_url` | text \| null  |                                               |
| `is_mine`            | bool \| null  | true when the caller claimed it               |

Returns empty (not an error) for non-members.

#### `claim_bring_item(p_item_id uuid)` — tap to claim

Void. Errors: `bring_item_not_found`, `not_thread_member`, **`already_claimed`** (someone
beat you to it — refetch and show a toast), plus the verification gate (Gotchas).

#### `unclaim_bring_item(p_item_id uuid)` — release

Void. Allowed for the **claimer, the host, or the co-host**. Error: `not_claimer_or_host`.
Idempotent if the item is already open.

#### `add_bring_item(p_event_id uuid, p_label text)` — host adds an item

Void. **Host/co-host only.** Writes through `events.bring_items` (the trigger creates the
row synchronously, so a refetch right after the call sees it). Errors:
`invalid_bring_label` (empty / >60 chars), `not_event_host`,
`bring_list_full_or_duplicate` (12-item cap, case-insensitive duplicate).

### 3. Live updates

`event_bring_items` is in the `supabase_realtime` publication (RLS applies — members only):

```ts
supabase
  .channel(`bring-items-${eventId}`)
  .on(
    'postgres_changes',
    { event: '*', schema: 'public', table: 'event_bring_items', filter: `event_id=eq.${eventId}` },
    () => reload() // refetch via get_event_bring_items — payload lacks the joined nickname
  )
  .subscribe();
```

### 4. UI spec (matches the C-CHAT-BR-COFFEE mock)

Pinned collapsible card **between the group-chat header and the message list** in
`group-conversation-view.tsx` (precedent: the pinned co-host invite card in the 1:1 view):

- Header row: 📌 `PINNED · WHO BRINGS WHAT`, chevron toggles collapse.
- One row per item: label, then
  - **claimed** → "— {nickname}" (or "you") + a check mark; tapping **your own** claim
    releases it (`unclaim_bring_item`); other people's claims are inert.
  - **open** → underlined "open · tap to claim" → `claim_bring_item`.
- Optimistic claim is fine, but on **any** error refetch and revert;
  map `already_claimed` to its own toast ("Someone just claimed that one.").
- **Host/co-host** additionally get an add-item input (maxLength 60) → `add_bring_item`.
- Hide the card entirely when the list is empty and the viewer can't edit.
- The claim's system message arrives on the **existing** `messages` realtime channel and
  renders with the current null-sender system styling — nothing new to build there.

---

## Part B — Read receipts ("Seen" / "Seen by Maria")

### 1. Model

There is **no per-message receipt table**. Each member has one cursor —
`chat_thread_members.last_read_at` — which `mark_thread_read` (already called on open and
on each live incoming message) advances. A message is "seen by X" ⇔
`X.last_read_at >= message.sent_at`. Works identically for 1:1 and group threads.

### 2. RPC

#### `get_thread_read_receipts(p_thread_id uuid)` — other members' cursors

| Column         | Type                | Notes                                   |
|----------------|---------------------|-----------------------------------------|
| `user_id`      | uuid                |                                         |
| `nickname`     | text \| null        | nickname only — never gate real names here |
| `avatar_url`   | text \| null        |                                         |
| `last_read_at` | timestamptz \| null | null = never opened the thread          |

Excludes the caller; excludes members who left; blocked users filtered both ways.

### 3. Live updates

`chat_thread_members` is **already** in the realtime publication (members can select via
RLS), so:

```ts
supabase
  .channel(`read-receipts-${threadId}`)
  .on(
    'postgres_changes',
    { event: 'UPDATE', schema: 'public', table: 'chat_thread_members', filter: `thread_id=eq.${threadId}` },
    () => reload() // refetch the RPC — payload lacks the nickname join
  )
  .subscribe();
```

### 4. UI spec

- Render the receipt **only under the viewer's newest own message** (the cursor implies
  every earlier message is read too — receipts under every bubble is noise).
- **1:1**: small muted "Seen" when the other member's `last_read_at >= sent_at`.
- **Group**: "Seen by {names}" with the nicknames whose cursor passed it —
  `Intl.ListFormat(locale, { type: 'conjunction' })` for the joining.
- Compare as dates (`new Date(a) >= new Date(b)`), not strings. Caveat: an optimistic
  message's `sent_at` is client clock; a skewed clock can delay the receipt until the next
  refetch — acceptable.

---

## Part C — Live unread dots

The dots already exist (`unread_count` from `get_my_threads` / `get_my_group_threads`,
rendered in `chats-drawer.tsx` + `useHasNewChats` for the nav) — they just only refresh on
open/close today. To make them live:

- Subscribe to `messages` **INSERT** with **no filter** — RLS scopes the feed to threads
  the viewer belongs to — in `useHasNewChats` (always, while signed in) and `useMyChats`
  (while the drawer is open). On event: re-run the existing fetch.
- **Debounce the refetch ~500–800 ms.** If the user has the conversation open, its own
  `mark_thread_read` fires on the same insert; refetching instantly can race it and flash
  the dot.
- No webhooks, no polling, no new backend — this is pure client subscription.

---

## Gotchas

- **Verification gate:** `claim_bring_item`, `unclaim_bring_item`, `add_bring_item` all
  call `_require_verified_neighbor_comms()` → `not_authenticated` /
  `anonymous_user_comms_blocked` / `phone_not_verified`. Members are verified by
  definition, so this only bites stale sessions — surface the existing "verify to chat"
  toast.
- **Generated types:** until `database.types.ts` is regenerated against the migrated DB,
  call the new RPCs through the `UntypedRpc` cast pattern already used in
  `src/lib/cohost.ts` (cast the client, not the detached method).
- **System messages** have `sender_id = null`, `kind = 'system'` — both conversation views
  already render that shape. They bump `last_message_at` and count toward `unread_count`
  like any message.
- **Privacy:** receipts and claimer names are **nicknames**. Do not substitute real names,
  even in `direct`-tier threads, without going through the existing tier gating.
- Suggested i18n keys (add to `conversation.*` in en/es/pt-BR): `bringTitle`,
  `bringOpenTap`, `bringTakenToast`, `bringFailedToast`, `bringAddPlaceholder`,
  `bringAddAria`, `bringAddFailedToast`, `seen`, `seenBy` (`"Seen by {names}"`).
