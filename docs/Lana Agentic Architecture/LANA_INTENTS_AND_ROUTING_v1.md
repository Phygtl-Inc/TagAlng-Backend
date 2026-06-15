# Lana · Intents & Backend Routing · v1

*Compiled 2026-06-12 · for Asjid (backend) · Aki (AI · when back) · Abdullah (frontend review) · Tommaso (founder review)*

> **Purpose:** the single source of truth for every user intent Lana can hear · what we fulfill directly (linear) · what we capture for the roadmap (fall-back). This expands the 12-phase routing the backend already shipped (per `LANA_UNIFIED_DISCOVERY_FRONTEND.md`) into the full catalog Lana needs to handle at v0.2 launch.
>
> **Grounded in:** `LANA_MODUS_OPERANDI.md` (per-module doctrine) · `LANA_TOOL_ROUTING_v1.md` (R/A/T/C decision logic) · `LANA_OUT_OF_SCOPE_PLAYBOOK.md` (capture pattern) · `LANA_SIGNAL_AGGREGATOR.md` (dashboard pipeline) · `LANA_UNIFIED_DISCOVERY_FRONTEND.md` (shipped backend contract) · `lana-v01-walkthrough.html v0.2.7` (current frontend state).
>
> **Status:** v1 first pass · founder + eng team to review/edit before lock.

---

## §1 · TL;DR

Lana hears two classes of utterance:

1. **Linear intents** — things v0.2 actively fulfills. These route to a capability tool (`T`), ask for a missing slot (`A`), or respond conversationally (`R`). **23 distinct linear intents** across 7 capability surfaces.
2. **Fall-back intents** — requests v0.2 can't fulfill but captures via the `capture_inquiry` tool. Each capture lands in `out_of_scope_inquiry` table · gets clustered + aggregated by the Signal Aggregator into a roadmap dashboard. **14 named fall-back categories** (expandable as production data arrives).

The 12-phase `routing_phase` in the shipped backend is a sub-set focused on the **discovery + auth** sub-flows. This doc expands to the full v0.2 surface area.

**Operating sentence:** every fall-back is a roadmap data point · never a failure · never a flat refusal.

---

## §2 · Linear intents (23 · in-scope · fulfilled by v0.2)

Each row: intent ID · trigger phrasing · `active_intent` value · current `routing_phase` if shipped · what Lana does · what tool fires · what DB tables are touched · v0.2 status.

### §2.1 · DISCOVERY · find people / blocks

| Intent ID | Trigger phrasing examples | `active_intent` | `routing_phase` | Lana action | Tool | Tables | v0.2 status |
|---|---|---|---|---|---|---|---|
| `discovery.find_peers` | "find people like me on the block" · "show me nearby moms" · "who's around me" | `discovery.find_peers` | `listening` → `need_zip` → `need_identity` → `need_display_name` → `preview` → `gate_verify` → `await_signup_*` → preview-full | ZIP → identity → name → redacted preview → verify gate → full matches | `get_blocks_near_zip` · `find_peer_matches` | `users` · `user_identity_claims` · `blocks` · `peer_matches` (computed) | **SHIPPED** (per GitHub doc) |
| `discovery.find_by_attrs` | "find a Brazilian mom with toddlers" · "moms near me who speak Portuguese" | `discovery.find_by_attrs` | `need_identity` → `preview` (filtered) | Bell-affinity selector · multi-attribute match · ranked preview | `find_peer_matches({filters})` | same as above + filter on `user_identity_claims.dimension` | **MOCKED** in walkthrough · backend extension of `find_peers` |
| `discovery.find_in_block` | "what's happening in my block" · "who's new on my block" | `discovery.block_summary` | `listening` → `preview` (block-scoped) | Summarize block state · count moms · recent activity · sparked/quiet status | `get_block_summary` | `blocks` · `user_block_membership` · `local_signals` | **TO BUILD** for v0.2.x |

### §2.2 · IDENTITY · self-describe + profile

