# TagAlng — Frontend Integration: Communication Model

This doc is the **Supabase RPC contract** for neighbor-to-neighbor comms:
**discover → connect → chat (nicknames) → reveal names → meet / swap**,
plus safety (block & report).

**Related docs (read together):**

| Doc | Use for |
|-----|---------|
| [`LANA_LAYER1_MANUAL_TEST.md`](LANA_LAYER1_MANUAL_TEST.md) | Lana chat turn fields (`signal_saved`, `block_log_entries`, `ui_intent`) |
| [`LANA_WHAT_WE_BUILT.md`](LANA_WHAT_WE_BUILT.md) | What is demo-ready vs not yet |

---

## 0. How to call everything

Most endpoints are **Postgres RPCs** — call by name with the logged-in user's JWT.

**Supabase JS (direct RPC):**
```ts
const { data, error } = await supabase.rpc('send_nudge', {
  p_recipient_id: otherUserId,
  p_context_message: 'Hi! Saw we both have toddlers',
});
```

**Lana worker (conversational path — PWA `/chat` today):**
```ts
// POST {LANA_WORKER_URL}/lana/sessions/{id}/messages
// Response includes assistant_message + structured cards (see §0.2)
```

Lana **calls RPCs on your behalf** for many flows (signals, block log, intros). You render the turn JSON; you do not need to call those RPCs from the PWA unless building a native screen.

### Auth
- User must be **signed in** (`auth.uid()` from JWT).
- Comms actions require **non-anonymous + phone-verified** — else `phone_not_verified` / `anonymous_user_comms_blocked`.

### Error shape
```json
{ "code": "P0001", "message": "blocked" }
```

### Return shape
- **Scalar** — UUID string or tier string (`"acquaintance"`).
- **Table** — array of rows.
- **JSON object** — noted inline (`save_local_signal`, `create_inquiry`, etc.).

### Action vs Read
- 🟢 **ACTION** — mutates state (send/accept/block/etc.).
- 🔵 **READ** — query for rendering a screen.

### Tier ladder
```
stranger → nudge → acquaintance → direct → irl_peer
```
`blocked` is separate (not a tier). Use 🔵 `get_relationship_tier` before rendering a neighbor.

---

## 0.1 Two swap systems (do not confuse them)

TagAlng has **two** ways to swap/give items on a block. They use different tables and UX.

| | **§2 Local signals + block log** | **§6 Marketplace** |
|---|----------------------------------|---------------------|
| **How user starts** | Talk to Lana: *"I want to give away my kid's bicycle"* | Native UI: list item → browse → Message |
| **Storage** | `local_signals` → matcher → `block_log_entries` | `marketplace_items` → `inquiries` → chat thread |
| **Match model** | Opposing intents on same block (`swap_seek` ↔ `swap_offer`) | Inquiry on a listing |
| **Chat opens** | After intro/nudge tier — not automatic from signal alone | `create_inquiry` opens inquiry thread |
| **Live in Lana chat today** | ✅ Yes | ❌ Not wired through Lana yet |
| **Live in PWA native screens** | Block log cards from Lana turns only | RPCs exist; full UI TBD |

**Example:** Kashaf saying *"swap my kids bicycle"* in Lana → `save_local_signal` with `swap_offer`. That is **not** `list_marketplace_item`.

### Signal intents (`local_signals.intent`)

| DB intent | Meaning | Layer 1 linear intent |
|-----------|---------|------------------------|
| `swap_seek` | Looking for an item | `looking.swap` |
| `swap_offer` | Offering / giving away an item | `sharing.swap` |
| `meet_seek` | Wants a neighbor to do something with | `looking.meet` |
| `host_meet` | Offering to host a meetup | `sharing.host` |
| `tip_seek` | Wants a local recommendation | `looking.tip` |
| `tip_share` | Sharing a recommendation | `sharing.tip` |

Matcher pairs **opposing** intents only (e.g. `swap_seek` ↔ `swap_offer`, `meet_seek` ↔ `host_meet`).

---

## 0.2 Lana integration map (v0.1)

What the **lana-worker** calls vs what **FE must call directly** for native screens.

