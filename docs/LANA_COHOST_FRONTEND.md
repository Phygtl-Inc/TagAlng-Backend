# Co-Host — Invite, Accept & Manage · Frontend Integration

_Last updated: 2026-06-30_

This covers the **co-host** flow: a host invites a neighbor to co-host their event,
the neighbor accepts/declines in chat, and an accepted co-host can help run the meet.

Everything is a Supabase RPC — call with the authenticated client:

```ts
const { data, error } = await supabase.rpc('<function>', { ...params });
```

Auth, permissions, and "who can do what" are enforced server-side. You just call the
functions and render what comes back. **One co-host per event** ("one at a time").

---

## 0. The flow at a glance

```
Host taps "Add a co-host"
      → get_cohost_candidates        (list block + nearby neighbors, searchable)
Host taps "Invite" on a candidate
      → propose_cohost               (also opens a 1:1 chat thread automatically)
Candidate sees invite in their chat
      → get_my_cohost_invites('received')   (renders the action card)
      → accept_cohost_invite  /  decline_cohost_invite
Host watches status
      → get_my_cohost_invites('sent')       (WAITING → accepted)
      → revoke_cohost                (host removes the co-host)
Accepted co-host helps run the meet
      → update_event, get_my_event_requests, decide_event_request  (already authorize co-host)
"Message" button on any card
      → use thread_id from the invite + existing chat RPCs
```

---

## 1. List candidates — `get_cohost_candidates`

Call when the host opens the **"INVITE A CO-HOST"** picker.

**Params**
| param | type | notes |
|---|---|---|
| `p_event_id` | uuid | the event being co-hosted |
| `p_search` | text \| null | name search; `null` = top picks |
| `p_limit` | int | default 20, max 100 |

**Returns** (one row per candidate)
| field | type | render as |
|---|---|---|
| `candidate_id` | uuid | pass to `propose_cohost` |
| `nickname` | text | name |
| `avatar_url` | text \| null | avatar |
| `block_name` | text | block label |
| `same_block` | bool | `true` → **TOP FIT** / "same block" badge; `false` → "nearby / same lane" badge |
| `shared_affinity_count` | int | how many affinities they share with the host |
| `affinities` | text[] | the chips ("toddler 2y", "Brazilian", "stroller") |
| `meets_hosted` | int | "X MEETS HOSTED" stat |
| `contributions` | int | "X CONTRIBS" stat |
| `suggested_overlap_reason` | text | prefill the invite note (already ≥10 chars) |
| `already_invited` | bool | disable **Invite** (invite pending/accepted) |
| `is_current_cohost` | bool | this person is already the co-host |

> The list is ranked: same-block first, then most shared affinities, then most meets hosted.

### Search behavior
- The **"Search by name"** box maps to `p_search`. Leave it `null` for the ranked
  "TOP PICKS ON YOUR BLOCK" list; pass the box text to filter.
- Match is **case-insensitive, partial, name only** — typing `ma` finds "Maria", "Mateus".
- **Scope:** the host's whole **cluster** (their block + nearby blocks on the same lane),
  excluding themselves. It is **not** a global search across all app users — you can only
  search neighbors you're allowed to see.
- Results show public info only (nickname, avatar, public affinity chips, hosting stats).

---

## 2. Send the invite — `propose_cohost`

Call when the host taps **Invite**.

**Params**
| param | type | notes |
|---|---|---|
| `p_candidate_id` | uuid | from the candidate row |
| `p_overlap_reason` | text | the note ("You were the first mom I thought of…"), **≥ 10 chars** |
| `p_event_id` | uuid | the event |

**Returns:** `invite_id` (uuid).

Side effect: a 1:1 chat thread with the candidate is opened automatically, so the
invite lands in their chat and the **Message** button works immediately.

After success → flip the host's UI to **"CO-HOST PENDING · WAITING"**.

---

## 3. Read invites (both sides) — `get_my_cohost_invites`

One function powers both the host's pending card and the candidate's action card.

**Param:** `p_direction` — `'received'`, `'sent'`, or `'all'`.