| Intent ID | Trigger phrasing | `active_intent` | `routing_phase` | Lana action | Tool | Tables | v0.2 status |
|---|---|---|---|---|---|---|---|
| `identity.add_claim` | "I'm a Latino mom, two toddlers, runner" · "we're Brazilian, Christian" · "Oh actually I'm also a teacher" | `identity.add_claim` | `need_identity` or in-flow | Background extract → `user_identity_claims` upsert · sync confirmation | `extract_identity_claims` | `user_identity_claims` (7-dim model: stage/activity/interest/lifestyle/language/heritage/faith) | **SHIPPED** (per `LANA_MODUS_OPERANDI §1`) |
| `identity.edit_claim` | "edit my identity" · "I'm not Brazilian, I'm Mexican" · "tap a chip" | `identity.edit_claim` | `confirm_profile` (re-prompt one dimension) | Confirm what to change · upsert · resync 7-token sentence | `extract_identity_claims({existing_claims})` | same | **PARTIAL** (re-prompt flow needs frontend wiring) |
| `identity.complete_profile` | "that's me" · "yes, exactly" · accepting MAPPED YOU CTA | `identity.complete_profile` | `confirm_profile` → `listening` | Final extract · embeddings · mark `ready_to_complete` | `POST /lana/sessions/{id}/complete` | `users.identity_summary` · `user_identity_claims.is_canonical` | **SHIPPED** |
| `identity.show_my_profile` | "show me what you have" · "what do you know about me" · tap profile rail | `identity.show_my_profile` | `confirm_profile` (read-only) | Render MAPPED YOU layout · 7-token sentence · identity threads | `get_user_profile` | same | **MOCKED** (C-12 in walkthrough) |

### §2.3 · LOOKING lane · what mom wants from the block

| Intent ID | Trigger phrasing | `active_intent` | `routing_phase` (proposed) | Lana action | Tool | Tables | v0.2 status |
|---|---|---|---|---|---|---|---|
| `looking.swap` | "I'm looking for 3T rain boots" · "anyone have a size 5 dress?" · "borrow or swap" → mom wants an item | `looking.swap` | `listening` → `extract` → `confirm_missing` → `listening_state` | Voice capture → semantic extract → confirm missing piece → save as `local_signal` (intent=swap_seek) | `extract_looking_intent` · `save_local_signal` | `local_signals` (intent=`swap_seek`) · `swap_intents` | **MOCKED** (C-4-look-swap-P1..P4 in walkthrough) · backend not built |
| `looking.meet` | "looking for a playgroup" · "stroller walk buddies" · "moms with toddlers Saturday morning" | `looking.meet` | `listening` → `extract` → `confirm_missing` → `listening_state` | Same 4-phase pattern → save as `local_signal` (intent=meet_seek) | `extract_looking_intent` · `save_local_signal` | `local_signals` (intent=`meet_seek`) · `meet_intents` | **MOCKED** |
| `looking.tip` | "looking for a mom-tested pediatrician" · "anyone know a good dentist?" · "rec for music class" | `looking.tip` | same | Same → save as `local_signal` (intent=tip_seek) | same · save_local_signal | `local_signals` (intent=`tip_seek`) | **MOCKED** |

### §2.4 · SHARING lane · what mom offers the block

| Intent ID | Trigger phrasing | `active_intent` | `routing_phase` (proposed) | Lana action | Tool | Tables | v0.2 status |
|---|---|---|---|---|---|---|---|
| `sharing.swap` | "I have 3T rain boots my kid outgrew" · "stroller free to a good home" | `sharing.swap` | `listening` → `extract` → `confirm_missing` → `published` | Same 4-phase → save as `local_signal` (intent=swap_offer) · share-onward CTA | `extract_sharing_intent` · `save_local_signal` · `draft_warm_link` | `local_signals` (intent=`swap_offer`) | **MOCKED** |
| `sharing.host` | "I want to host a Brazilian moms coffee" · "Saturday playgroup at my house" | `sharing.host` | same | Same → save as `local_signal` (intent=host_meet) · share-onward CTA | `publish_activity` · `save_local_signal` | `local_signals` (intent=`host_meet`) · `meets` (deferred) | **MOCKED** |
| `sharing.tip` | "Dr. Sarah is the best pediatric dentist" · "I love the trail at Moss Park" | `sharing.tip` | same | Same → save as `local_signal` (intent=tip_share) | `extract_sharing_intent` · `save_local_signal` | `local_signals` (intent=`tip_share`) | **MOCKED** |