| Capability | RPC / surface | Lana chat | PWA today |
|------------|---------------|-----------|-----------|
| Peer discovery | `find_peers_*` (via worker) | ✅ `peer_matches` | ✅ cards in chat |
| Post ask/offer (swap/meet/tip) | `save_local_signal` | ✅ `signal_saved` | ✅ card in chat |
| Block log / matches | `get_my_block_log`, `refresh_my_signal_matches` | ✅ `block_log_entries` | ✅ cards in chat |
| Dismiss/save match | `block_log_action` | ⚠️ RPC exists; FE gesture TBD | ❌ |
| Formal intro | `propose_intro`, `get_my_intros` | ✅ partial | ✅ list via Lana |
| Accept/decline intro | `accept_intro`, `decline_intro` | ✅ `tier.respond_nudge`* | ✅ via chat phrases |
| Nudge | `send_nudge`, `accept_nudge` | ⚠️ orchestrator tool only | ❌ no nudge inbox UI |
| 1:1 chat | `get_my_threads`, `send_message` | ❌ | ❌ |
| Unmask | `propose_unmask`, `accept_unmask` | ❌ | ❌ |
| Marketplace | `list_marketplace_item`, `create_inquiry`, … | ❌ | ❌ |
| Block/report | `block_user`, `report_message` | ⚠️ via intro decline path | ❌ |

\* **`tier.respond_nudge` is a misnomer in Layer 1** — it handles **intro** accept/decline/block (`accept_intro` / `decline_intro`), not `accept_nudge`. Nudge inbox is §1.

### Lana turn JSON (render these in `/chat`)

Inspect `POST .../messages` response. See [`LANA_LAYER1_MANUAL_TEST.md`](LANA_LAYER1_MANUAL_TEST.md) for test phrases.

| Field | UI |
|-------|-----|
| `assistant_message` | Lana bubble text |
| `ui_intent` | Which card template (`signal_saved`, `show_block_log`, …) |
| `active_intent` | Layer 1 intent (debug / analytics) |
| `peer_matches` | Neighbor preview cards |
| `block_log_entries` | Match log cards (swap/meet/tip matches) |
| `signal_saved` | “Posted to your block” confirmation |
| `identity_profile` | Claims card |
| `pending_intros` | Intro inbox |

**Note:** `matches_created` on save reflects **new** matcher rows for that save. Existing matches may already be in `block_log_entries` even when the reply says “I'll let you know when a neighbor matches.” Say **show my block log** to surface them.

---

## 1. Discover & connect — Nudge and Intro

> **Story:** AK sees AM in Discover. She **nudges** AM. AM **accepts** → shielded chat opens.
> Separately, Lana can **propose an intro** with a match reason; AM accepts → same outcome.

### 🟢 `send_nudge`
```ts
supabase.rpc('send_nudge', { p_recipient_id, p_context_message })
```
**Returns:** `nudge_id`. **Errors:** `recipient_not_on_block`, `nudge_rate_limit_daily`, `phone_not_verified`.

### 🟢 `accept_nudge` → `"acquaintance"` + shielded chat
```ts
supabase.rpc('accept_nudge', { p_nudge_id })
```

### 🟢 `decline_nudge`
```ts
supabase.rpc('decline_nudge', { p_nudge_id })
```

### 🔵 `get_my_nudges`
```ts
supabase.rpc('get_my_nudges', { p_direction: 'received' }) // or 'sent'
```

### 🔵 `get_my_intros`
```ts
supabase.rpc('get_my_intros', { p_direction: 'all' }) // or 'sent' | 'received'
```
**Lana:** “show my intros” → `pending_intros[]`, `active_intent: social.list_intros`.

### 🟢 `propose_intro`
```ts
supabase.rpc('propose_intro', {
  p_candidate_id,
  p_match_reason,
  p_shared_dimensions,
  p_match_score,
  p_joint_moment_id,
})
```
**Prerequisite:** tier ≥ `nudge` with candidate. **Errors:** `tier_too_low_send_nudge_first`, `match_reason_too_short`.

**Lana:** “introduce me to …” after peer preview → worker calls this (or equivalent).

### 🟢 `accept_intro` / `decline_intro`
```ts
supabase.rpc('accept_intro', { p_intro_id })
supabase.rpc('decline_intro', { p_intro_id })
```
**Lana:** user says “yes introduce” / “not now” on pending intro → `accept_intro` / `decline_intro` (via `tier.respond_nudge` handler).

### 🔵 `get_relationship_tier`
```ts
supabase.rpc('get_relationship_tier', { p_other_user_id })
```

---

## 2. Local signals & block log (Lana Layer 1)

