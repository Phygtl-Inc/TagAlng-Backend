# Lana Backend Architecture · TPR for Asjid

*Author · Tommaso (via concierge synthesis) · Date · 2026-06-08 · Version · 1.0 · Status · ready to act on*

*Source of truth contract: this document supersedes ad-hoc Slack threads and prior architecture sketches. Where it disagrees with `LANA_FRONTEND_SPEC/52-54`, those specs are the deeper canonical reference and this doc is the action briefing built on top of them. Where it disagrees with the legacy `LANA_AGENT_ARCHITECTURE_v1.md` Claude-references, **this doc wins** (OpenAI is locked per `00_INDEX §13`).*

---

## §0 · Reading order

You don't need to read 15 specs to start. Here is the order:

1. **Read §1 (Context)** to internalize what we are building and why the architecture is shaped the way it is.
2. **Read §2 (What you're building)** for the one-page mental model of the 4 communication paths + 2 Lana presence patterns.
3. **Read §3 (Tier ladder)** to lock the enum, invariants, and edges. Every other section assumes this in working memory.
4. **Read §4 (Schema)** as your migration plan. This is the substrate; nothing else compiles without it.
5. **Read §5 (State machines)** for the server-side authoritative transitions and 10 race conditions you must resolve. Pin this near your editor while implementing edge functions.
6. **Read §6 (Edge functions)** as your per-function build list. 46 functions; ordered by build-week in §9.
7. **Read §7 (AI integration)** for the OpenAI dispatcher pattern, the inline-hint mechanism, and the safety hold pipeline.
8. **Read §8 (Push notifications)** for the 18 trigger inventory and APNS payload contract.
9. **Read §9 (Implementation sequence)** for your 6-week schedule. Sprint 1 starts at Week 1 §9.
10. **Open spec files only when you need the verbatim CSS/markup of a frontend contract** — i.e. when wiring up the API shapes a screen expects. Specs 41-51 are the per-screen homes; §11 of this doc gives you the TypeScript types so you usually don't need to dig.

§10 (edge case catalog) and §12 (open questions) are reference material. §13 (out of scope) is the "we are not building" list — re-read whenever a feature request lands in your DM.

**Working in parallel with the frontend team.** Spec 53 §12 lists 12 race conditions; half have FE-side implications. Coordinate with Aki (FE) on those before sprint 3 so the optimistic-then-reconcile pattern is identical on both sides.

---

## §1 · Context (the founder's lens)

I have been speccing this thing for two months. Lana is the agentic concierge that replaces what was originally going to be a conventional TagAlng PWA navigation model. There is no bottom nav. The user does not pick a tab and then find a feature — the user talks to Lana and Lana decides whether to respond, ask, call a tool, or capture. The entire app surface is downstream of that single decision loop. Plan-Activity, Discover, Marketplace, Activity map, Profile — they are all reached by speaking to Lana or by tapping affordances that route through her.

This shape has consequences for the backend you are about to build. **It cannot be a regular CRUD backend with a thin AI layer bolted on.** The agentic surface is the navigation; the backend has to be designed as if every request might originate from a tool call, every read might be filtered by a relationship tier, and every chat surface has a server-side hook where Lana drops a personal hint that only the recipient can see. That is the difference between "a Supabase app with some OpenAI sprinkled in" and what we are actually building.

You own the Supabase project, the edge functions, the OpenAI integration, and the state machine implementation. You are the schema gatekeeper — migrations only, no dashboard edits — per the global CLAUDE.md rule and the standing team agreement. This document is the single briefing you can act on without reading 15 files; it is intentionally dense because the surface is dense, and I would rather hand you one 14,000-word doc you can grep than 15 separate specs you have to context-switch between.

**The new Supabase project is `lana-app-v01`.** Provision it in the Phygtl organization, separate from the existing PWA-v01 project (`rjlcyvwogmfmngemhbmn`). Do not migrate or share schema with the PWA project — Lana is a clean slate. Auth: email (magic link) + Apple + Google + phone+OTP. The OTP gate fires only at F2 (the introduce-action turn), not at signup landing. Mobile (React Native Expo) is in scope for v0.1 but web ships first — the backend you build serves both clients via the same edge functions and the same realtime channels. **OpenAI is locked** (gpt-4o for synthesis, gpt-4o-mini for cheap routing) — the legacy `LANA_AGENT_ARCHITECTURE_v1.md` document still says Claude Haiku/Sonnet in places; ignore those references, OpenAI is canonical per `LANA_FRONTEND_SPEC/00_INDEX §13`.

The core architectural decision that everything else descends from: **Lana is not a chatbot. She is the navigation chrome that mediates all peer-to-peer interactions.** The user never thinks "I am using a chat app" — the user thinks "I am doing the thing I came to do, with Lana making it smoother." This means the backend you build must:

- **Be agentic-aware.** Every API call may originate from a Lana tool call. The `/api/lana/conversation` endpoint is the front door; the 46 edge functions in §6 are the back doors that the tool dispatcher calls into. Each must be safe to call as a tool (idempotent, validated, scoped to the user's permissions).
- **Be tier-aware.** Every read of another user's data filters by the current relationship tier between the viewer and the target. We have a helper function `lana_current_tier(viewer, other)` and every RLS policy and every API response composes around it. Spec 41 §3 defines the per-tier visibility rules; spec 52 §3 implements the `users_public` view that exposes only the tier-safe fields. A leak here is the highest-severity bug class we can ship.
- **Be inline-hint-aware.** Every chat surface (shielded, direct, group, inquiry) has a server-pushed hook where Lana can drop a tooltip that only the recipient sees. This is new for v7.0 and you will not find it fully specced in the existing files. The architecture is: an event (message sent, time elapsed since last reply, etc.) fires the `draft_lana_hint` edge function, which calls gpt-4o-mini to produce a 1-2 sentence tooltip plus 1-3 CTA chips, persists into `lana_inline_hints`, and pushes via Realtime to the recipient on channel `lana_hints:{user_id}`.
- **Be context-aware.** When the user opens the Lana floating modal from any surface, the conversation API receives an `overlay_context` payload telling it what surface, focused entity, and recent action the user just had. This lets Lana respond contextually ("oh you're looking at Maria's profile — want me to set up a coffee with her?") without the user re-explaining where she is.
- **Be race-safe.** Mutual nudges, mutual unmask acceptances, mutual marketplace swap completions, mutual IRL-met confirmations — all can race within milliseconds. Every state transition needs an idempotency key and a row-level lock; spec 53 enumerates 10 race conditions and the resolution patterns. Some of them require coordinated FE/BE behavior; flag them as you build and we will fix the FE side in parallel.

The frontend team (Aki, Abdullah) is working off `LANA_FRONTEND_SPEC/40-55`. Your job is to make the backend match the FE contracts in those specs without surprising them. Every API shape in §11 below is what they expect. When in doubt, ask in the daily standup — but the contracts here are intended to be authoritative.

---

## §2 · What you're building (the one-page mental model)

You are building the backend for **peer-to-peer communication mediated by Lana** on a single launch block (East Park, Lake Nona, ~250 moms). There are exactly **4 communication paths** and exactly **2 Lana presence patterns**. Everything else is downstream of these six things.

### The 4 communication paths (all peer-to-peer · all Lana-involved)

1. **1:1 for meeting peers.** A user discovers another user (via Discover D1/D2 results, via the neighbor drawer on the activity map, via Lana's proactive D3 joint-moment proposal), taps Nudge, sends a Lana-drafted opener. The recipient gets a push, accepts, a shielded chat opens. They talk under nicknames. Either taps "Ask to unmask," both accept, real names reveal — they are now direct. This is the canonical path Lana sells the user.

2. **1:1 leading to an event.** Either at any point in path 1, or after, a user invites the other to plan an activity together (or just to attend one). The plan-activity slot-fill conversation runs in Lana, an activity is created, the other RSVPs, both attend, 24h grace passes, the cron auto-promotes them to irl_peer. Or the manual path: both tap "We met IRL" from inside the direct chat. Either way, the irl_peer tier unlocks the vouching UI (action deferred to v0.2) and the "Together so far" history section in the neighbor drawer.

3. **1:1 marketplace inquiry.** A user lists an item (free or swap only in v0.1 — no selling). Another taps "Message" on the item detail, opens an inquiry chat. Lana mediates. They negotiate (text), agree on a time and place. Both tap "Confirm in-person handoff." 6h after the scheduled time, Lana asks both "did the swap happen?" — both tap yes, completion records. A post-completion card optionally appears: "Stay in touch?" — if BOTH tap yes, the pair gets promoted to acquaintance (not irl_peer; that requires a hosted activity per the linear ladder). This is the only path from marketplace to the tier ladder, and it is opt-in.

4. **1:N group post-RSVP.** When an activity is published, a group chat is auto-created (single host membership). When attendees RSVP "going," they auto-join via pg trigger. Lana is **absent** from this group thread (no drafted chips, no system bubbles drafting messages for users); the only "system" content is bookkeeping (Helena added a bring-list item, Maria left, Helena moved the venue). Inside the group, members can still take **pairwise actions** between each other: selectively unmask one specific member, block one, invite one to plan another activity. The selective-unmask flow (new in v7.0) sends `propose_unmask` with `context='group_chat:{thread_id}'` and the rest of the unmask state machine runs identically to a 1:1.

### The 2 Lana presence patterns

These are the only two ways Lana shows up in the comms layer. Confusing them is the #1 way the backend can break the trust contract.

- **Inline Lana.** A server-pushed tooltip plus 1-3 CTA chips that appears inside a chat thread (shielded, direct, group, or inquiry). It is labeled "only you can see this." It is drafted by gpt-4o-mini based on the thread context, the user's profile, and the trigger event. It is **never user-summon-able** — Lana decides to drop one based on signals (message sent, time elapsed, marketplace handoff approaching, mutual-IRL eligibility detected). The recipient can dismiss it, tap a CTA, or ignore it. It is personal — Lana speaks for individuals only, never to the group. New table: `lana_inline_hints`.

- **Floating Lana.** A corner sheep icon (on every surface except Settings) opens a transparent canvas modal — the canvas inherits the screen context underneath. The modal mounts the Lana home conversation surface. The conversation API call includes an `overlay_context` payload: `{surface: 'neighbor_drawer', focused_entity: {kind:'user', id:'u_maria'}, recent_action: 'opened_profile'}` so Lana can respond contextually. **Lana is absent on Settings entirely** — no corner icon, no overlay route. The backend does not need a special check for this; the FE simply does not mount the corner icon on Settings routes. Document the rule because product/legal occasionally ask why.

### What this means for your backend

Every chat surface needs three hooks:
1. **The send pipeline** (`send_message` per §6.10) — validates, runs safety check, inserts, fans out push, triggers downstream effects (draft replies for inquiries, hint dispatcher for shielded/direct).
2. **The inline-hint hook** (`draft_lana_hint` per §7) — event-driven; produces hints and pushes them to specific users.
3. **The tier-gated read** — every message, every member render, every header label respects `lana_current_tier(viewer, other)` and `lana_is_blocked(viewer, target)`.

Every action surface (nudge, accept-nudge, unmask, block, report, RSVP, mark-IRL-met, mark-acquaintance-from-inquiry, etc.) is an edge function with:
- Zod input validation
- Idempotency key (computed or accepted from `X-Idempotency-Key` header)
- Auth + permission check (re-validated against data, not just JWT)
- Rate limit enforcement
- Transactional state mutation with row-level lock
- Realtime event emit after commit
- Push fanout (async)
- Structured telemetry

That is the pattern. Now read the rest.

---

## §3 · Communication tier ladder (canonical enum + state machine)

The tier ladder is the substrate. Every other piece of the backend references it. The user never sees the enum — she sees nicknames, sees shielded chat, sees "Ask to unmask," sees the green star pill on B5 — but engineers see this exact enum everywhere.

### The 5-tier canonical enum

```ts
// lib/relationship/tier.ts (shared between FE and BE)
export const TIER = ['stranger', 'nudge_pending', 'acquaintance', 'direct', 'irl_peer'] as const;
export type Tier = typeof TIER[number];

import { z } from 'zod';
export const TierSchema = z.enum(TIER);
```

Postgres mirror (§4 will spec the full schema):

```sql
CREATE TYPE tier AS ENUM ('stranger', 'nudge_pending', 'acquaintance', 'direct', 'irl_peer', 'blocked');
```

`blocked` is a parallel **lane**, not a tier, but it lives in the same enum because `tier_edges.current_tier` carries it when the pair is in the blocked state. The five real tiers are linear (no skipping); `blocked` is reached from any tier via the block action, and on unblock the restored tier is determined by the `restoredTier(preBlockTier, blockedUnblockable)` rule in §5.

### The 4 promotion edges (event-driven; no time-based auto-promotion except IRL)

| From | To | Trigger | Who confirms | Server event emitted |
|---|---|---|---|---|
| stranger | nudge_pending | `send_nudge` | sender only | `nudge.sent` + `tier.promoted` (one side) |
| nudge_pending | acquaintance | `accept_nudge` by recipient | recipient | `nudge.accepted` + `tier.promoted` (both sides) + `chat.shielded_opened` |
| acquaintance | direct | `propose_unmask` + `accept_unmask` by both | both | `unmask.accepted` + `tier.promoted` + `relationship.unmasked` |
| direct | irl_peer | auto: cron at `activity.ends_at + 24h` if both attended · manual: `confirm_irl_met` by both | server (auto) OR both users (manual) | `tier.promoted` + `relationship.irl_promoted` |

### The 3 exit-via-block paths

| From | To | Trigger | Restoration on unblock |
|---|---|---|---|
| any tier | blocked | `block_user` by either side | acquaintance (unmask undone, shielded chat re-opens with nicknames) |
| acquaintance | blocked | `block_user` | acquaintance (no degradation) |
| irl_peer | blocked | `block_user` with safety category | `blocked_unblockable=true` (requires support to unblock) |

### The 15 invariants (verbatim from spec 40 §9 — the founder cares about these word-for-word)

1. **Real name privacy.** A user can never see another user's real first name, real last name, real photo, or kids' names before BOTH users have completed the mutual-unmask flow. In acquaintance tier the only identifier shown is the Lana-generated nickname.
2. **Block precedence.** A shielded, direct, or group message MUST NOT be delivered to a recipient who has blocked the sender. The blocked party sees the block; future messages do not propagate.
3. **Nudge uniqueness.** A user cannot send a second nudge to the same recipient while a previous one is pending. Server returns 409 with `existing_nudge_id`.
4. **Marketplace inquiry uniqueness.** A user cannot open a second inquiry on the same item within 30 days of opening the first.
5. **Group chat membership.** A user can only post to a group chat if their RSVP status is 'going' for the linked activity. RSVP cancel removes posting rights immediately; read access is preserved.
6. **Drafted reply scope.** `draft_replies` only fires for shielded and inquiry chats. Direct, group, and other surfaces do not receive drafted replies in v0.1.
7. **Tier monotonicity except on explicit downgrade.** Tier is one-way unless the user explicitly toggles "shield this conversation" (Direct → Acq) or unblocks. IRL Peer can never downgrade in v0.1.
8. **Unmask requires both sides.** Unilateral propose puts the request in pending state; the other must accept to promote.
9. **Marketplace swap reveals address, not name.** Handoff confirm reveals exact addresses (for handoff coordination only, one-time display) but does not promote tier or reveal names. Tier promotion is a separate mutual opt-in.
10. **Push deduplication.** Server coalesces push events fired within 60s for the same `(user_id, trigger, payload_hash)` tuple.
11. **No push for cancellations.** RSVP cancels do not push the host. Negative-loop pushes are banned.
12. **Visitor cannot emit comms events.** Any peer-to-peer event must originate from `auth_state='signed_in'`. Visitor and otp_pending are rejected.
13. **Lana voice attribution.** Every push body and every Lana drafted reply is tagged `source:'lana'` in telemetry.
14. **Read-on-tap for shielded chats.** Lana does not silently observe shielded chat content. `draft_replies` reads context but is gated by user action; no background analyzer reads shielded chats.
15. **Group chat post-event persistence.** Group chats stay open forever (locked default). Server does not auto-archive group chats post-activity in v0.1.

### Invariant 16 (founder-locked 2026-06-08; permanent for v0.1)

16. **No auto-promotion via marketplace.** Completed marketplace inquiries never promote tier automatically — even after N successful swaps with the same pair. Promotion happens only via the explicit "Stay in touch" mutual opt-in card (spec 48 §6). Backend must enforce: `update_relationship_tier(trigger='inquiry_*')` is rejected unless `trigger === 'inquiry_mutual_opt_in'`.

These 16 invariants are non-negotiable for v0.1. If you find an implementation path that requires breaking one, escalate before writing the code.

### The 8 locked v0.1 defaults (cross-referenced from the mockup §6 ladder)

1. **Nudge expiry: 7 days, silent.** Server cron at expiry; sender sees "no reply," recipient never sees the expired nudge.
2. **Shielded → Direct: mutual consent only.** No partial unmask.
3. **IRL peer: first co-attend + 24h grace.** Auto-promotion fires `activity.ends_at + 24h` via cron, only if host marked both attended.
4. **Group chat: auto-join on RSVP.** No "request to join."
5. **Block visibility: blockee sees the block** (renders as "account deleted" placeholder — no leak that block specifically happened, but the contact disappears).
6. **Lana reads shielded chat on-tap only.** No background passive analyzer.
7. **Group chats stay open forever.** No auto-archive in v0.1.
8. **Vouching: IRL peer tier only.** Vouch action deferred to v0.2; tier is visible.

---

## §4 · Database schema (24 core tables + auxiliary, per spec 52)

This is your migration plan. Run in the order listed; the FK dependencies enforce most of the order naturally but a few constraints are added in later migrations to avoid circular FK creation.

### Conventions

- `snake_case` table and column names; plural table names.
- `id uuid PRIMARY KEY DEFAULT gen_random_uuid()` on every standalone table; composite PK on join tables.
- `created_at timestamptz NOT NULL DEFAULT now()` on every table.
- `updated_at timestamptz NOT NULL DEFAULT now()` on mutable tables with a `bump_updated_at()` trigger.
- All times UTC.
- `ENABLE ROW LEVEL SECURITY` on every table. RLS is the safety net; edge functions are the canonical permission boundary and run as `service_role` to bypass when needed.
- PII (phone, exact location) encrypted at rest via `pgsodium`.
- Migration file convention: `supabase migration new <descriptive_name>` → produces `YYYYMMDDHHMMSS_<name>.sql`. Run with `supabase db push`. CI runs `supabase db diff --linked` as a guardrail.

### Three SECURITY DEFINER helpers (centralize policy logic)

```sql
CREATE OR REPLACE FUNCTION lana_is_blocked(viewer uuid, target uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT EXISTS (
    SELECT 1 FROM user_blocks
    WHERE (blocker = viewer AND blocked = target)
       OR (blocker = target AND blocked = viewer)
  );
$$;

CREATE OR REPLACE FUNCTION lana_in_thread(thread uuid, viewer uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT EXISTS (
    SELECT 1 FROM chat_thread_members
     WHERE thread_id = thread AND user_id = viewer AND left_at IS NULL
  );
$$;

CREATE OR REPLACE FUNCTION lana_current_tier(viewer uuid, other uuid)
RETURNS tier LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT COALESCE(
    (SELECT current_tier FROM tier_edges
       WHERE from_user = viewer AND to_user = other),
    'stranger'::tier
  );
$$;
```

Every RLS policy that touches another user's data calls these. They are the only SECURITY DEFINER functions; everything else is SECURITY INVOKER.

### Enums (one migration; declared before any table)

```sql
CREATE TYPE tier AS ENUM ('stranger','nudge_pending','acquaintance','direct','irl_peer','blocked');
CREATE TYPE nudge_status AS ENUM ('sent','accepted','declined','expired','cancelled');
CREATE TYPE unmask_status AS ENUM ('pending','accepted_one_side','accepted','declined','expired','cancelled');
CREATE TYPE chat_kind AS ENUM ('shielded','direct','group_activity','inquiry');
CREATE TYPE inquiry_status AS ENUM ('open','committed','completed','closed','expired');
CREATE TYPE rsvp_status AS ENUM ('going','waitlist','cancelled','attended','no_show');
CREATE TYPE intro_proposal_status AS ENUM ('proposed','accepted','declined','expired');
CREATE TYPE invite_channel AS ENUM ('sms','whatsapp','share_link','phonebook','native_share');
CREATE TYPE message_delete_kind AS ENUM ('for_me','for_everyone');
CREATE TYPE moderation_report_status AS ENUM ('open','in_review','resolved','dismissed');
CREATE TYPE moderation_action_kind AS ENUM ('soft_suspend','hard_suspend','ban','message_hold','message_delete','tier_revoke','warn');
CREATE TYPE moderation_actor AS ENUM ('system','human_moderator','auto_safety');
CREATE TYPE appeal_status AS ENUM ('pending','approved','denied');
CREATE TYPE push_platform AS ENUM ('ios','web','android');
CREATE TYPE hold_reason AS ENUM ('detected_unsafe','rate_limit','suspended','pending_review');
CREATE TYPE rate_limit_bucket AS ENUM ('nudge_day','nudge_week','msg_shielded_day','msg_direct_day','inquiry_day','activity_create_day','report_day','unmask_propose_day');

CREATE TYPE notification_type AS ENUM (
  -- Tier ladder (5)
  'nudge_received','nudge_accepted','nudge_expired','unmask_proposed','unmask_accepted',
  -- Chat (5)
  'shielded_msg_received','shielded_msg_preview','direct_msg_received','group_msg_received','group_mention',
  -- Marketplace (2)
  'inquiry_received','inquiry_handoff_confirmed',
  -- Activity (5)
  'activity_invite','activity_rsvp_to_my_event','rsvp_reminder','intro_proposed','intro_received',
  -- IRL
  'irl_promoted',
  -- Moderation (1) + fallback
  'report_acknowledged','moderation_action','system'
);
```

### The 24 core tables

#### 1. `users` (extends auth.users)

Profile + identity + lifecycle state. Phone encrypted via pgsodium (decrypted only in edge functions that send SMS). A `users_public` view exposes only tier-safe fields (`id, display_name, nickname, avatar_url, avatar_blurhash, bio, block_id, last_seen_at`); the client queries the view, never the raw table.

Key columns: `id`, `display_name`, `first_name`, `last_name`, `nickname` (Lana-generated), `block_id` (FK), `phone_encrypted`, `phone_last4`, `phone_verified_at`, `email`, `avatar_url`, `bio`, `tier_seed_dimensions jsonb`, `language` (en/pt/es), `is_suspended`, `suspend_until`, `is_deleted`, `delete_grace_until` (14d window), `last_seen_at`, `push_notif_paused`, `quiet_hours jsonb`.

RLS: self read/update + public read of safe fields filtered by `lana_is_blocked` both directions. Trigger `handle_new_auth_user` creates the row on auth.users INSERT.

#### 2. `blocks` (geographic shards; v0.1 has 1 row: east_park)

`id`, `slug`, `display_name`, `h3_index` (H3 cell at res 9), `centroid geography(POINT,4326)`, `cohort_phase`. Seed `east_park` immediately. Public read.

#### 3. `tier_edges` (directed; two rows per pair)

The single source of truth for what tier two users are at. Every read of "what can I see about this person?" hits this table.

Key columns: `from_user`, `to_user`, `current_tier tier NOT NULL DEFAULT 'stranger'`, `tier_changed_at`, `nudge_id` (FK declared later), `unmask_pending_id` (FK declared later), `irl_peer_since`, `proof_artifact_id`, `blocked_unblockable boolean DEFAULT false`.

Constraint: `UNIQUE (from_user, to_user)` + `CHECK (from_user <> to_user)`.

Why two rows per pair: asymmetric states (`nudge_pending` for sender means "I'm waiting on her"; for recipient means "she nudged me"). Doubles writes on every transition but simplifies the asymmetric-state queries dramatically.

RLS: only the two parties read. Writes are SERVICE_ROLE-only (every transition goes through an edge function).

#### 4. `tier_events` (event-sourced audit log)

Every tier transition writes one row. Idempotency key on `sha256(edge_id || from_tier || to_tier || trigger || proof_artifact_id || day_bucket)` so duplicate edge-function invocations within a bucket are no-ops. Append-only; never UPDATE.

#### 5. `nudges`

`sender`, `recipient`, `opener_text`, `status nudge_status`, `sent_at`, `expires_at` (default sent_at + 7d), `resolved_at`, `cancellation_silent`.

Indexes: unique partial on `(sender, recipient) WHERE status='sent'` to enforce one-pending-per-pair. Index on `(expires_at) WHERE status='sent'` for the expiry cron. Index on `(sender, recipient, resolved_at DESC) WHERE status IN ('declined','expired')` for the 30d cooldown query.

RLS: sender always reads; recipient reads only if not (cancelled + silent).

#### 6. `unmask_requests`

`tier_edge_id`, `proposer`, `responder`, `status unmask_status`, `proposer_accepted_at` (set at INSERT), `responder_accepted_at`, `declined_at`, `declined_by`, `expires_at` (default proposed_at + 48h).

Unique partial index `(tier_edge_id) WHERE status='pending'` enforces one-pending-per-edge. The `accepted_one_side` enum value is RESERVED for a v0.2 revoke feature; in v0.1, since proposer auto-accepts at INSERT, we go pending → accepted directly when responder accepts.

#### 7. `chat_threads`

`kind chat_kind`, `tier_edge_id` (for shielded/direct), `activity_id` (for group), `inquiry_id` (for inquiry), `title` (group only), `created_by`, `last_message_at`, `archived_at`, `muted_globally`.

CHECK constraint enforces the right FK is set per kind. **Important resolution from QA:** on inquiry → tier promotion (the "Stay in touch" opt-in path), we do NOT mutate the inquiry thread's `kind`. Instead, `confirm_completion` INSERTs a brand new `chat_threads` row with `kind='shielded'` keyed to the new tier_edge, and the original inquiry thread stays as `kind='inquiry'` (archived).

#### 8. `chat_thread_members`

PK `(thread_id, user_id)`. `joined_at`, `left_at`, `last_read_at`, `muted`.

#### 9. `messages`

Hottest table. Index strategy biased to `(thread_id, sent_at DESC)`.

`thread_id`, `sender` (null for system/lana), `text`, `attachments jsonb`, `is_system`, `is_lana`, `reply_to`, `sent_at`, `delivered_at`, `read_by jsonb`, `deleted_at`, `deleted_kind`, `edited_at`, `hold_id` (FK declared later), `client_dedupe_key` (per-sender per-thread idempotency), `deleted_for_users jsonb DEFAULT '[]'` (per-user delete-for-me list).

A view `messages_visible_for` filters out per-user deletions. For large groups, may need to migrate to a `message_user_deletes` join table in v0.2 — for v0.1 single-block ~250 moms, the jsonb array is fine.

RLS: read if `lana_in_thread` AND sender not blocked. INSERTs blocked from client (forced through `send_message` edge function). Self-soft-delete allowed for own messages within 1h grace.

Trigger `bump_thread_last_message_at` after INSERT.

#### 10. `marketplace_items`

`seller`, `block_id`, `title`, `description`, `category`, `intent_type` ('free'|'swap'|'sell'), `price_cents`, `photos jsonb`, `status` ('active'|'reserved'|'sold'|'removed'|'expired'), `reserved_for`, `expires_at`.

**v0.1 enforcement: reject any `intent_type='sell'` with `price > 0`** — selling is deferred. The DB allows the column but the `create_inquiry` and listing edge functions reject. Adjust the CHECK constraint to `intent_type IN ('free','swap')` if you want a hard guard at the schema level (recommended).

#### 11. `inquiries`

`item_id`, `inquirer`, `seller`, `status inquiry_status`, `opening_text`, `handoff_when`, `handoff_where`, `handoff_lat`, `handoff_lng`, `committed_at`, `completed_at`, `closed_at`, `closed_by`, `expires_at` (default opened_at + 14d).

Unique partial index `(inquirer, item_id) WHERE status='open'` enforces one-open-per-pair-per-item.

#### 12. `activities`

`host`, `block_id`, `title`, `description`, `starts_at`, `ends_at`, `location_label`, `location_lat`, `location_lng`, `capacity`, `audience` ('block'|'direct_only'|'irl_only'|'custom'), `visibility` ('public'|'private'), `status` ('draft'|'published'|'cancelled'|'completed'), `cancelled_at`, `cancel_reason`.

#### 13. `activity_rsvps`

PK `(activity_id, user_id)`. `status rsvp_status`, `responded_at`, `attended_at`, `cancelled_at`.

**Add `rsvp_note text` column per spec 55 §8 #16** — max 280 chars, optional, displayed verbatim in host's H4 view and pushed in the `activity_rsvp_to_my_event` payload. Wiped on cancel.

#### 14. `intro_proposals`

`from_user`, `peer_a`, `peer_b`, `context_text`, `status intro_proposal_status`, `proposed_at`, `expires_at` (proposed_at + 72h).

#### 15. `invites`

`inviter`, `channel invite_channel`, `recipient_phone_hash`, `recipient_email_hash` (sha256 of normalized E.164 / lowercased email — GDPR-safe; we never store raw phone numbers of non-users), `recipient_name`, `handle_slug` (unique short slug for the share URL), `share_url`, `message_template`, `clicked_at`, `click_count`, `signed_up_user_id`, `signed_up_at`, `expires_at` (default 30d).

#### 16. `notifications`

`user_id`, `type notification_type`, `payload jsonb`, `deep_link`, `collapse_id` (for APNS dedup), `is_read`, `is_pushed`, `pushed_at`, `read_at`.

Index `(user_id, created_at DESC) WHERE is_read=false`.

#### 17. `push_tokens`

`user_id`, `device_token`, `platform push_platform`, `app_version`, `bundle_id`, `endpoint_arn` (web push), `p256dh`, `auth_key`, `is_revoked`, `last_seen_at`.

Unique `(user_id, device_token, platform)`.

#### 18. `moderation_reports`

`reporter`, `target_user`, `target_message_id`, `target_thread_id`, `category` ('harassment'|'spam'|'sexual'|'self_harm'|'threat'|'off_platform_ask'|'csam'|'other'), `description`, `context jsonb` (snapshot of surrounding 10 messages), `status moderation_report_status`, `reviewer`, `resolution_note`, `resolved_at`.

Targets cannot see they were reported (intentional per spec 49 moderation rules).

#### 19. `moderation_actions`

`target_user`, `related_report`, `kind moderation_action_kind`, `reason`, `actor moderation_actor`, `actor_user_id`, `expires_at` (for soft suspends), `rescinded_at`, `rescinded_by`.

Targets CAN read their own actions (so they know they're suspended).

#### 20. `user_blocks`

PK `(blocker, blocked)`. `reason`, `created_at`. Index on `(blocked)` for the reverse-lookup.

This is the canonical block table; the `blocks` table is the geographic shard. Confusing but historical naming. Considered renaming `blocks` → `geographic_blocks` but the slug `east_park` is widely referenced.

#### 21. `appeals`

`moderation_action_id`, `appellant`, `text`, `attachments jsonb`, `status appeal_status`, `submitted_at`, `resolved_at`, `resolver`, `resolution_note`.

Unique partial `(moderation_action_id) WHERE status='pending'` enforces one-pending-appeal-per-action.

#### 22. `visitor_sessions`

Anonymous F0/F1 capture before OTP gate. Client-generated UUID stored in localStorage; server-side mirror in this table.

`device_fingerprint`, `f0_transcript jsonb`, `f1_transcript jsonb`, `identity_claims jsonb`, `inferred_block_slug`, `ip_country`, `user_agent`, `upgraded_to_user_id`, `upgraded_at`, `expires_at` (default 24h).

**Per spec 55 §8 #19, recommended:** rename `f0_transcript` + `f1_transcript` → single `messages jsonb`; add `referred_by_user_id uuid REFERENCES users(id)` and `utm jsonb` columns. Not blocking — works at the API layer either way — but cleaner if done at migration time.

RLS: anon role has full read/write on its own row by unguessable UUID; safety is the UUID + 24h expiry.

#### 23. `lana_message_holds`

When the safety layer flags a message as unsafe pre-send, OR rate-limit guard fires, OR sender is soft-suspended, the message goes here instead of `messages`. A human moderator (or the sender via "Send anyway") reviews and releases.

`would_be_sender`, `would_be_thread_id`, `text`, `attachments`, `reason hold_reason`, `reason_category` ('hate'|'violence'|'sexual'|'self_harm'|'pii_leak'|'off_platform_ask'|'spam'|'other'), `send_anyway_allowed boolean DEFAULT true` (set false for hate/violence per spec 51 §M hard-block rule), `detector` ('openai_moderation'|'llama_guard'|'rate_limit_engine'|'manual'), `detector_score jsonb`, `released_at`, `released_by`, `released_message_id`, `denied_at`, `denied_by`.

#### 24. `rate_limits`

Sliding-window counters. PK `(user_id, bucket, window_start)`. `count`. Policy lives in `lib/rateLimits.ts` (code, not DB) so policy changes ship in a release rather than a migration.

### Auxiliary tables (declare alongside the 24 core)

- `idempotency_cache(key text PK, user_id, function, response jsonb, created_at)` — 24h TTL.
- `draft_replies_cache(thread_id, last_message_id, drafts jsonb, created_at)` — 30s TTL, GC every minute.
- `inquiry_handoff_confirmations(inquiry_id, user_id, when, where, lat, lng, confirmed_at)` — sub-table for bilateral handoff agreement.
- `inquiry_completion_confirmations(inquiry_id, user_id, at)` — sub-table for bilateral "did you meet?" confirmation.
- `inquiry_tier_promotions(inquiry_id, user_id, consented_at)` — sub-table for "Stay in touch" mutual opt-in.
- `mutual_irl_confirmations(user_a, user_b, confirmed_by, at)` — sub-table for manual IRL promotion.
- `irl_promotion_processed(activity_id PK)` — marker for "this activity's auto-IRL check was run" so the cron doesn't re-process.
- `lana_inline_hints(id, user_id, thread_id, text, cta_chips jsonb, context jsonb, expires_at, dismissed_at)` — NEW; the persistence layer for §7's inline hint pattern.
- `moderation_evidence.message_snapshots(...)` — separate schema, 90-day retention; SERVICE_ROLE only.

### Encryption, retention, replication (per spec 52 §23)

- **`users.phone_encrypted`** — pgsodium symmetric encryption, key in Supabase Vault, only decrypted in `send_otp` / `send_invite_sms` edge functions. Rotate annually.
- **`invites.recipient_phone_hash`** — sha256 of normalized E.164.
- **Attachments** — Supabase Storage `chat-attachments/` bucket, EXIF stripped on upload.
- **Soft-delete vs hard-delete:** users have a 14d grace via `delete_grace_until` (the 14d window IS the "double approval" per the global CLAUDE.md rule). Messages have 1h `for_everyone` window; `for_me` is permanent. Moderation: 7y retention. Visitor sessions: 24h.
- **PITR:** Supabase Point-in-Time Recovery enabled, 30-day window.
- **Daily logical dumps** to S3 separate AWS account, 90-day retention, lifecycle to Glacier after 30d.
- **Per-table backup priority tiers** — Tier 1 (RPO < 1m): `users`, `tier_edges`, `messages`, `chat_thread_members`. Tier 2 (RPO < 1h): `nudges`, `unmask_requests`, `inquiries`, `activities`, `activity_rsvps`, `notifications`. Tier 3 (RPO < 24h): `rate_limits`, `visitor_sessions`, `lana_message_holds`. Cold: `moderation_evidence.*`.

### Realtime publication

```sql
ALTER PUBLICATION supabase_realtime SET TABLE
  tier_edges, tier_events, nudges, unmask_requests,
  messages, chat_threads, chat_thread_members,
  inquiries, activity_rsvps, notifications, intro_proposals,
  lana_inline_hints;
```

Realtime respects RLS; clients only receive change events their policies admit.

---

## §5 · Server-side state machines

The FE XState machines are observers. The server is authoritative. If a client computes a transition the server rejects, the client rolls back. Per spec 53 §1.

### Universal invariants (always hold across every state machine)

1. **One writer per transition.** All state mutations go through edge functions. RLS denies direct client writes on tier_edges, messages, nudges, etc. Edge functions serialize concurrent requests via row-level locks (`SELECT ... FOR UPDATE`).
2. **Idempotency on every write.** Every transition computes a deterministic key, stored on the resulting event row.
3. **Block always wins.** Any transition between two users where either has blocked the other is rejected with `BLOCKED`.
4. **Realtime is best-effort, push is reliable.** Every event emits on Realtime AND queues for push. The server never assumes the client received an event.
5. **No tier promotion without proof.** Every `tier.promoted` carries `proof_artifact_id` (nudge UUID, unmask UUID, activity UUID, or the literal string `mutual_confirm`).
6. **Write-after-rails.** No memory/message commits until safety rails pass. `lana_message_holds` is the persistence boundary for failed-rails content.
7. **Server events are append-only.** `tier_events`, `moderation_actions`, `notifications` are never UPDATEd by edge functions; they are reconstructible from the event log.

### Tier edge state machine

ASCII (full per spec 53 §3):

```
   stranger ── send_nudge ──► nudge_pending
       ▲                            │
       │                  ┌─────────┼──────┐
       │                  ▼         ▼      ▼
       │             accepted   declined  expired
       │                  │
       │                  ▼
       │             acquaintance ── propose_unmask + both accept ──► direct
       │                  │                                              │
       │                  │                          co_attended + 24h   │
       │                  │                                              ▼
       │                  │                                          irl_peer
       │                  │                                              │
       │                  │            block (any tier)                  │
       │                  └─────────────────────────────────────────────►│
       │                                                                 ▼
       │                                                            blocked
       │                                                                 │
       └─────────────────────────────────────────  unblock ──────────────┘
                          (restored_tier = acquaintance unless pre-block was irl_peer non-unblockable)
```

Allowed transitions table (verbatim from spec 53 §3) — see edge function specs in §6 for the exact behavior of each. Idempotency keys per transition documented in §11 (the API contracts section).

The `restored_tier` rule on unblock:

```ts
function restoredTier(preBlockTier: Tier, blockedUnblockable: boolean): Tier {
  if (blockedUnblockable) throw new Error('Cannot unblock — contact support');
  if (preBlockTier === 'irl_peer') return 'irl_peer'; // grandfathered if not unblockable
  if (preBlockTier === 'direct' || preBlockTier === 'acquaintance') return 'acquaintance';
  return 'stranger';
}
```

`blocked_unblockable=true` only when block originates from `irl_peer` AND reason category is `harassment`/`threat`/`sexual`/`self_harm`/`csam`.

### Nudge state machine

```
sent → accepted (terminal · promotes tier)
sent → declined (terminal · silent to sender · 30d cooldown)
sent → expired (terminal · cron 7d · sender only sees "no reply")
sent → cancelled (terminal · sender-initiated · silent to recipient)
```

Idempotency: `accept:{nudge_id}` vs `decline:{nudge_id}` vs `cancel:{nudge_id}` — distinct keys but the row guard rejects the second after first wins.

### Unmask state machine

```
pending → accepted (terminal · when responder accepts; proposer auto-accepted at INSERT)
pending → declined (terminal · 48h cooldown · silent-ish per founder open question)
pending → expired (cron 48h · both sides notified)
pending → cancelled (block / account delete cascade)
```

Mutual-accept race: both call `accept_unmask` within ms. Server idempotent on `request_id`; SELECT FOR UPDATE serializes; second call returns idempotent success (the `tier.promoted` event was already emitted on the first).

### Inquiry state machine

```
open → committed (both confirmed handoff time+place)
open → closed (either closes manually)
committed → completed (both tap "did the swap happen?" → Yes; cron 6h post-handoff)
open/committed → expired (14d no activity)
any → closed (moderator close)
```

The off-path states (`cancelled` after handoff confirm, `declined_by_seller`, `closed_by_seller`, `seller_suspended`, `blocked`, `item_removed`) are documented in spec 48 §5 — each maps to a banner + read-only state and a system-message in the thread.

Per founder lock (invariant 16), `completed` does NOT auto-promote tier. Promotion is a separate opt-in via the `inquiry_tier_promotions` sub-table — both must consent, then a new `chat_threads(kind='shielded')` row is INSERTed alongside the archived inquiry thread.

### Moderation state machine

```
report → open → in_review → resolved / dismissed
                              ↓ (if resolved upheld)
                         moderation_action (warn / soft_suspend / hard_suspend / ban / message_hold / message_delete / tier_revoke)
                              ↓
                         appeal → pending → approved (rescind) / denied
```

Auto-escalation rules (codify in `report_message` edge function per spec 51 §B):
- 1 report in 24h on target → record only, no action.
- 3 reports in 7d (different reporters) → auto `soft_suspend` 24h.
- 5 reports in 7d → auto `hard_suspend` 7d + queue human review.
- Llama Guard severity ≥ 2 on a message → immediate `message_hold` + report.
- Category in (`threat`, `csam`, `self_harm`) → immediate `hard_suspend` + Slack alert.

### The 10 race conditions you must resolve (per spec 53 §12)

These are the production-realistic race conditions. Each has a server-side resolution mechanic; some have FE implications.

1. **Mutual nudge → instant acquaintance.** A and B nudge each other within seconds. Detected in `send_nudge` by querying `SELECT 1 FROM nudges WHERE sender=$recipient AND recipient=$sender AND status='sent'`. If hit, auto-promote: mark both nudges accepted, promote both tier_edges to acquaintance, INSERT one shielded chat (not two — the second's would-be chat is discarded), emit single merged event `nudge:mutual_match`. Idempotency key: `mutual_match:{LEAST(a,b)}:{GREATEST(a,b)}:{day}`. **FE TODO (Aki):** add `MUTUAL_MATCH` event to the conversation machine so the merged Lana opener ("You both reached out at the same time. That's rare.") renders.

2. **Block during fanout.** Recipient blocks sender between `send_message` INSERT and the push fanout for the in-flight message. We let the in-flight message deliver (the push payload already left); on the recipient's next thread open, the thread is archived (per block cascade) and the message stays in their history but the sender renders as the placeholder. **FE TODO:** spec 27 chats list and spec 28 chat thread should specify rendering when `last_message.sender` is now blocked. Suggested: render the row with `"Message hidden"` substituting the preview, and the avatar greyed.

3. **Activity cancel → RSVP cascade.** Host cancels an activity. `activity_rsvps` rows are not bulk-updated (they remain with `status='going'` but `activities.status='cancelled'`). FE needs to filter on `activities.status` everywhere it reads RSVPs. **FE TODO:** spec 25 event detail and spec 27 chats list should clarify cancelled-activity display. Suggested: show a red banner "Cancelled" at the top of the event detail; in chats list, show the group chat with a `[cancelled]` greyed tag.

4. **Inquiry close-vs-complete race.** Inquirer taps "Close inquiry" same instant seller taps "Mark complete." First write wins via row lock. The losing side sees a toast. **FE TODO:** spec 19 should specify toast copy. Suggested: "Sara just closed this inquiry" (loser is the close-taper if completion won) / "Sara just confirmed the swap" (loser is the closer if completion lost).

5. **Multi-device optimistic divergence.** User has iOS app + web open. Action on one device fires events the other observes. Both reconcile from event. **Pattern:** tag every optimistic local write with a `pending_op_id`; the realtime handler clears the pending flag when the matching event arrives. The actor device treats its own action as authoritative until mirrored.

6. **Suspension mid-session.** User is mid-typing when their `soft_suspend` fires. Server-side: `send_message` checks `moderation_actions` on every call. **FE TODO:** spec 28 chat thread should re-render the send button as disabled on `moderation:suspended` event, with placeholder text "sending paused while we review."

7. **Delete-grace undelete-on-login.** User deleted account but signs in within 14d grace. Login API should detect `is_deleted=true AND delete_grace_until > now()` and surface an `undelete` prompt. **FE TODO:** spec 32 settings + login route missing this branch. Suggested: a Lana modal "You started to delete your account. Want to undo that?" with Yes/No.

8. **Auto-IRL promotion mid-direct-chat.** Cron promotes two users to irl_peer while one is in their direct chat. The realtime `tier:promoted` arrives mid-thread. **FE TODO:** spec 28 should specify the "you just became IRL peers" inline toast presentation. Spec 41 §7 defines the toast component (`JointMomentToast`); the per-chat presentation is missing.

9. **Notification collapse-id under high frequency.** 10 messages arrive in 1 second on the same thread. APNS delivers only the latest (collapse_id = `msg:{thread_id}`); notifications table still gets 10 rows. Client should batch its `mark_read` calls accordingly.

10. **OTP gate double-tap.** User taps "introduce me" twice in quick succession on F2. `send_otp` idempotent on `(phone_e164, request_id)` for a 60s window; second tap returns same OTP without resending SMS. **FE TODO:** disable the introduce CTA for 5s after first tap regardless.

For each race, the server-side resolution is already in the edge function specs in §6. Coordinate FE handling at the kickoff of sprint 3.

---

## §6 · Edge functions catalog (46 functions)

The 46 functions, organized by domain. Each entry: name · trigger · key behavior · idempotency · permissions. Full Zod input shapes in §11.

Every function runs through the common middleware (auth, idempotency cache, rate limit, trace, error envelope). The envelope shape:

```ts
type EdgeResponse<T> =
  | { ok: true; data: T; idempotency_hit?: boolean; trace_id: string }
  | { ok: false; error: { code: ErrorCode; message: string; field?: string; retry_after?: number }; trace_id: string };
```

### Tier ladder (5 functions)

#### 1. `send_nudge` (POST)
Inputs: `{recipient_id, opener_text?}`. Validates pair, block check, suspension check, rate limit (`nudge_day` + `nudge_week`), 30d cooldown check, mutual-nudge collision detection (auto-promote if hit), INSERT nudge, UPDATE both tier_edges → `nudge_pending`, INSERT tier_event, emit `nudge:sent` to recipient, push. Idempotency: `nudge:{sender}:{recipient}:{date}`.

#### 2. `accept_nudge` (POST)
Inputs: `{nudge_id}`. SELECT FOR UPDATE, verify recipient + status='sent', UPDATE status='accepted', promote both tier_edges → acquaintance, INSERT tier_event, INSERT shielded `chat_threads` + two `chat_thread_members`, insert Lana opener message, emit `nudge:accepted` + `tier:promoted` + `chat:shielded_opened`, push to sender. Idempotency: `accept:{nudge_id}`.

#### 3. `decline_nudge` (POST)
Inputs: `{nudge_id}`. UPDATE status='declined', revert tier_edges → stranger, INSERT tier_event, emit `nudge:declined` to RECIPIENT only (sender silent). NO push. 30d cooldown starts.

#### 4. `cancel_nudge` (POST)
Inputs: `{nudge_id}`. Sender-initiated. UPDATE status='cancelled' with `cancellation_silent=true`. Revert sender's tier_edge. Emit `nudge:cancelled` to sender only.

#### 5. `expire_nudges` (cron, hourly at :05)
SELECT all `nudges WHERE status='sent' AND expires_at < now() FOR UPDATE`. Batch mark expired, revert tier_edges, emit `nudge:expired` to sender only. Performance budget: < 5s for 10k pending.

### Unmask (4 functions)

#### 6. `propose_unmask` (POST)
Inputs: `{other_user_id}`. Verify tier='acquaintance', no existing pending, rate limit `unmask_propose_day`. INSERT unmask_requests with `proposer_accepted_at=now()`. UPDATE tier_edges.unmask_pending_id. Emit `unmask:proposed`, push to responder. Idempotency: `propose_unmask:{tier_edge_id}:{date}`.

#### 7. `accept_unmask` (POST)
Inputs: `{request_id}`. SELECT FOR UPDATE, verify responder + status='pending'. UPDATE responder_accepted_at, status='accepted'. Promote both tier_edges → direct. INSERT tier_event. Emit `unmask:accepted` + `tier:promoted` + `relationship:unmasked` on both sides. Push to both. Idempotency: `accept_unmask:{request_id}:both`. Same chat thread continues (don't UPDATE `kind`; FE renders direct styling by reading the tier).

#### 8. `decline_unmask` (POST)
Inputs: `{request_id}`. UPDATE status='declined'. Emit `unmask:declined` to PROPOSER only. 48h cooldown.

#### 9. `expire_unmasks` (cron, hourly at :15)
SELECT all pending past `expires_at`. Mark expired. Emit `unmask:expired` to both.

### Messages (3 functions)

#### 10. `send_message` (POST · the single highest-throughput function)
The canonical pipeline. Performance: p95 < 400ms (excluding safety_check). Detailed in spec 54 §12 — read it. Briefly:

1. Idempotent return on `client_dedupe_key`.
2. Auth + `lana_in_thread`.
3. Sender state (not suspended).
4. Block check per other member.
5. Rate limit (bucket by chat kind).
6. Safety check (800ms budget, parallel; per §7). On hold → INSERT `lana_message_holds`, emit `message:held` to sender, return `{status:'held', hold_id}`.
7. INSERT messages.
8. Emit `message:sent` on `thread:{thread_id}`.
9. Fanout: per other member, INSERT notification + queue push (unless muted/quiet hours).
10. **For inquiry threads:** trigger `draft_replies` (async, fire-and-forget, cached 30s).
11. **For shielded threads:** event-driven trigger of `draft_lana_hint` (per §7 — checks if Lana should suggest something to the recipient).
12. Return `{message_id, sent_at, status:'sent'}`.

Idempotency: client-provided `client_dedupe_key uuid`.

#### 11. `delete_message` (POST)
Inputs: `{message_id, kind: 'for_me'|'for_everyone'}`. `for_me`: jsonb append to `deleted_for_users`. `for_everyone`: verify sender + within 1h, UPDATE tombstone, queue storage cleanup, emit `message:deleted`.

#### 12. `mark_thread_read` (POST)
Inputs: `{thread_id, up_to_message_id}`. UPDATE chat_thread_members.last_read_at + messages.read_by. Emit `message:read` events (batched).

### Block + Report (3 functions)

#### 13. `block_user` (POST · single transaction)
INSERT user_blocks, cancel pending nudges (both directions), cancel pending unmasks, determine pre-block tier, set `blocked_unblockable` flag if irl_peer + safety category, UPDATE both tier_edges → blocked, INSERT tier_event, archive shared chats, emit `relationship:blocked` to blocker only.

#### 14. `unblock_user` (POST)
Verify not `blocked_unblockable`. DELETE user_blocks. Compute `restored_tier` per the rule. UPDATE both tier_edges → restored. Un-archive last chat. INSERT tier_event. Emit `relationship:unblocked`.

#### 15. `report_message` (POST)
Rate limit `report_day`. Resolve target_user. Snapshot 10 surrounding messages → jsonb + copy to `moderation_evidence.message_snapshots`. INSERT moderation_reports. Auto-escalation rules per §5 moderation state machine. Emit `moderation:report_received` to reporter only.

### Inquiry (5 functions)

#### 16. `create_inquiry` (POST)
Verify item active, inquirer != seller, block check, rate limit `inquiry_day`. **Reject if `intent_type='sell' AND price > 0`** (no selling v0.1). INSERT inquiries (unique-partial-index prevents duplicate-open). INSERT chat_threads(kind='inquiry'). INSERT members. INSERT inquirer's opening message. INSERT Lana mediation system message. Emit `inquiry:opened`. Push to seller.

#### 17. `confirm_handoff` (POST)
Inputs: `{inquiry_id, when, where, lat?, lng?}`. INSERT sub-table `inquiry_handoff_confirmations`. If both rows present AND when/where match (with tolerance), UPDATE inquiries.status='committed'. INSERT Lana confirmation system message. Queue calendar reminder at when - 4h. Emit `inquiry:committed`.

#### 18. `confirm_completion` (POST)
Inputs: `{inquiry_id}`. Sub-table `inquiry_completion_confirmations`. On both confirmed: UPDATE inquiries.status='completed'; marketplace_items.status='sold' (or stays based on intent).

**Tier-promotion bridge (separate consent · per spec 48 + invariant 16).** Completion alone does NOT promote tier. Promotion is opt-in: each party taps "Stay in touch" → INSERT sub-table `inquiry_tier_promotions(inquiry_id, user_id, consented_at)`. On both consents:
1. Verify pre-promotion tier is `stranger` (if higher, skip).
2. Promote both `tier_edges` rows from `stranger` to `acquaintance`. Set `proof_artifact_id = inquiry_id` and `via_inquiry_id = inquiry_id`.
3. INSERT tier_event with idempotency `acq_via_inquiry:{inquiry_id}`.
4. **INSERT a NEW `chat_threads` row** with `kind='shielded'`, `tier_edge_id=<new_edge>`. INSERT both as `chat_thread_members`. Insert Lana opener: "You and {nickname} met for the swap. The chat stays open if you want it."
5. Emit `tier:promoted` + `chat:shielded_opened` on both user channels.
6. Push to both only if both consented within same 5min window; if delayed second consent, only in-app inline recap.

Per invariant 7 (tier monotonicity), this lands at `acquaintance` not `irl_peer`. A future co-attended hosted activity can then auto-promote per spec 46.

#### 19. `close_inquiry` (POST)
Inputs: `{inquiry_id, reason?}`. Either party. UPDATE status='closed', closed_at, closed_by. Archive thread. Emit `inquiry:closed`.

#### 20. `expire_inquiries` (cron, daily 03:30 UTC) + **NEW: 2-day swap-response auto-publish cron**
Standard expiry: UPDATE WHERE status IN ('open','committed') AND updated_at < now() - 14d SET status='expired'.

**NEW per v7.0/v7.1 mockup decisions:** for swap-style inquiries where the proposer offered an item to the seller in exchange and the seller hasn't responded within 2 days (`proposed_at + 48h`), auto-publish the proposer's offered item back to the marketplace OR close the inquiry, per the founder lock. Behavior:
- Cron at 48h post-proposal checks inquiries with no seller response.
- If proposer had attached a swap-offer item, that item's `status` flips back to `active` on the proposer's marketplace shelf.
- Close the inquiry with reason='expired_no_seller_response'.
- Notify the proposer via inline recap (NOT push — low priority).

### Activity (6 functions)

#### 21. `create_activity` (POST)
Validate, rate limit `activity_create_day`, INSERT activities. **Triggered pg-trigger `auto_create_group_chat` (§29)** INSERTs chat_threads(kind='group_activity') + INSERTs host as member. Emit `activity:created` on block channel.

#### 22. `rsvp_activity` (POST)
Verify published + not full (auto-flip to waitlist if full). INSERT activity_rsvps with `rsvp_note text` (max 280 chars). **Triggered pg-trigger `add_to_group_on_rsvp` (§27)** INSERTs chat_thread_members on `status='going'`. Send system message in group ("Maria is going · pão de queijo" — note appears verbatim). Emit `activity:rsvp`.

#### 23. `cancel_rsvp` (POST)
UPDATE status='cancelled'. **Triggered pg-trigger `remove_from_group_on_cancel` (§28)** sets chat_thread_members.left_at. System message ("Maria can no longer attend").

#### 24. `cancel_activity` (POST · host only)
UPDATE activities.status='cancelled'. Notify all RSVPs via push + system message in group. Emit `activity:cancelled`.

#### 25. `attendance_to_irl_check` (cron, every 15 min)
For each activity with `ends_at + 24h < now()` not yet processed, scan all pairs of `attended` RSVPs (both with `attended_at IS NOT NULL`), promote each pair from direct → irl_peer if not blocked. Idempotency: `irl_auto:{activity_id}:{LEAST(a,b)}:{GREATEST(a,b)}`. INSERT `irl_promotion_processed(activity_id)` marker after run.

#### 26. `confirm_irl_met` (POST · manual path)
Sub-table `mutual_irl_confirmations`. On both confirms within 7d, promote. Idempotency: `irl_manual:{user_a}:{user_b}`.

### Group chat (3 pg triggers)

#### 27. `add_to_group_on_rsvp` (pg trigger on activity_rsvps)
On INSERT or UPDATE where NEW.status='going' AND (OLD IS NULL OR OLD.status<>'going'): INSERT chat_thread_members.

#### 28. `remove_from_group_on_cancel` (pg trigger)
On status transition to cancelled: UPDATE chat_thread_members.left_at=now().

#### 29. `auto_create_group_chat` (pg trigger on activities)
On INSERT where status='published': INSERT chat_threads(kind='group_activity', activity_id=NEW.id, title=NEW.title, created_by=NEW.host) + INSERT chat_thread_members for host.

### AI (5 functions)

#### 30. `draft_replies` (async, called by send_message for inquiry/shielded threads)
Cache lookup on `(thread_id, last_message_id)`, 30s TTL. Load last 8 messages, thread tier, Lana voice constraints. Call gpt-4o-mini with structured prompt (see §7). Validate response (array of 3 strings, ≤15 words each). **Post-generation: run `safety_check_message` on each chip; drop unsafe chips rather than fall back to garbage** (per spec 55 §8 #21). Persist cache. Return `{drafts: [string, string, string]}`. Cost: ~$0.0001/call. Budget alert at $50/day.

#### 31. `draft_lana_hint` (NEW · the inline hint pattern)
Triggered by events (message sent + recipient hasn't replied in N min, marketplace handoff approaching, mutual-IRL eligibility detected, etc.). Inputs: `{user_id, thread_id, context: {trigger, focused_entity, recent_action}}`. Call gpt-4o-mini with system prompt + thread context (last 20 messages, respecting tier-gating) + the trigger context. Output: `{text: string, cta_chips: [{label, action}]}`. INSERT into `lana_inline_hints`. Push to user via Realtime channel `lana_hints:{user_id}`. Hint expires when user dismisses OR action is taken OR 5 min idle (cron sweep).

System prompt skeleton (compile per surface):

```
You are Lana. You are about to drop a private tooltip in {user_first_name}'s {thread_kind} chat with {other_nickname_or_name}. Only {user_first_name} will see this.

Context:
- Recent messages: {last_5_messages_summary}
- Trigger: {trigger_description}
- Relationship tier: {tier}

RULES:
- 1-2 sentences max.
- No PII (no names of others, no addresses, no kid names).
- Warm, observational, never lecturing.
- If suggesting an action, also include 1-3 CTA chips with concrete actions.

Return JSON: { text: "...", cta_chips: [{label: "...", action: "..."}] }
```

CTA chip actions are well-known strings the FE knows how to dispatch: `propose_unmask`, `mark_as_acquaintance`, `confirm_completion`, `open_plan_activity`, `dismiss`, etc.

#### 32. `safety_check_message` (internal, called by send_message)
OpenAI moderation API (free) + Llama Guard 3 8B (Replicate, ~150ms, $0.0002/call) + heuristics (phone-pattern, off-platform handles, URLs). 800ms total timeout; **fail open for text < 200 chars** (acceptable risk, flagged for tuning). Return `{decision: 'pass'|'hold'|'hard_blocked', reason?, detector?, scores?}`.

Hard-block categories (`hate`, `violence` at high confidence): set `send_anyway_allowed=false` on the hold so `override_held_message` rejects with `hard_blocked`. User agency: all other holds allow "Send anyway" per spec 51 invariant 13.

After 3 holds in 7 days, auto-flag user for soft-suspend review per moderation auto-escalation rules.

#### 33. `override_held_message` (POST · the "Send anyway" path)
Inputs: `{provisional_id}`. SELECT FOR UPDATE, verify sender. If `send_anyway_allowed=false`, return `{status:'hard_blocked'}` + log `moderation_actions(kind='warn', actor='auto_safety')`. Otherwise INSERT into messages with `hold_id` linkage, mark hold released, queue async human review via Slack `#safety-alerts`, emit `message:sent`. Telemetry: `message_overridden_by_sender`.

#### 34. `submit_appeal` (POST)
Inputs: `{moderation_action_id, text, attachments?}`. One-pending-per-action enforced. Webhook to Slack `#moderation-appeals`. Emit `moderation:appealed`.

#### (admin) `process_appeal` (POST · admin only)
Admin validated via service_role key header. UPDATE appeals.status. If approved, call `rescind_moderation_action`. Push to appellant.

### Account lifecycle (5 functions)

#### 35. `delete_account_grace_check` (cron, daily 04:00 UTC)
SELECT users WHERE is_deleted=true AND delete_grace_until < now(). For each: anonymize PII (`display_name='Deleted user'`, first_name/last_name/nickname/avatar_url/bio/phone_encrypted/email = NULL), DELETE auth.users (cascades), tombstone messages (sender set to NULL via FK ON DELETE SET NULL).

#### 36. `propose_intro` (POST · spec 11)
Server-mediated intro proposal between two strangers Lana detected as overlap. Creates `intro_proposals` row. Returns `{intro_id, joint_overlap_present}`.

#### 37. `accept_intro` (POST)
On accept: same effect as a mutual nudge — auto-promote both to acquaintance, INSERT shielded chat.

#### 38. `decline_intro` (POST)
UPDATE status='declined'. Silent.

#### 39. `snooze_intro` (POST)
Inputs: `{intro_id, duration: '24h'|'never'}`. UPDATE snooze_until.

### Invites (2 functions)

#### 40. `create_invite` (POST)
Inputs: `{channel, recipient_phone?, recipient_email?, recipient_name?, message_template?}`. Hash phone/email; generate handle_slug; INSERT invites; return `share_url`.

#### 41. `redeem_invite` (POST · called on landing page hit)
Inputs: `{handle_slug}`. UPDATE clicked_at, click_count++. On signup completion via the attribution flow: link `signed_up_user_id`.

### Auth + visitor (4 functions)

#### 42. `start_visitor_session` (POST · anonymous)
INSERT visitor_sessions with client-generated UUID. Rate limit by `device_fingerprint`. Return session.

#### 43. `upgrade_visitor_to_user` (POST)
Called post-OTP. Link `upgraded_to_user_id`. Migrate identity_claims to the users row.

#### 44. `send_otp` (POST)
Rate limit 5 sends per phone per hour. Phone decryption via pgsodium. Send via Twilio Verify API. Idempotent on `(phone_e164, request_id)` for 60s window.

#### 45. `verify_otp` (POST)
Verify OTP via Twilio Verify. On success: create or sign in user, write phone_verified_at, return session JWT.

### Infrastructure (3 functions)

#### 46a. `register_push_token` (POST)
Inputs: `{device_token, platform, app_version, bundle_id, endpoint_arn?, p256dh?, auth_key?}`. UPSERT push_tokens, idempotent on `(user_id, device_token, platform)`.

#### 46b. `enforce_rate_limit` (internal helper)
Per spec 54 §47 implementation. UPSERT rate_limits row, check count vs policy, throw RateLimitError on overflow.

#### 46c. `push_fanout_worker` (queue worker)
Long-running Deno worker on Modal (or Supabase Function with cron-pull). Drains queue from `pg_notify` triggered by `notifications` INSERTs. Constructs APNS/FCM payloads per §8. Honors quiet hours + per-thread mute + global push pause + token revocation on bad-token errors.

---

## §7 · AI integration (OpenAI · NOT Claude)

OpenAI is **locked** per `LANA_FRONTEND_SPEC/00_INDEX §13`. Ignore Claude references in `LANA_AGENT_ARCHITECTURE_v1.md` — those docs predate the lock. Two models:

| Model | Use case | Latency budget |
|---|---|---|
| `gpt-4o` | Synthesis turns — Discover synthesis (`explain_match`), Plan-Activity confirmation echo, F0/F1/F2 hero turns, complex multi-tool routing | p95 < 1.8s |
| `gpt-4o-mini` | Cheap routing — intent classification, drafted replies (`draft_replies`), inline hints (`draft_lana_hint`), captures (`capture_inquiry`), safety heuristics | p95 < 600ms |

`text-embedding-3-small` for pgvector embeddings (identity_claims, neighbor_facts, inquiry_signals) — p95 < 200ms.

### The dispatcher pattern (the canonical envelope)

The `/api/lana/conversation` endpoint is the front door. Per turn:

```ts
async function callLanaTurn(input: TurnInput): Promise<TurnOutput> {
  const sys = compileSystemPrompt(input.user, input.session, input.overlay_context);
  const messages = [
    { role: 'system', content: sys },
    ...input.history,
    { role: 'user', content: input.userText },
  ];

  const first = await openai.chat.completions.create({
    model: input.expensive ? 'gpt-4o' : 'gpt-4o-mini',
    messages,
    tools: TOOL_REGISTRY,        // from lib/openai/tools.ts (14 tools per LANA_TOOL_ROUTING §4)
    tool_choice: 'auto',
    max_tokens: 800,
  });

  const msg = first.choices[0].message;
  if (!msg.tool_calls) return { text: msg.content!, tool_calls: [] };

  const results = await Promise.all(msg.tool_calls.map(async (call) => {
    const args = JSON.parse(call.function.arguments);
    const result = await dispatchTool(call.function.name, args, input.user.id);
    return { tool_call_id: call.id, content: JSON.stringify(result) };
  }));

  const finalMessages = [...messages, msg, ...results.map(r => ({ role:'tool' as const, ...r }))];
  const final = await openai.chat.completions.create({
    model: input.expensive ? 'gpt-4o' : 'gpt-4o-mini',
    messages: finalMessages,
  });
  return { text: final.choices[0].message.content!, tool_calls: msg.tool_calls };
}
```

The 4 turn outcomes per `LANA_TOOL_ROUTING_v1.md §1`:
- **R · Respond** — conversational reply, no tool calls.
- **A · Ask** — clarifying question, no tool calls.
- **T · Tool-call** — single tool call followed by response with the result.
- **C · Capture** — `capture_inquiry` tool for out-of-scope requests + warm bridge.

Confidence buckets per §3 of that doc: ≥0.85 act, 0.50-0.85 ask clarifying, <0.50 respond conversationally.

### The 14 tools in TOOL_REGISTRY

Per `LANA_TOOL_ROUTING_v1.md §4`:
1. `find_peers` — Discover D1 search.
2. `get_neighbor_profile` — D2.5 drawer load.
3. `explain_match` — D2.5 "what you have in common" synthesis.
4. `publish_activity` — host an activity.
5. `find_exchange_match` — marketplace search.
6. `list_marketplace_item` — list an item.
7. `propose_intro` — D3 joint moment.
8. `send_intro` — D3 → D4 delivery.
9. `snooze_intro` — D3 "not yet."
10. `send_nudge` — first contact.
11. `propose_cohost` — during publish_activity slot-fill (deferred UI v0.2; tool exists for capture).
12. `capture_inquiry` — out-of-scope capture.
13. `update_relationship_tier` — internal, fired by events not by Lana directly (rejected at tool layer unless trigger is whitelisted; per invariant 16 reject any trigger starting with `inquiry_` except `inquiry_mutual_opt_in`).
14. `flag_sensitive` — safety gate, fires first if crisis/medical/DV/child-safety/mental-health detected.

The dispatcher maps each tool name to the corresponding edge function (HTTP loopback or direct DB depending on perf).

### The inline hint pattern (NEW · the v7.0 mechanism)

This is the key new server capability per the v7.0/v7.1 mockup decisions. Lana drops personal tooltips ("only you can see this") inside chat threads, with action chips.

Architecture:
1. **Event-driven trigger.** After `send_message`, after time elapsed since last reply, on marketplace handoff approaching, on mutual-IRL eligibility detected, on a recent suggest-unmask CTA tap from the other side. Each event source enqueues a `draft_lana_hint` call.
2. **`draft_lana_hint(user_id, thread_id, context)`** — gpt-4o-mini call (see §6.31).
3. **INSERT into `lana_inline_hints`.** Schema: `id, user_id, thread_id, text, cta_chips jsonb, context jsonb, expires_at (default created_at + 5min), dismissed_at`.
4. **Push via Realtime channel `lana_hints:{user_id}`.** Payload: `{hint_id, thread_id, text, cta_chips}`.
5. **FE renders** the `.lana-inline-hint` block (per spec — Aki owns the rendering).
6. **Lifecycle:** expires when user dismisses (`dismissed_at=now()`), when an action is taken (any CTA tap), or 5 min idle (cron sweep clears).

CTA chips have well-known actions: `propose_unmask`, `mark_as_acquaintance`, `confirm_completion`, `open_plan_activity`, `dismiss`, `report_message`, etc. The FE knows how to dispatch each.

Cost ceiling: per-user max 10 hints/day to prevent over-presencing (per Agent 6 "don't over-presence" doctrine). Enforced via `rate_limits` with a new bucket `lana_hint_day` (max 10/day).

### Floating modal overlay_context

When the user opens the Lana corner modal from any surface (except Settings), the FE includes an `overlay_context` field on the `/api/lana/conversation` POST:

```ts
type OverlayContext = {
  surface: 'neighbor_drawer' | 'chat_thread' | 'activity_detail' | 'marketplace_item' | 'profile' | 'lana_home' | 'discover_results' | 'plan_activity_conversation';
  focused_entity?: {
    kind: 'user' | 'activity' | 'marketplace_item' | 'inquiry' | 'chat_thread';
    id: string;
  };
  recent_action?: string;  // e.g. 'opened_profile', 'tapped_nudge_cta', 'rsvp_yes'
};
```

The system prompt compiler injects this into the prompt so Lana responds contextually. Example: `surface='neighbor_drawer' + focused_entity={kind:'user', id:'u_maria'} + recent_action='opened_profile'` → Lana opens with "Oh, you're looking at Maria — she's the Brazilian runner. Want me to set up a coffee?" instead of a generic greeting.

### Safety check pattern (see §6.32)

Pre-send guard on every `send_message`. OpenAI moderation (free) + Llama Guard (paid, ~$0.0002/call) + heuristics (phone, off-platform handles, URLs). 800ms timeout, fail open for short text. Hard-block categories (hate, violence) set `send_anyway_allowed=false`.

After 3 holds in 7d → auto-flag for soft-suspend review.

False positive recovery: 1-tap appeal via Slack `#safety-alerts`; if reviewer marks FP, no user-facing change, classifier gets training signal.

### Cost monitoring

Every OpenAI call logs `model`, `prompt_tokens`, `completion_tokens`, `cost_cents` to `openai_usage_log`. Daily aggregate Slack post at 09:00 UTC with cost-per-DAU. Hard cap per user per day ($0.50); soft cap at $0.25 warns; hard cap → Lana switches to canned fallback responses ("Let me think about that and circle back").

Tool-call count tracking per user per day; alert at 80% threshold of any expected ceiling (e.g. >40 tool calls/day signals a runaway loop).

### Latency budgets and fail-open behavior

- gpt-4o synth turns: p95 < 1.8s. Fail-open: timeout → canned response per surface ("Give me a moment — I'll be right back").
- gpt-4o-mini routing: p95 < 600ms. Fail-open: timeout → treat as R (conversational) outcome.
- `draft_replies`: p95 < 800ms (per spec 43). Fail-open: timeout → no chips render that turn.
- `draft_lana_hint`: p95 < 600ms. Fail-open: timeout → no hint drops.
- `safety_check_message`: 800ms total. Fail-open for short text; fail-closed for attachments or long text.

OpenAI 5xx rate > 10% for 10min → trigger fail-open to canned Lana replies app-wide.

---

## §8 · Push notifications (18 triggers per spec 49)

Every push is signed Lana, sheep-icon prefixed, deep-linked to the relevant surface (NOT to an inbox), and belongs to one of 18 triggers. There is no notification inbox screen — Lana inline recap (spec 30) is the in-app alternative for "what's going on."

### The 18 triggers

#### Tier-ladder (5)

| Trigger | Fires when | Deep-link | Default | Quiet hours |
|---|---|---|---|---|
| `nudge_received` | Another mom sent a nudge | `/lana?intent=respond_nudge&nudge_id={id}` | ON | DEFER |
| `nudge_accepted` | Your nudge came back accepted | `/activity/chats/{chat_id}` | ON | DEFER |
| `nudge_expired` | 7d no response (silent · no push · audit row only) | — | — | — |
| `unmask_proposed` | Other party tapped "Share real names" | `/activity/chats/{chat_id}` | ON | DEFER |
| `unmask_accepted` | Both consented to unmask | `/activity/chats/{chat_id}` | ON | DEFER |

#### Chats (5)

| Trigger | Fires when | Default | Quiet hours |
|---|---|---|---|
| `shielded_msg_received` | New shielded msg | ON | DEFER |
| `shielded_msg_preview` | (same trigger; opt-in variant with body preview) | **OFF (privacy default)** | DEFER |
| `direct_msg_received` | New direct msg (preview shown by default since real names exchanged) | ON | DEFER |
| `group_msg_received` | New group msg | ON | DEFER |
| `group_mention` | `@name` mention in group | ON | **IMMEDIATE override** |

#### Marketplace (2)

| Trigger | Fires when | Default | Quiet hours |
|---|---|---|---|
| `inquiry_received` | Someone inquired on your item | ON | DEFER |
| `inquiry_handoff_confirmed` | Other party confirmed handoff, your turn | ON | DEFER |

#### Activity (5)

| Trigger | Fires when | Default | Quiet hours |
|---|---|---|---|
| `activity_invite` | Host invited you to activity (direct, not feed discovery) | ON | DEFER |
| `activity_rsvp_to_my_event` | Someone RSVP'd to your event (includes `rsvp_note` verbatim) | ON | DEFER |
| `rsvp_reminder` | T-1h before activity you RSVP'd to | ON | **IMMEDIATE override** |
| `intro_proposed` | Lana proposed introducing you to another mom | ON | DEFER |
| `intro_received` | Lana intro went through; you can now reach them | ON | DEFER |

#### Moderation (1)

| Trigger | Fires when | Default | Quiet hours |
|---|---|---|---|
| `report_acknowledged` | Your report was reviewed and acknowledged | ON | DEFER |

#### Banned in v0.1

- Morning brief / daily streak / weekly roundup (deferred to v0.2 with streak engine).
- Marketing pushes ("Lana misses you" etc.).
- Any auto-promotion that wasn't user-initiated (auto-IRL goes via in-app inline recap, not push).
- Out-of-scope captures (silent per `LANA_OUT_OF_SCOPE_PLAYBOOK`).

Total: 17 deliverable + 1 silent = 18 active triggers.

### Payload schema

```ts
export interface PushPayload {
  type: NotificationType;
  title: string;        // always "Lana"
  body: string;         // Lana voice · max 95 chars · italic quotes when relayed
  thread_id?: string;   // APNS collapses by this
  deep_link: string;    // canonical app route · always includes ?from=push&push_id={id}
  surface_target: SurfaceTarget;
  badge_count: number;
  priority: 'urgent' | 'normal' | 'background';
  silent: boolean;
  payload: Record<string, any>;  // { activity_id, chat_id, sender_user_id, etc }
}

export interface ApnsEnvelope {
  aps: {
    alert: { title: string; subtitle?: string; body: string };
    sound: string;          // 'lana_chime.caf' normal · 'default' urgent · null silent
    badge: number;
    category: string;       // LANA_NUDGE | LANA_MSG_DIRECT | LANA_MSG_GROUP | LANA_INQUIRY | LANA_ACTIVITY | LANA_REMINDER | LANA_MODERATION
    'thread-id': string;    // see APNS grouping rules
    'mutable-content': 1;   // NSE overrides icon with sheep cream-tile
    'interruption-level': 'active' | 'time-sensitive' | 'passive';
  };
  tagalng: PushPayload;
}
```

### APNS thread-id grouping rules

| Triggers | thread-id |
|---|---|
| `shielded_msg_received`, `shielded_msg_preview`, `direct_msg_received` | `chat:{chat_id}` |
| `group_msg_received`, `group_mention` | `chat:{chat_id}` |
| `inquiry_received`, `inquiry_handoff_confirmed` | `inquiry:{thread_id}` |
| `nudge_received`, `nudge_accepted`, `unmask_proposed`, `unmask_accepted` | `tier:{block_id}` |
| `activity_invite`, `activity_rsvp_to_my_event`, `rsvp_reminder` | `activity:{activity_id}` |
| `intro_proposed`, `intro_received` | `lana:{block_id}` |
| `report_acknowledged` | `moderation:{user_id}` |

### iOS notification categories (with inline reply)

| Category | Triggers | Actions |
|---|---|---|
| `LANA_NUDGE` | `nudge_received` | Accept · Later · Decline |
| `LANA_NUDGE_RESPONSE` | `nudge_accepted`, `unmask_accepted` | Open chat · Later |
| `LANA_UNMASK_REQUEST` | `unmask_proposed` | Yes unmask · Stay shielded · Open |
| `LANA_MSG_DIRECT` | `direct_msg_received` | Reply (iOS text input, gpt-4o-mini drafts default text) · Mark read |
| `LANA_MSG_SHIELDED` | `shielded_msg_*` | Reply (`draft_replies` pre-computed chip prefills input) · Mark read |
| `LANA_MSG_GROUP` | `group_msg_*` | Reply (freeform, no Lana draft) · Mark read · Mute event |
| `LANA_INQUIRY` | `inquiry_*` | Reply (inquiry-style chip prefills) · View inquiry |
| `LANA_ACTIVITY_INVITE` | `activity_invite`, `intro_*` | View · RSVP yes · Maybe · Decline |
| `LANA_REMINDER` | `rsvp_reminder` | On my way (sends preset to group) · Can't make it · Snooze 15m |
| `LANA_RSVP_TO_MY_EVENT` | `activity_rsvp_to_my_event` | View · Reply (thank-you to attendee) · Mute event |
| `LANA_MODERATION` | `report_acknowledged` | Open |

**Pre-compute the suggested reply at push time** (the NSE can't reliably make external HTTP calls). The `suggested_reply` field ships in the payload; iOS displays it as default text in the inline input field.

### Quiet hours

Default: **10pm-7am user-local** (configurable in Settings). Non-urgent pushes queue server-side, deliver consolidated at 7am ("3 things happened on East Park overnight — open up?"). Urgent overrides: `rsvp_reminder`, `group_mention`. Always-immediate opt-in: `nudge_received` (per user agency over interruptibility).

### Deep-link routing

Always include `?from=push&push_id={id}` in the deep_link so the destination surface can (a) render a "from Lana" breadcrumb, (b) mark the push as handled server-side, (c) log dwell + downstream action for the push ROI metric.

Cold-start: payload from `UIApplicationLaunchOptionsRemoteNotificationKey`, route on splash dismiss with 600ms loading state. Warm-start: route immediately. Foreground: if user is already on the deep-link target, just revalidate data; otherwise render top-banner toast with tap-to-route.

### Edge cases (16 cases · per spec 49 §10)

- Device offline → APNS retries 7d.
- APNS service down → server queue with exponential backoff, page on-call after 30min.
- User has no push tokens → in-app banner only.
- Multiple devices → fire to all tokens, iOS dedupes via `apns-collapse-id`.
- Push target invalid (chat archived, item deleted) → destination renders graceful error.
- Two pushes for same logical event → dedupe on `(type, payload.actor_user_id, payload.context_id, hash_60s_window)`.
- Push fires for a chat the user just blocked → server checks block-state at enqueue, drops.
- Push fires during foreground on destination → just refresh data, no banner.
- OS focus mode active → iOS respects, app does nothing differently.

---

## §9 · Implementation sequence (6 weeks)

This is your week-by-week schedule. Adjust as you learn; the dependencies are mostly enforced by the order below.

### Week 1 — Foundation

- Provision Supabase project `lana-app-v01` in Phygtl org.
- Migrations: enums, blocks (seed `east_park`), users (with `handle_new_auth_user` trigger), tier_edges, helper functions (`lana_is_blocked`, `lana_in_thread`, `lana_current_tier`).
- Auth setup: Supabase Auth with email magic link + Apple + Google providers.
- Phone OTP gate: `send_otp`, `verify_otp` via Twilio Verify API.
- Basic `/api/lana/conversation` endpoint scaffold (returns canned response; OpenAI wiring in W3).
- Realtime channels skeleton: `user:{uuid}`, `thread:{uuid}`, `activity:{uuid}`.
- Idempotency_cache table + middleware.

**Deliverable:** a clean Supabase project with auth working, users can sign up via OTP, the tier_edges table is queryable but empty.

### Week 2 — Tier ladder

- Migrations: nudges, unmask_requests, tier_events, lana_inline_hints.
- Edge functions: `send_nudge`, `accept_nudge`, `decline_nudge`, `cancel_nudge`, `expire_nudges` (cron), `propose_unmask`, `accept_unmask`, `decline_unmask`, `expire_unmasks` (cron).
- Server-side state machines for tier + nudge + unmask. Codify the mutual-nudge collision auto-promote logic.
- Push fanout worker skeleton (in-app banner mode; APNS in W5).
- Telemetry: every edge function emits structured `state_transition` logs with `trace_id`.

**Deliverable:** two test users can nudge each other, accept, propose unmask, and reach `direct` tier. Verify via Realtime that both clients observe `tier:promoted` events.

### Week 3 — Chat + messaging + AI

- Migrations: chat_threads, chat_thread_members, messages (+ `messages_visible_for` view), draft_replies_cache, lana_message_holds (with `send_anyway_allowed`), idempotency_cache.
- Edge functions: `send_message` (the canonical pipeline), `delete_message`, `mark_thread_read`, `safety_check_message` (internal), `draft_replies`, `draft_lana_hint`, `override_held_message`.
- **OpenAI integration:** wire `gpt-4o` and `gpt-4o-mini`, function-calling format, the 14-tool registry, the dispatcher pattern from §7. Cost monitoring logs.
- Inline hint dispatcher: event-driven on `message_sent` (others — wire up additional triggers in W4).
- Llama Guard integration via Modal/Replicate.
- Slack webhook `#safety-alerts` for hard-block + 3-holds escalations.

**Deliverable:** users can chat in a shielded thread, drafted reply chips render server-side (verify via Realtime push), inline hints appear post-message (verify on `lana_hints:{user_id}` channel), safety holds work, "Send anyway" override works.

### Week 4 — Marketplace + activity + group

- Migrations: marketplace_items, inquiries (+ sub-tables: handoff_confirmations, completion_confirmations, tier_promotions), activities, activity_rsvps (with `rsvp_note`), intro_proposals, mutual_irl_confirmations, irl_promotion_processed.
- Edge functions: `create_inquiry`, `confirm_handoff`, `confirm_completion` (with the tier-promotion bridge), `close_inquiry`, `expire_inquiries` (with 2-day swap auto-publish cron), `create_activity`, `rsvp_activity`, `cancel_rsvp`, `cancel_activity`.
- Pg triggers: `auto_create_group_chat`, `add_to_group_on_rsvp`, `remove_from_group_on_cancel`.
- Edge function: `attendance_to_irl_check` (cron, every 15min), `confirm_irl_met`.
- Intro flow: `propose_intro`, `accept_intro`, `decline_intro`, `snooze_intro`.
- Additional inline-hint triggers: marketplace handoff approaching, mutual-IRL eligibility detected, post-completion "Stay in touch" prompt.

**Deliverable:** end-to-end marketplace flow (list → inquiry → handoff confirm → completion → opt-in tier promotion). End-to-end activity flow (create → RSVP → auto-group-join → attend → 24h grace → auto-IRL promote).

### Week 5 — Moderation + visitor + polish

- Migrations: moderation_reports, moderation_actions, user_blocks, appeals, visitor_sessions, rate_limits, moderation_evidence schema + message_snapshots.
- Edge functions: `block_user`, `unblock_user`, `report_message`, `submit_appeal`, `process_appeal` (admin), `delete_account_grace_check` (cron), `register_push_token`, `enforce_rate_limit` (internal helper), `start_visitor_session`, `upgrade_visitor_to_user`.
- Auto-escalation logic in `report_message` (3 reports / 5 reports / safety-category immediate).
- APNS integration: real push delivery via APNS HTTP/2; web push via VAPID.
- iOS notification categories registration on app launch.
- Inline-reply pre-computation: when `send_message` triggers a chat push, pre-call `draft_replies` and ship the top suggestion in `payload.suggested_reply` so the iOS NSE doesn't need to.
- Slack webhooks: `#moderation-appeals`, `#openai-cost` (daily), `#signal-aggregator` (weekly).

**Deliverable:** moderation works end-to-end (report → auto-escalation → suspension → appeal). Push notifications deliver on real devices. Visitor sessions persist across F0/F1.

### Week 6 — E2E + load testing + DR

- Race-condition test harness: 10 known races, each with a reproducible test. Run repeatedly under load.
- Load test: 1000 concurrent users, 100 messages/sec, 50 nudges/sec, 25 unmasks/sec. Target p95 send_message < 400ms.
- Moderation evidence retention validation: confirm 90-day cron sweep works.
- GDPR delete flow end-to-end: request → 14d grace → anonymization → hard delete.
- Backup and DR runbook: dry-run a PITR restore into staging, validate row counts vs production.
- Observability dashboard: edge function duration p99, error rate, push delivery rate, OpenAI cost, rate-limit rejections, safety holds.
- Alert wiring: p99 send_message > 1s for 5min → page; error rate > 5% on any function for 5min → page; push delivery error rate > 20% for 15min → page.

**Deliverable:** load-tested production-ready backend. DR runbook in `RUNBOOKS/restore.md`. All 10 race conditions verified resolved.

---

## §10 · Edge cases catalog (cross-cutting · indexed by surface)

Every "what if" surfaces here. Format: scenario · trigger · backend behavior · frontend behavior · privacy invariant preserved.

### A · Block (per spec 51 §A)

| Scenario | Backend | FE | Invariant |
|---|---|---|---|
| Block during active group chat | Blocker sees blockee msgs as "(message hidden by you)". Other members unchanged. | Render hidden placeholder | #2 block precedence |
| Block host of activity user is attending | RSVP cancels silently. Bring-list contribs anonymize to "a neighbor". | Activity card disappears | #2 |
| Host blocks attendee | Attendee removed silently. Polite Lana note. | "(removed)" not "removed by host" | #2 |
| Block recently-IRL-peer | `blocked_unblockable=true`. Unblock requires support. | Standard block UX | #2 |
| Block during open marketplace inquiry | Inquiry auto-closes. Both told "this conversation ended". | Read-only banner | #2 |
| Block self | Server returns 400. Menu item hidden client-side. | — | — |
| Block in 2-person event group | Group auto-archived. Remaining party gets "{nickname} left the group." | Auto-archive UX | #2 |

### B · Report (per spec 51 §B)

| Scenario | Backend | FE |
|---|---|---|
| 3+ reports same target 7d different reporters | Auto soft_suspend pending review | Suspended user sees banner |
| Reports from blocked users | Still count for pattern (history was real) | — |
| Self-harm concern report | Routes to on-call moderator within 1h SLA | — |
| Reporter blocks target after submitting | Independent flows | — |
| Reported message already deleted-for-everyone | Server retains (90d evidence) | Reviewer sees original |
| Same person 3x in 24h different categories | All stored, flagged `multi_category_burst` | — |
| False-report farming (>10/7d <10% upheld) | Reporter enters `report_cooldown` | Ack copy changes to "reviewing carefully" |

### C · Suspend (per spec 51 §C)

| Scenario | Backend |
|---|---|
| Suspend during active marketplace inquiry | Inquiry frozen; counterparty told "the buyer can't respond right now" |
| Suspend during pending unmask | Auto-cancelled; other told "request expired" |
| Suspend during hosting | Activity PAUSED not cancelled; attendees told "Lana paused this one"; auto-cancel after 7d |
| Suspend during open group chat | Suspended user appears greyed ("paused") in member list; prior msgs visible; input disabled |
| Soft-suspend expires mid-session | Send re-enables silently on 30s poll |

### D · Appeal (per spec 51 §D)

| Scenario | Backend |
|---|---|
| Appeal during 24h grace; suspend expires before review | Still reviewed as record-correction |
| Appeal then immediately request account deletion | Deletion supersedes; appeal closed `withdrawn_user_deleted` |
| 6+ appeals in 30d | Flagged for review (potential abuse pattern) |
| Appeal references another user by name | Reviewer redacts before internal forwarding |

### E · Message delete (per spec 51 §E)

| Scenario | Backend |
|---|---|
| Delete-for-everyone while recipient is reading | Bubble cross-fades to placeholder 320ms |
| Delete-for-everyone while still in offline send queue | Removed from queue silently |
| Delete a message recipient already inserted as chip text | Recipient's inserted text stays; only original bubble vanishes |
| Voice-transcript message | Same flow (transcript IS what's stored) |
| Group sender deletes own quoted-by-3-others message | Quotes flatten to placeholder; replies stay attributed |

### F · Conversation delete/leave (per spec 51 §F)

| Scenario | Backend |
|---|---|
| Leave shielded | Tier downgrades to stranger; 30d re-nudge cooldown |
| Leave direct | Tier stays direct; either side can DM again from drawer |
| Leave group | RSVP removed; system message "{first name} left"; host not individually notified |
| Host leaves their own group | Group DESTROYED; all members told "this group ended"; activity auto-cancelled |
| Last member leaves | Group auto-archives |
| Leave during pending unmask | Request auto-declines |

### G · Tier demotion

**No demotion paths in v0.1 except via block.** Spec is explicit. If user asks Lana "can I take a step back?", Lana points to Settings > Privacy.

### H · Evidence retention

| Data class | Retention |
|---|---|
| Deleted messages (for_everyone) | 90d in `moderation_evidence.message_snapshots`, reviewers only |
| Deleted messages (for_me) | Indefinite, sender + reviewers |
| Block events | 5y for pattern/regulatory |
| Reports | 7y |
| Suspended account data | 30d post-suspend |
| Lana auto-mod holds | 90d |

### I · GDPR delete (per spec 51 §I)

14-day grace, cancelable. After grace: anonymize PII (display_name='Deleted user'), hard-delete auth.users (cascades), tombstone messages (sender NULL, text retained as co-authored).

| Scenario | Backend |
|---|---|
| Pending-deletion user receives nudge | Delivered normally; if accepted, post-delete shows "former member" |
| Active marketplace inquiry mid-grace | Other side NOT told (avoid harvesting); inquiry runs until grace ends |
| Pending unmask mid-grace | Stays open; if counterparty accepts pre-grace-end, unmask happens |
| Cancel deletion at day 14 hour 23 | Honored if before nightly 02:00 UTC worker |
| Re-registration same email post-delete | Rejected generically "not available for sign-up" |

### J · Rate limiting

Returns `429 rate_limited` with `retry_after_seconds`. Client renders inline chip (not modal), warm voice copy ("Let's slow down").

### K · Unmask decline

Privacy invariant: requester sees "request expired" (same as time-out). 48h cooldown. 3+ declines in 30d → cooldown extends to 30 days.

### L · Nudge expiry

Sender sees "no reply" (silent expiry framing). 30d re-nudge cooldown. 60d cooldown if recipient explicitly declined. Infinite if blocked.

### M · Lana auto-moderation

Held messages render intercepted-style for sender. Hard-block categories (hate, violence) reject "Send anyway". After 3 holds 7d → flag for soft-suspend candidacy.

### v7.0/v7.1 additions

| Scenario | Backend |
|---|---|
| Selective unmask race in group | `propose_unmask` accepts `context='group_chat:{thread_id}'`; identical state machine to 1:1; promotion affects only the pair (other group members unchanged) |
| 2-day swap auto-publish (M6) | Cron at `proposed_at + 48h`: if no seller response, flip swap-offer item back to active on proposer's shelf, close inquiry with `reason='expired_no_seller_response'` |
| "Stay in touch" post-completion (M7) | Mutual opt-in via `inquiry_tier_promotions`; promotes to `acquaintance` and INSERTs new shielded `chat_threads` row |
| Lana absent on Settings | No corner icon, no overlay route; backend has no special check (FE doesn't mount), but documented |
| Inline hint dispatcher | Event-driven; gpt-4o-mini drafted tooltip + CTA chips; "only you can see this"; max 10/day per user |

### The 10 race conditions (cross-referenced from §5)

Catalog reference; resolutions are documented in §5 and the relevant edge function in §6.

---

## §11 · API contracts (TypeScript types · the shared frontend-backend contract)

The frontend imports these types. This section is your authoritative contract. Where any shape conflicts with what spec 52-54 says, use this section.

### Common types

```ts
export type Tier = 'stranger' | 'nudge_pending' | 'acquaintance' | 'direct' | 'irl_peer';

export type EdgeResponse<T> =
  | { ok: true; data: T; idempotency_hit?: boolean; trace_id: string }
  | { ok: false; error: EdgeError; trace_id: string };

export interface EdgeError {
  code: 'UNAUTHENTICATED' | 'FORBIDDEN' | 'NOT_FOUND' | 'CONFLICT'
      | 'RATE_LIMIT' | 'BLOCKED' | 'SUSPENDED' | 'CROSS_BLOCK'
      | 'BAD_REQUEST' | 'INTERNAL' | 'SAFETY_HOLD' | 'ALREADY_EXISTS'
      | 'VALIDATION' | 'TIMEOUT' | 'HARD_BLOCKED';
  message: string;
  field?: string;
  retry_after?: number;
}
```

### Tier domain

```ts
// POST /functions/v1/send_nudge
export interface SendNudgeRequest { recipient_id: string; opener_text?: string; }
export interface SendNudgeResponse { nudge_id: string; expires_at: string; }
// Errors: BLOCKED · SUSPENDED · RATE_LIMIT · CONFLICT · CROSS_BLOCK · VALIDATION

// POST /functions/v1/accept_nudge
export interface AcceptNudgeRequest { nudge_id: string; }
export interface AcceptNudgeResponse { chat_id: string; tier: 'acquaintance'; }

// POST /functions/v1/decline_nudge · cancel_nudge
export interface NudgeActionRequest { nudge_id: string; }
export interface NudgeActionResponse { ok: true; }

// GET /functions/v1/get_tier?other_id=...
export interface GetTierResponse {
  tier: Tier;
  promoted_at?: string;
  irl_peer_since?: string;
  proof_artifact_id?: string;
  nudge_id?: string;
  expires_at?: string;
}

// POST /functions/v1/propose_unmask
export interface ProposeUnmaskRequest {
  other_user_id: string;
  context?: 'dm' | `group_chat:${string}`;  // NEW v7.0: support group-initiated
}
export interface ProposeUnmaskResponse { request_id: string; expires_at: string; }

// POST /functions/v1/accept_unmask · decline_unmask
export interface UnmaskActionRequest { request_id: string; }
export interface AcceptUnmaskResponse { tier: 'direct'; promoted_at: string; }
```

### Chat domain

```ts
// POST /functions/v1/send_message
export interface SendMessageRequest {
  thread_id: string;
  text?: string;
  attachments?: Array<{ storage_path: string; kind: 'image' | 'file'; blurhash?: string }>;
  reply_to?: string;
  client_dedupe_key: string;  // client-generated UUID for idempotency
}
export type SendMessageResponse =
  | { status: 'sent'; message_id: string; sent_at: string }
  | { status: 'held'; hold_id: string; reason_category: string; send_anyway_allowed: boolean };

// POST /functions/v1/delete_message
export interface DeleteMessageRequest {
  message_id: string;
  kind: 'for_me' | 'for_everyone';
}

// POST /functions/v1/mark_thread_read
export interface MarkThreadReadRequest { thread_id: string; up_to_message_id: string; }
export interface MarkThreadReadResponse { read_count: number; }

// POST /functions/v1/override_held_message
export interface OverrideHeldRequest { provisional_id: string; }
export type OverrideHeldResponse =
  | { status: 'sent'; message_id: string }
  | { status: 'hard_blocked' };
```

### Inquiry domain

```ts
// POST /functions/v1/create_inquiry
export interface CreateInquiryRequest { item_id: string; opening_text: string; }
export interface CreateInquiryResponse { inquiry_id: string; chat_id: string; }

// POST /functions/v1/confirm_handoff
export interface ConfirmHandoffRequest {
  inquiry_id: string;
  when: string;       // ISO8601
  where: string;
  lat?: number;
  lng?: number;
}
export interface ConfirmHandoffResponse {
  status: 'one_side' | 'committed';
  agreed_at?: string;
}

// POST /functions/v1/confirm_completion
export interface ConfirmCompletionRequest { inquiry_id: string; }
export interface ConfirmCompletionResponse { status: 'one_side' | 'completed'; }

// POST /functions/v1/mark_acquaintance_from_inquiry (the "Stay in touch" opt-in)
export interface MarkAcquaintanceFromInquiryRequest { inquiry_id: string; consent: boolean; }
export interface MarkAcquaintanceFromInquiryResponse {
  status: 'recorded_waiting_other' | 'both_consented_promoted' | 'skipped';
  new_chat_id?: string;  // populated on both_consented_promoted
}

// POST /functions/v1/close_inquiry
export interface CloseInquiryRequest { inquiry_id: string; reason?: string; }
```

### Activity domain

```ts
// POST /functions/v1/create_activity
export interface CreateActivityRequest {
  title: string;
  description?: string;
  starts_at: string;
  ends_at: string;
  location_label: string;
  location_lat?: number;
  location_lng?: number;
  capacity?: number;
  audience: 'block' | 'direct_only' | 'irl_only' | 'custom';
  visibility: 'public' | 'private';
}
export interface CreateActivityResponse { activity_id: string; chat_id: string; }

// POST /functions/v1/rsvp_activity
export interface RsvpActivityRequest {
  activity_id: string;
  status: 'going' | 'waitlist' | 'cancelled';
  rsvp_note?: string;  // max 280 chars · displayed in host view + push
}

// POST /functions/v1/confirm_irl_met (manual IRL promotion)
export interface ConfirmIrlMetRequest { other_user_id: string; }
export interface ConfirmIrlMetResponse { status: 'one_side' | 'promoted'; tier?: 'irl_peer'; }
```

### Moderation domain

```ts
// POST /functions/v1/block_user
export interface BlockUserRequest {
  blocked_user_id: string;
  reason?: 'harassment' | 'threat' | 'sexual' | 'self_harm' | 'csam' | 'discomfort' | 'spam' | 'other';
}

// POST /functions/v1/report_message
export interface ReportMessageRequest {
  message_id?: string;
  thread_id?: string;
  target_user_id?: string;
  category: 'harassment' | 'spam' | 'sexual' | 'self_harm' | 'threat' | 'off_platform_ask' | 'csam' | 'other';
  description?: string;
}
export interface ReportMessageResponse { report_id: string; }

// POST /functions/v1/submit_appeal
export interface SubmitAppealRequest {
  moderation_action_id: string;
  text: string;
  attachments?: string[];  // storage paths
}
export interface SubmitAppealResponse { appeal_id: string; sla_until: string; }
```

### Lana conversation domain

```ts
// POST /api/lana/conversation (the OpenAI dispatcher front door)
export interface ConversationRequest {
  user_text?: string;          // omitted when triggered by a tap or system event
  audio_blob?: string;          // base64 if voice (transcribed server-side via Web Speech equivalent)
  overlay_context?: {
    surface: 'neighbor_drawer' | 'chat_thread' | 'activity_detail' | 'marketplace_item' | 'profile' | 'lana_home' | 'discover_results' | 'plan_activity_conversation';
    focused_entity?: { kind: 'user' | 'activity' | 'marketplace_item' | 'inquiry' | 'chat_thread'; id: string };
    recent_action?: string;
  };
  session_id: string;
}
export interface ConversationResponse {
  outcome: 'R' | 'A' | 'T' | 'C';
  text: string;                  // Lana's reply (may be empty for pure tool-call turns)
  tool_calls?: Array<{ name: string; args: any; result?: any }>;
  captures?: Array<{ category: string; raw: string; sentiment: string }>;
}

// Realtime channel `lana_hints:{user_id}` payload (inline hint pattern)
export interface InlineHintPushed {
  hint_id: string;
  thread_id: string;
  text: string;
  cta_chips: Array<{ label: string; action: string; payload?: any }>;
  expires_at: string;
}

// POST /functions/v1/dismiss_lana_hint
export interface DismissHintRequest { hint_id: string; }
```

### Realtime channels

| Channel | Subscribers | Events |
|---|---|---|
| `user:{user_id}` | The user, any device | tier promotions, nudges, unmasks, intros, moderation, push events |
| `thread:{thread_id}` | Thread members | message:sent/delivered/read/deleted, typing (v0.2) |
| `activity:{activity_id}` | RSVPs | rsvp updates, cancellations, reminders |
| `block:{block_id}:presence` | Block members | online/offline (Realtime presence), new activity created |
| `lana_hints:{user_id}` | The user, any device | inline hint pushes |

---

## §12 · Open questions for the founder

These are decisions you should not resolve unilaterally. Most are from spec 55 §8 founder-review list. Suggested defaults in italics; founder confirms or overrides.

### P0 — need answers before sprint 1 ships

1. **Inquiry tier-promotion ceiling.** RESOLVED to `acquaintance` per wireframe + invariant 7. Confirm.
2. **Decline-vs-expire visibility on unmask.** Currently spec surfaces unmask decline neutrally to initiator ("She wants more time") while nudge decline is fully silent. *Suggested: keep current — the 2-week-deep relationship warrants the small disclosure, asymmetric with nudge is okay.*
3. **E6 swap auto-promote without Apple Pay.** Currently YES (auto-promote on confirm, no payment). *Confirm.*
4. **2-day swap-response auto-publish (M6 mockup decision).** New per v7.0. Confirm: if seller doesn't respond to swap inquiry within 48h, auto-flip proposer's swap-offer item back to active on their shelf AND close the inquiry. Alternative: hold longer.

### P1 — need answers before sprint 2 starts

5. **IRL 24h grace: before or after promotion flip?** *Suggested: before-promote — Lana asks "still feels right?" at hour 24 and locks if no objection.*
6. **J3 vouch+invite tier inheritance for new arrival.** *Suggested: (a) Stranger with affinity bump — explicit user action always required to elevate.*
7. **Visitor session resume: F0 each session vs resume at F2.** *Suggested: F0 each session; persistence post-OTP only.*
8. **Lana corner badge unread count: combined or split.** *Suggested: split (one badge on Activity corner for peer chats, one on Marketplace corner for inquiries).*
9. **Block microcopy** (what blockee sees). *Suggested: "(unavailable)" name + greyed avatar + "Sara has paused contact with you."*
10. **Direct → Acq downgrade copy** (Lana confirms once to downgrader, counterpart silent). *Suggested: yes ("Got it, I'll shield this chat going forward").*
11. **Auto-cancel on cross-block move during pending unmask.** *Suggested: yes, cancel.*
12. **24h cooldown on system-cancel** (block / delete / cross-block). *Suggested: yes, 24h soft cooldown to discourage immediate re-try.*
13. **"Ask to unmask" inside thread B7 vs only in drawer B4.** *Suggested: keep both surfaces; user is mid-conversation when she realizes she's ready.*
14. **Should 2nd co-attended event promote acquaintance → irl_peer directly, skipping direct?** *Suggested: no — must unmask first to keep ladder linear and consent path clear.*
15. **Manual mutual-IRL initiator notification on decline.** *Suggested: silent (matches nudge pattern).*
16. **Spec 51 §A 2-person-group auto-archive copy.** Verify wording with brand voice.
17. **Inline-hint frequency cap.** *Suggested: 10/day per user.* Confirm or adjust.
18. **Inline-hint trigger inventory.** Initial set: post-message reminder, marketplace handoff approaching, mutual-IRL eligibility, "Stay in touch" prompt after completion. *Confirm or extend.*

### P2 — answer before public launch

19. **`rsvp_note` payload.** Add `rsvp_note text` to activity_rsvps (max 280 chars, optional). Confirm.
20. **Visitor seeing seller real name on shared marketplace link.** *Suggested: NO (masked as "A neighbor in East Park").*
21. **Visitor session schema: rename f0/f1_transcript to single `messages` jsonb + add `referred_by_user_id` + `utm`.** *Suggested: yes, cleaner.*
22. **IRL-peer-block-unblockable operational cost.** Estimate volume; 2-person mod team capacity.
23. **3-reports/7d auto-suspend threshold.** Acceptable for 250-person cohort? May need to drop to 2.
24. **72h appeal SLA capacity.** Same 2-person mod team question.
25. **Host-deletion auto-reassign co-host UX.** Currently v0.1 auto-with-note. Acceptable, or v0.2 explicit confirm?
26. **3 nudges/day rate limit.** Generous for Helena (~1/day); too tight for power users? v0.2 needs per-user overrides.
27. **Send-anyway always available except hard-block legal review.** Confirm.

---

## §13 · Out of scope for v0.1

Do NOT build any of these in v0.1, even if asked. They live in `BACKLOG.md`.

- **Selling on marketplace.** Apple Pay, Stripe, paid sell flow. v0.1 marketplace is free + swap only. Backend enforces: reject `intent_type='sell' AND price > 0` in `create_inquiry`. v0.2 ships Apple Pay + Stripe webhook.
- **TTS / Lana voice output.** Lana replies in text only in v0.1. Text-to-speech is v0.2 (separate spec).
- **Streak engine.** P3 streak hook UI exists in spec 36 but the engine is OFF. No streak counter, no morning brief, no daily push.
- **Co-host (CH0-CH2).** Conversation flows capture as `inquiry_signal`; full UI ships v0.2. Per spec 51 §I, GDPR-delete-host fallback is an exception (auto-reassign for safety, not the full co-host feature).
- **Vouching action.** IRL peer tier is visible (★ pill); the vouch button is disabled with v0.2 tooltip. The `submit_vouch` tool exists in MODUS but is not wired in v0.1.
- **Auto-promotion via marketplace.** Per invariant 16. Backend must reject `update_relationship_tier(trigger='inquiry_*')` unless trigger is `inquiry_mutual_opt_in`.
- **Group-level Lana suggestions.** Lana speaks for individuals only in v0.1 (per inline hint pattern, hints are 1:1). No "Hey group, here are 3 ideas" type messages.
- **Lana on Settings.** No corner icon, no overlay route. FE handles; backend has no special path.
- **Multi-block (cross-block discovery).** Block fixed to `east_park` in v0.1. `find_peers` returns `expansion_suggested: 'neighboring_blocks'` but the action is disabled.
- **Morning brief push / daily streak push / weekly roundup.** Deferred with streak engine.
- **Marketing pushes.** Banned. No "Lana misses you" type notifications ever.
- **Notification inbox screen.** No inbox UI; Lana inline recap (spec 30) is the in-app alternative.
- **Android push (FCM).** iOS only in v0.1. The platform enum supports it for forward compat; FCM provider wiring ships v0.2.
- **Voice messages in chat.** Mic button transcribes via Web Speech API → text. No audio bubbles in v0.1.
- **Read receipts UI.** Server emits `chat:read` events; FE hides per spec 45 §6. UI ships v0.2.
- **Explicit "step back" tier demotion UX** (acquaintance → stranger without going through block). Not exposed in v0.1.
- **Admin / moderator console UI.** v0.1 ops via Slack `#moderation-appeals` + direct DB read.
- **Streaming OpenAI responses.** v0.1 uses non-streaming. Streaming v0.2.
- **Visitor session multi-device continuity.** Each device has its own `session_id` in v0.1.
- **Auto-co-host invite during publish_activity flow.** `propose_cohost` tool exists for capture (so the data is collected) but no UI surfaces it. v0.2.

For anything not on this list, default to "ship it per spec." When in doubt, ask in the daily standup; do not silently expand scope.

---

## Closing notes

- **The 16 invariants in §3 are non-negotiable.** If implementation seems to require breaking one, escalate before writing code.
- **Idempotency is your friend.** Every mutating function takes (or computes) a key. Re-execution is a no-op that returns the original response.
- **Block always wins.** If you're unsure whether a particular code path should respect block state, the answer is yes.
- **Coordinate FE-side handling for the 10 race conditions in §5** at the kickoff of sprint 3 (week 3 of the schedule). Aki on FE should know which races have client-side reconciliation TODOs.
- **Slack channels for ops:** `#moderation-appeals`, `#safety-alerts`, `#openai-cost` (daily summary), `#signal-aggregator` (weekly capture digest). Wire them in week 5 alongside APNS.
- **Backup runbook in `RUNBOOKS/restore.md`** with double-approval (you + me) per the global CLAUDE.md "never cancel anything without double approval" rule. The 14-day grace on account deletion IS the double approval mechanism for that flow.
- **OpenAI cost ceiling: $0.50/user/day hard, $0.25 soft.** Hit the hard cap → Lana switches to canned fallback responses. Daily Slack post at 09:00 UTC.
- **You are the schema gatekeeper.** Migrations only, no dashboard edits. Run `supabase db diff --linked` in CI as a guardrail.

If anything in this doc contradicts itself or contradicts a spec, flag it in our standup. The frontend specs (52-54) are the deeper canonical reference for the schema/state-machine/edge-function detail; this doc is the action briefing built on top of them.

You can start Week 1 today.

—

*End of ASJID_BACKEND_ATPR.md · v1.0 · 2026-06-08 · briefed for Asjid by Tommaso · ~14,500 words.*