### §2.5 · RELATIONSHIP TIER · nudge + ladder progression

| Intent ID | Trigger phrasing | `active_intent` | `routing_phase` (proposed) | Lana action | Tool | Tables | v0.2 status |
|---|---|---|---|---|---|---|---|
| `tier.send_nudge` | "introduce me to Maria" · "send Maria a note" · tap Nudge button on fellow card | `tier.send_nudge` | `listening` → `compose_nudge` → `nudge_sent` (notification triggered) | Draft nudge in Lana's voice · confirm before send · log relationship as `nudge_pending` (7-day quiet expire) | `propose_intro` · `draft_nudge` · `send_nudge` | `relationship_tier_state` (status=`nudge_pending`) · `nudge_messages` | **MOCKED** (C-13 Fellows tab) · backend partial |
| `tier.respond_nudge` | recipient's response: "yes — introduce us!" · "not now" · "block" | `tier.respond_nudge` | external trigger → `tier_advance` or `nudge_declined` or `nudge_blocked` | Update relationship state · trigger signup if guest · advance ladder | `respond_nudge` · `check_consent` | same | **TO BUILD** |
| `tier.advance_acquaintance` | implicit on first warm exchange | `tier.advance_acquaintance` | background | Bump ladder Nudge → Acquaintance | `advance_tier` | `relationship_tier_state` | **TO BUILD** |
| `tier.advance_direct` | implicit on first meet attendance | `tier.advance_direct` | background | Bump ladder Acquaintance → Direct | `advance_tier` | same | **TO BUILD** |
| `tier.advance_irl` | implicit on sustained meets (≥3) | `tier.advance_irl` | background | Bump ladder Direct → IRL | `advance_tier` | same | **TO BUILD** |

### §2.6 · AUTH · signup + login + logout + photo

| Intent ID | Trigger phrasing | `active_intent` | `routing_phase` | Lana action | Auth call | v0.2 status |
|---|---|---|---|---|---|---|
| `auth.signup_phone` | gives phone after preview | `auth.signup_phone` | `await_signup_phone` (`ui_intent: collect_phone`) | Send `auth_action: link_phone_signup` | `PUT /auth/v1/user` | **SHIPPED** |
| `auth.signup_otp` | gives OTP after phone | `auth.signup_otp` | `await_signup_otp` (`ui_intent: collect_otp`) | Send `auth_action: verify_signup_otp` (`phone_change`) | `POST /auth/v1/verify` | **SHIPPED** |
| `auth.login_phone` | "log in" · "I already have an account" · "sign in" | `auth.login_phone` | `await_login_phone` (`ui_intent: collect_phone`) | Send `auth_action: send_login_otp` | `POST /auth/v1/otp` | **SHIPPED** (per GitHub doc · new sign-in flow in walkthrough v0.2.6 mirrors this) |
| `auth.login_otp` | OTP for login | `auth.login_otp` | `await_login_otp` (`ui_intent: collect_otp`) | Send `auth_action: verify_login_otp` (`sms`) · start new session | `POST /auth/v1/verify` | **SHIPPED** |
| `auth.logout` | "log out" · "sign out" | `auth.logout` | `await_logout` (`ui_intent: sign_out`) | Send `auth_action: logout` · clear session · start fresh anon | `signOut()` then anon signup | **SHIPPED** |
| `auth.upload_photo` | "add my picture" · "upload photo" · accept after Lana suggests | `auth.upload_photo` | `await_profile_photo` (`ui_intent: upload_profile_photo`) | Show file/camera picker · `POST /lana/profile-photo` | none (Lana endpoint) | **SHIPPED** |