> **Story:** AM tells Lana *“I'm looking for 3T rain boots”* or *“I want to give away my kid's bicycle.”*
> Lana saves a **local signal**, the matcher finds neighbors with the **opposite** intent on the same block,
> and results land in the **block log**. This is the live conversational swap/meet/tip path — not §6 Marketplace.

**Requires:** phone-verified user with `home_block_id` set.

### 🟢 `save_local_signal` — post ask or offer
```ts
const { data } = await supabase.rpc('save_local_signal', {
  p_intent: 'swap_offer',           // swap_seek | swap_offer | meet_seek | host_meet | tip_seek | tip_share
  p_detail_text: "kids bicycle",
  p_category: null,                 // optional; tips may use education|health|food|home|activities
  p_block_id: null,                 // defaults to home_block_id
  p_zip: null,
  p_stage: null,                    // optional size hint e.g. "3T", "adult"
});
```
**Returns (JSON):**
```json
{
  "signal_id": "uuid",
  "intent": "swap_offer",
  "detail_text": "kids bicycle",
  "block_id": "8a2a1072…",
  "matches_created": 1
}
```
`matches_created` = **new** `block_log_entries` rows written for this save (0 if no opposite signal or deduped within 24h).

**Errors:** `not_authenticated`, `invalid_intent`, `detail_required`, `block_required`.

**Lana:** user phrases like “looking for rain boots” / “give away my bicycle” → worker calls this after confirm cascade. FE reads `signal_saved` on the turn — do not call RPC from PWA unless building a non-Lana form.

### 🔵 `get_my_block_log` — pending matches for me
```ts
const { data } = await supabase.rpc('get_my_block_log', {});
```
**Returns:** array of rows (newest / strongest first, limit 20):

| Column | Notes |
|--------|--------|
| `id` | block log entry id |
| `match_type` | `inbound_for_my_seek`, `inbound_for_my_offer`, `meet_*`, `tip_match`, … |
| `peer_user_id` | neighbor (nickname hidden until verified in UI policy) |
| `peer_preview_label` | e.g. nickname or “A neighbor on your block” |
| `match_strength` | 0–1 |
| `match_reasons` | human-readable strings |
| `my_signal_detail`, `peer_signal_detail` | linked `local_signals.detail_text` (when migration applied) |
| `my_signal_intent`, `peer_signal_intent` | linked intents |
| `block_name` | block display name |

**Lana:** “show my block log” → worker calls `refresh_my_signal_matches` then `get_my_block_log` → `block_log_entries` on turn.

### 🟢 `refresh_my_signal_matches` — re-run matcher (optional)
```ts
const { data } = await supabase.rpc('refresh_my_signal_matches', {});
```
**Returns:** count of new rows inserted. Worker calls this before `get_my_block_log` when matches may be stale.

### 🟢 `block_log_action` — dismiss / save / nudge a match
```ts
await supabase.rpc('block_log_action', {
  p_entry_id: entryUuid,
  p_action: 'dismissed',  // nudged | dismissed | saved | ignored
});
```

### Match types (for FE filtering)

| `match_type` | When shown to me |
|--------------|------------------|
| `inbound_for_my_seek` | I was **seeking**; neighbor **offered** |
| `inbound_for_my_offer` | I **offered**; neighbor was **seeking** |
| `meet_attendee_potential` | I sought meet; neighbor hosts |
| `meet_invite_potential` | I host; neighbor sought meet |
| `tip_match` | tip seek ↔ tip share |

When rendering after a **swap offer** save, show only swap-related types (`inbound_for_my_offer`), not meet rows from older signals.

### SQL sanity check (admin / SQL editor)

`auth.uid()` is null in SQL editor as `postgres` — use explicit `user_id`:

```sql
select created_at, intent, detail_text, status
from public.local_signals
where user_id = 'YOUR_USER_UUID'
order by created_at desc
limit 20;
```

---

## 3. Chat (shielded & direct)

> **Story:** Connected neighbors chat under nicknames (`shielded`) or real names (`direct`).
> **Not wired in PWA v0.1** — RPCs exist for native chat screens.

### 🔵 `get_my_threads`
```ts
supabase.rpc('get_my_threads')
```
**Returns:** `{ thread_id, kind, other_user_id, other_nickname, tier, last_message_at, unread_count, … }`.

### 🟢 `send_message`
```ts
supabase.rpc('send_message', {
  p_thread_id,
  p_content,
  p_client_dedupe_key,  // fresh UUID per send — idempotent retries
  p_reply_to,
})
```