**Returns** (one row per invite)
| field | type | use |
|---|---|---|
| `invite_id` | uuid | pass to accept/decline |
| `direction` | text | `'sent'` (you invited) or `'received'` (you were invited) |
| `event_id`, `event_title` | uuid, text | event header |
| `starts_at` | timestamptz | "Sat · Jun 7 · 10:00am" |
| `venue_name`, `venue_address` | text | "Foxtail · Lake Nona" |
| `host_id`, `host_name`, `host_avatar` | — | "Helena invited you" |
| `candidate_id`, `candidate_name`, `candidate_avatar` | — | who was invited |
| `overlap_reason` | text | the **"NOTE FROM HELENA"** block |
| `status` | text | `'proposed'` \| `'accepted'` \| `'declined'` \| `'revoked'` |
| `thread_id` | uuid | the chat thread → **Message** button target |
| `created_at`, `responded_at` | timestamptz | timestamps |

Rows are ordered with pending (`proposed`) first.

- **Host side** (`'sent'`): `status='proposed'` → WAITING card; `'accepted'` → active co-host.
- **Candidate side** (`'received'`): `status='proposed'` → render the Accept/Decline action card.

---

## 4. Respond to an invite (candidate)

**Accept** — `accept_cohost_invite`
- Param: `p_invite_id`
- Returns: `event_id`

**Decline** — `decline_cohost_invite`
- Param: `p_invite_id`
- Returns: nothing

---

## 5. Host management

**Revoke the co-host** — `revoke_cohost`
- Param: `p_event_id`
- Returns: nothing
- Host-only. Use for the **⋯ → Revoke co-host** popover action.

**Message** (either side, any card)
- Use the `thread_id` from `get_my_cohost_invites` and your existing 1:1 chat screen
  (`send_message`, `get_thread_messages`, `get_my_threads`). No new RPC needed.

---

## 6. What an accepted co-host can do

After acceptance, the co-host is authorized through the **existing** event RPCs — no
new calls, no special flags. Just let the co-host into the same screens as the host:

| Action | RPC | Co-host allowed? |
|---|---|---|
| Edit meet details | `update_event` | ✅ yes |
| See the event in any status | event reads | ✅ yes |
| See RSVPs / approve joins | `get_my_event_requests`, `decide_event_request` | ✅ yes |
| Cancel the event | event cancel/delete | ❌ host only |
| Revoke a co-host | `revoke_cohost` | ❌ host only |

> So: show **Edit meet** and the **join-approval queue** to the co-host; hide **Cancel
> event** and **Revoke** from them.

---

## 7. Error codes

RPCs raise these on the error path — map to friendly UI copy:

| code | meaning |
|---|---|
| `candidate_not_in_cluster` | candidate isn't in the host's block/cluster |
| `cohost_already_set` | event already has a co-host (one at a time) |
| `cohost_invite_pending` | an invite for this event is already waiting |
| `overlap_reason_too_short` | note is under 10 characters |
| `not_event_host` | only the event's host can invite |
| `cohost_invite_not_found` | invite missing / already responded / not yours |
| `event_not_found_or_no_cohost` | revoke target invalid or has no co-host |
| `not_authenticated` | no signed-in user |

---

## 8. Quick reference

| Screen / action | RPC |
|---|---|
| Open co-host picker | `get_cohost_candidates(p_event_id, p_search, p_limit)` |
| Tap Invite | `propose_cohost(p_candidate_id, p_overlap_reason, p_event_id)` |
| Host pending card | `get_my_cohost_invites('sent')` |
| Candidate action card | `get_my_cohost_invites('received')` |
| Accept | `accept_cohost_invite(p_invite_id)` |
| Decline | `decline_cohost_invite(p_invite_id)` |
| Revoke | `revoke_cohost(p_event_id)` |
| Message | existing chat RPCs + `thread_id` |
| Co-host edits event | `update_event` (existing) |
| Co-host approves joins | `get_my_event_requests` / `decide_event_request` (existing) |