### §2.7 · SETTINGS · utility

| Intent ID | Trigger phrasing | `active_intent` | `routing_phase` (proposed) | Lana action | Tool | v0.2 status |
|---|---|---|---|---|---|---|
| `settings.change_name` | "change my name" · "my name is actually ___" | `identity.add_claim` (display_name dimension) | `collect_display_name` | Update `users.nickname` | `update_user_name` | **SHIPPED** |
| `settings.change_zip` | "I moved" · "update my ZIP" | `settings.change_zip` | `need_zip` | Re-prompt ZIP · re-derive block | `update_user_zip` · `get_blocks_near_zip` | **TO BUILD** |
| `settings.notification_prefs` | "stop texting me" · "I want fewer messages" | `settings.notification_prefs` | `confirm_settings` | Update SMS frequency | `update_user_prefs` | **TO BUILD** |
| `help.what_can_you_do` | "what can you do" · "help" · "what is this" | `help.what_can_you_do` | `listening` | R · capability summary (warm · 2-3 sentences) | none (LLM only) | **CONVERSATIONAL** |
| `help.who_are_you` | "who are you" · "what's your name" | `help.who_are_you` | `listening` | R · "I'm Lana · your block concierge" + warm follow-up | none | **CONVERSATIONAL** |

---

## §3 · Fall-back intents (14 categories · out-of-scope · captured to dashboard)

Each fall-back follows the 4-step playbook from `LANA_OUT_OF_SCOPE_PLAYBOOK.md`: **ACKNOWLEDGE the underlying need · ENGAGE 1-2 turns · NAME the constraint · CAPTURE the signal**.

All captures call `capture_inquiry` → land in `out_of_scope_inquiry` table → flow through the Signal Aggregator.