### 🔵 `get_thread_messages` · 🟢 `mark_thread_read` · 🟢 `delete_message`
See prior patterns; params unchanged.

---

## 4. Unmask (acquaintance → direct)

RPCs: `propose_unmask`, `accept_unmask`, `decline_unmask`. **Not wired in Lana/PWA v0.1.**

---

## 5. Group chats & meeting in real life

| Endpoint | Type | Notes |
|----------|------|--------|
| `create_event` | 🟢 | auto-creates group chat |
| `request_to_join_event` | 🟢 | |
| `decide_event_request` | 🟢 | approved → added to group chat |
| `get_my_group_threads` | 🔵 | group chat list |
| `confirm_irl_met` | 🟢 | mutual direct → `irl_peer` |

Group threads reuse §3 chat endpoints.

---

## 6. Marketplace (formal listings — not Lana voice swap)

> **Story:** AM lists “Toddler bike” in a **marketplace shelf**. AK browses, taps Message → inquiry chat.
> Handoff + completion + optional “Stay in touch.”

**This is separate from §2.** Lana conversational *“give away my bicycle”* does **not** create a `marketplace_items` row.

### 🟢 `list_marketplace_item`
```ts
supabase.rpc('list_marketplace_item', {
  p_title,
  p_description,
  p_intent_type: 'free',  // or 'swap' — no selling in v0.1
  p_category,
  p_photos,
  p_block_id,
})
```

### 🔵 `get_marketplace_items` · 🟢 `create_inquiry` · 🟢 `confirm_handoff` · 🟢 `confirm_completion` · 🟢 `mark_acquaintance_from_inquiry` · 🟢 `close_inquiry` · 🔵 `get_my_inquiries`

Params and return shapes unchanged from v1 doc. **RPCs exist; Lana + PWA marketplace UI not shipped.**

---

## 7. Safety — block & report

### 🟢 `block_user` · 🟢 `unblock_user` · 🟢 `report_message`

Work across chat, intros, and marketplace. Lana can call `block_user` when user declines an intro with “block.”

---

## 8. Realtime

Subscribe to Postgres changes (best-effort UX; RPCs are source of truth).

| Table | Watch for |
|-------|-----------|
| `messages` | incoming chat |
| `chat_threads` | new thread, unmask flip |
| `inquiries` | marketplace status |
| `block_log_entries` | new matches (optional; Lana turn is primary today) |

---

## 9. End-to-end happy paths

### A. Social graph (native screens — §1 + §3)
```
get_relationship_tier → send_nudge → accept_nudge → get_my_threads → send_message
→ propose_unmask → accept_unmask → confirm_irl_met
```

### B. Conversational swap (Lana — §2)
```
User: "I want to give away my kid's bicycle"
  → Lana: save_local_signal(swap_offer) → signal_saved card
  → If matches_created > 0: block_log_entries cards
  → Else: "I'll let you know…" (matches may already exist — say "show my block log")

User: "show my block log"
  → get_my_block_log → block_log_entries cards
```

### C. Formal marketplace (§6 — future native UI)
```
list_marketplace_item → get_marketplace_items → create_inquiry → chat → confirm_handoff → confirm_completion
```

---

## Quick reference

**🟢 Actions:** `send_nudge`, `accept_nudge`, `decline_nudge`, `propose_intro`, `accept_intro`, `decline_intro`, `save_local_signal`, `refresh_my_signal_matches`, `block_log_action`, `send_message`, `mark_thread_read`, `delete_message`, `propose_unmask`, `accept_unmask`, `decline_unmask`, `block_user`, `unblock_user`, `report_message`, `create_event`, `request_to_join_event`, `decide_event_request`, `confirm_irl_met`, `list_marketplace_item`, `create_inquiry`, `confirm_handoff`, `confirm_completion`, `mark_acquaintance_from_inquiry`, `close_inquiry`.

**🔵 Reads:** `get_relationship_tier`, `get_my_nudges`, `get_my_intros`, `get_my_block_log`, `get_my_threads`, `get_thread_messages`, `get_my_group_threads`, `get_marketplace_items`, `get_my_inquiries`.

**Lana turn fields (not RPCs):** `peer_matches`, `block_log_entries`, `signal_saved`, `identity_profile`, `pending_intros`, `ui_intent` — see [`LANA_LAYER1_MANUAL_TEST.md`](LANA_LAYER1_MANUAL_TEST.md).