| Category ID | Example phrases | ACK pattern | What Lana captures |
|---|---|---|---|
| `services.beauty` | "looking for a nail tech" · "manicurist nearby" · "where do you get your hair done" · "Brazilian wax" | "you've earned an hour" | category · sub-service · preferred-language · willingness to pay |
| `services.childcare` | "babysitter for Friday" · "looking for a nanny" · "daycare options" | "you can't be in two places at once" | care-type · ages · cadence · price range |
| `services.education` | "Portuguese tutor for my kid" · "piano teacher" · "math tutor" · "language class" | "you're trying to give them everything" | subject · age · in-person vs virtual |
| `services.health` | "pediatrician recommendation" · "therapist" · "doula" · "lactation consultant" | "you want someone you can trust" | specialty · insurance · language |
| `services.home` | "house cleaner" · "organizer" · "handyman" · "general contractor" | "the to-do list never ends" | service-type · cadence · budget |
| `services.food` | "meal prep" · "organic produce delivery" · "personal chef" · "catering" | "you've stopped having time to cook" | service-type · dietary · cadence |
| `housing.rental` | "3-bedroom in Lake Nona" · "looking to rent" | "moving is a lot" | type · neighborhood · timeline · budget |
| `housing.buying` | "homes for sale near a good school" | "you're building a life here" | type · neighborhood · timeline · school priority |
| `housing.schools` | "best public school" · "how's Laureate Park Elementary" | "you want what's right for them" | school name · age · question category (rating/culture/safety) |
| `commerce.local_business` | "best restaurant in Lake Nona" · "kid-friendly café" · "where to take grandparents" | "you want to do it right" | category · occasion |
| `commerce.events` | "what's happening this weekend" · "festivals near me" · "kids' events" | "you're looking for a moment" | event-type · timeframe · age-appropriate |
| `transport.rideshare` | "carpool to school" · "ride to soccer" | "the logistics never end" | route · cadence · ages |
| `emergency.urgent` | "kid is sick · need a doctor now" · "stuck somewhere" · "emergency" | **escalate** · ACK + offer 911 + local urgent care | severity flag (don't aggregate · alert immediately) |
| `vent.companionship` | "I just need to talk" · "it's been a hard day" · "I'm lonely" | "I'm here" | sentiment · let conversation breathe · no capture · default to R |

**Note on `emergency.urgent`:** this is the only fall-back that does NOT just capture. Lana must ACK + offer real-world resources (911, Poison Control, urgent care) before any capture. See `LANA_MODUS_OPERANDI` Agent 6 (Safety) for the doctrine.

**Note on `vent.companionship`:** this is the COMPANIONSHIP capability surface · not a fall-back. Lana responds warmly without capturing. Distinct from a service ask.

---

## §4 · Backend routing table for Asjid

For each intent: the route Asjid's worker needs to handle · what state machine transition fires · what slots are required · what gets persisted.

```
┌─────────────────────────┬──────────────┬─────────────────────────┬─────────────────────┬──────────────────────────┐
│ INTENT ID               │ HANDLER       │ REQUIRED SLOTS          │ STATE TRANSITION    │ PERSISTS                 │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ DISCOVERY                                                                                                          │
│ discovery.find_peers    │ discovery.   │ zip, identity, nickname │ listening→need_zip→ │ user_identity_claims     │
│                         │ route.find   │                         │ need_identity→...   │ peer_matches (computed)  │
│ discovery.find_by_attrs │ discovery.   │ zip, filters[]          │ need_identity→      │ same + filter args       │
│                         │ route.find   │                         │ preview              │                          │
│ discovery.find_in_block │ discovery.   │ zip                     │ listening→preview    │ local_signals (read)     │
│                         │ route.block  │                         │                      │                          │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ IDENTITY                                                                                                           │
│ identity.add_claim      │ identity.    │ utterance               │ in-flow background  │ user_identity_claims     │
│                         │ extract      │                         │                     │                          │
│ identity.edit_claim     │ identity.    │ dimension, value         │ confirm_profile→    │ user_identity_claims     │
│                         │ extract      │                         │ need_identity        │                          │
│ identity.complete       │ identity.    │ (none · session-wide)    │ confirm_profile→    │ users.identity_summary  │
│                         │ complete     │                         │ listening            │ embeddings               │
│ identity.show_profile   │ identity.    │ (none)                   │ confirm_profile      │ (read only)              │
│                         │ read         │                         │                     │                          │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ LOOKING LANE  (4-PHASE VOICE-FIRST CASCADE · NEW for v0.2.x)                                                       │
│ looking.swap            │ signal.      │ item, stage, when        │ listening→extract→  │ local_signals            │
│                         │ capture      │                         │ confirm_missing→     │ (intent=swap_seek)       │
│                         │              │                         │ listening_state      │                          │
│ looking.meet            │ signal.      │ type, when, who         │ same                 │ local_signals            │
│                         │ capture      │                         │                     │ (intent=meet_seek)       │
│ looking.tip             │ signal.      │ category, why           │ same                 │ local_signals            │
│                         │ capture      │                         │                     │ (intent=tip_seek)        │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ SHARING LANE  (mirror 4-PHASE · NEW for v0.2.x)                                                                    │
│ sharing.swap            │ signal.      │ item, stage, when        │ same                 │ local_signals            │
│                         │ capture      │                         │                     │ (intent=swap_offer)      │
│ sharing.host            │ signal.      │ title, when, where      │ same                 │ local_signals            │
│                         │ capture      │ audience                │                     │ (intent=host_meet)       │
│ sharing.tip             │ signal.      │ category, value          │ same                 │ local_signals            │
│                         │ capture      │                         │                     │ (intent=tip_share)       │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ RELATIONSHIP TIER                                                                                                  │
│ tier.send_nudge         │ tier.nudge   │ target_user, draft       │ listening→          │ relationship_tier_state  │
│                         │              │                         │ compose_nudge→      │ nudge_messages           │
│                         │              │                         │ nudge_sent           │ notifications (queued)   │
│ tier.respond_nudge      │ tier.respond │ nudge_id, action         │ external→tier_      │ same                     │
│                         │              │                         │ advance/declined/   │                          │
│                         │              │                         │ blocked              │                          │
│ tier.advance_*          │ tier.advance │ relationship_id, level   │ background           │ relationship_tier_state  │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ AUTH  (already shipped per LANA_UNIFIED_DISCOVERY_FRONTEND)                                                        │
│ auth.signup_phone       │ guest_signup │ phone (E.164)            │ await_signup_phone   │ auth_action emitted      │
│ auth.signup_otp         │ guest_signup │ phone, otp               │ await_signup_otp     │ auth_action emitted      │
│ auth.login_phone        │ guest_login  │ phone                    │ await_login_phone    │ auth_action emitted      │
│ auth.login_otp          │ guest_login  │ phone, otp               │ await_login_otp      │ auth_action emitted      │
│ auth.logout             │ guest_logout │ (none)                   │ await_logout         │ auth_action emitted      │
│ auth.upload_photo       │ profile_     │ file (multipart)         │ await_profile_photo  │ users.profile_photo_url  │
│                         │ photo        │                         │                     │ Supabase Storage avatars │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ SETTINGS                                                                                                           │
│ settings.change_name    │ identity.    │ display_name             │ collect_display_name │ users.nickname           │
│                         │ extract      │                         │                     │                          │
│ settings.change_zip     │ user.update  │ zip                      │ need_zip             │ users.home_block_id      │
│ settings.notif_prefs    │ user.update  │ prefs (json)             │ confirm_settings     │ users.notification_prefs │
├─────────────────────────┼──────────────┼─────────────────────────┼─────────────────────┼──────────────────────────┤
│ FALL-BACK (all → capture_inquiry · then aggregated by Signal Aggregator)                                           │
│ services.beauty         │ capture_     │ raw_query, category=     │ (no transition)      │ out_of_scope_inquiry     │
│ services.childcare      │ inquiry      │  beauty|child|edu|...    │                     │ (+ sentiment + embedding │
│ services.education      │              │ block_id, user_id        │                     │  + follow_up_eligible)   │
│ services.health         │              │ urgency, sentiment       │                     │                          │
│ services.home           │              │                         │                     │                          │
│ services.food           │              │                         │                     │                          │
│ housing.rental          │              │                         │                     │                          │
│ housing.buying          │              │                         │                     │                          │
│ housing.schools         │              │                         │                     │                          │
│ commerce.local_business │              │                         │                     │                          │
│ commerce.events         │              │                         │                     │                          │
│ transport.rideshare     │              │                         │                     │                          │
│ emergency.urgent        │ safety.      │ severity, location?      │ escalate→listening   │ safety_event             │
│                         │ escalate     │                         │                     │ (high-priority alert)    │
│ vent.companionship      │ (LLM only)   │ (none)                   │ listening            │ (no DB write)            │
└─────────────────────────┴──────────────┴─────────────────────────┴─────────────────────┴──────────────────────────┘
```

### §4.1 · What Asjid needs to extend in `lana_unified_pipeline.py`

The shipped pipeline handles **3 capability surfaces**: discovery · identity · auth. To reach v0.2 parity with the walkthrough, **2 new capabilities need routing**:

1. **`signal.capture` handler** — fires for all 6 LOOKING + SHARING intents · 4-phase voice-first cascade · writes to `local_signals`. Reuses identity-extract pattern (background extract during chat).
2. **`tier.nudge` handler** — fires for `tier.send_nudge` · drafts message · queues notification · writes to `relationship_tier_state`. Also handles `tier.respond_*` external triggers.

The `capture_inquiry` tool from the Out-of-Scope playbook needs to be wired as a **fall-back catch-all** for the orchestrator when no in-scope intent classifies above 0.85 confidence (per `LANA_TOOL_ROUTING_v1.md §3` confidence bands).

### §4.2 · Recommended DB extensions (Asjid · for v0.2)

```sql
-- v0.2 · the local-signal log (replaces v0.1 swap_intents / meet_intents)
CREATE TABLE local_signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  block_id UUID REFERENCES blocks(id),
  zip TEXT,
  intent TEXT NOT NULL CHECK (intent IN (
    'swap_seek','swap_offer','meet_seek','host_meet','tip_seek','tip_share'
  )),
  category TEXT,
  detail_text TEXT,
  affinity_tags TEXT[],
  stage TEXT,
  status TEXT DEFAULT 'listening' CHECK (status IN (
    'listening','matched','published','closed','expired'
  )),
  source_surface TEXT DEFAULT 'lana',
  embedding VECTOR(1536),
  contact_permission BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);

CREATE INDEX idx_local_signals_block_intent ON local_signals(block_id, intent) WHERE status = 'listening';
CREATE INDEX idx_local_signals_embedding ON local_signals USING ivfflat (embedding vector_cosine_ops);

-- v0.2 · relationship tier state (5-stage ladder)
CREATE TABLE relationship_tier_state (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  peer_user_id UUID REFERENCES users(id) NOT NULL,
  tier TEXT NOT NULL DEFAULT 'stranger' CHECK (tier IN (
    'stranger','nudge_pending','acquaintance','direct','irl'
  )),
  nudge_id UUID,
  last_advanced_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(user_id, peer_user_id)
);

-- v0.2 · the fall-back capture (per LANA_OUT_OF_SCOPE_PLAYBOOK)
CREATE TABLE out_of_scope_inquiry (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id),
  block_id UUID REFERENCES blocks(id),
  zip TEXT,
  raw_query TEXT NOT NULL,
  extracted_intent TEXT NOT NULL,
  extracted_category TEXT NOT NULL,
  sentiment TEXT,
  urgency TEXT,
  embedding VECTOR(1536),
  follow_up_eligible BOOLEAN DEFAULT TRUE,
  captured_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_inquiry_category_time ON out_of_scope_inquiry(extracted_category, captured_at DESC);
CREATE INDEX idx_inquiry_embedding ON out_of_scope_inquiry USING ivfflat (embedding vector_cosine_ops);
```

---

## §5 · Open questions for the team

1. **Asjid:** confirm `local_signals` schema replaces the v0.1 `swap_intents` + `meet_intents` tables OR sits alongside (separate concerns)? Recommend: replace · one unified table simplifies the matcher.
2. **Asjid:** confirm the orchestrator should fire `capture_inquiry` as the catch-all fallback when no in-scope intent classifies ≥0.85 confidence (per `LANA_TOOL_ROUTING_v1.md §3`).
3. **Abdullah:** does the frontend handle `ui_intent` for the new lane-cascade phases (the 4-phase looking/sharing flow)? The shipped contract covers discovery + auth · we'll need new `ui_intent` values for `collect_looking_*` / `collect_sharing_*` if you can't reuse `chat` + bubble copy.
4. **Aki (when back):** confidence-threshold defaults (0.85 high / 0.50 medium / <0.50 low) — confirm OR tune from production logs after week 1.
5. **Founder:** any additional fall-back categories you've heard from the Brazilian-moms survey (n=8) that aren't in §3's list of 14?

---

## §6 · Sources

- `LANA_AGENT_ARCHITECTURE_v1.md` — Drive
- `LANA_MODUS_OPERANDI.md` — Drive
- `LANA_TOOL_ROUTING_v1.md` — Drive
- `LANA_OUT_OF_SCOPE_PLAYBOOK.md` — Drive
- `LANA_SIGNAL_AGGREGATOR.md` — Drive
- `LANA_AGENT_RESEARCH_v1.md` — Drive
- `LANA_UNIFIED_DISCOVERY_FRONTEND.md` — GitHub (TagAlng-Backend repo)
- `lana-v01-walkthrough.html v0.2.7` — local R&D folder
- Brazilian moms survey (n=8 · Orlando western suburbs) — referenced earlier in thread

---

*v1 first pass · 2026-06-12 · Tommaso, please review/edit/forward to Asjid+Abdullah · Aki when back · this becomes §X of the master Lana TPR.*
