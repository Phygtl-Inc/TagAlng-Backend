# Lana — What We Built (team & manager summary)

**Audience:** product, engineering, leadership  
**Environment:** tagalng-dev  
**Status:** implemented in code · migration + deploy needed for full memory on dev  
**Technical deep-dive:** [`LANA.md`](LANA.md) · specs in `LANA_AGENT_ARCHITECTURE_v1.md`, `LANA_TOOL_ROUTING_v1.md`

---

## TL;DR (30 seconds)

**Lana** is TagAlng’s block concierge: she chats with neighbors, learns who they are, helps them host activities, and **captures asks we can’t fulfill yet** (so product learns what to build).

We shipped a **real agent loop** on Cloud Run — not just a chat UI:

- **Claude on Vertex** decides each turn (Haiku routes, Sonnet speaks)
- **Python enforces the rules** (when to chat vs ask vs act vs capture)
- **Two-layer memory** (sticky session note + searchable past)
- **Tools** for profile, events, out-of-scope capture, and social graph actions
- **Audit log** for every turn

**Good for demo today:** profile intake, event hosting, warm out-of-scope capture, publish-on-confirm in chat.  
**Not yet:** full marketplace/discovery modules, production-grade safety AI, PWA accept screens for nudge/intro/cohost.

---

## What Lana is (plain English)

Lana sits on three ideas from the product:

| Pillar | What Lana does |
|--------|----------------|
| **Identity** | Learns your story → saved as **claims** (structured profile threads) |
| **Vicinity** | Knows your **home block**, neighbors, upcoming activities |
| **Activity** | Helps you **plan and publish** a block event in conversation |

**Hard rules we follow:**

- Lana **never invents** people, events, or facts not in context
- Lana **never auto-messages** neighbors without explicit tools + user intent
- Out-of-scope asks are **captured**, not brushed off — they become product signal

---

## What happens on every message

```
Neighbor sends a message
        ↓
   Safety check (crisis keywords → hotline response)
        ↓
   Load memory (sticky note + search past relevant chats/asks)
        ↓
   AI router: "What does the user want?"
        ↓
   Code rules: "Are we allowed to act? Do we have enough info?"
        ↓
   Maybe run a tool (save ask, update event draft, publish, recall memory…)
        ↓
   Lana writes the reply (warm, short, on-brand)
        ↓
   Save chat, update session memory, write audit log
```

This matches our architecture doc: **the AI proposes, the code decides.**

---

## The four turn types (R / A / T / C)

Every message ends in exactly one of these — defined in `LANA_TOOL_ROUTING_v1.md`:

| Code | Means | Example user message | What Lana does |
|------|-------|----------------------|----------------|
| **R** | **Respond** — just talk | "Rough morning." | Empathize, no backend action |
| **A** | **Ask** — one missing piece | "Want to host coffee." | "Love it — **when** and **where**?" |
| **T** | **Tool** — do something | "Coffee Sat 10am at my place." | Update draft → "Publish?" |
| **C** | **Capture** — we can't do it yet | "Know a nail tech?" | Save ask to DB + warm reply |

**What we fixed:** these rules used to live only in the AI prompt. Now **Python enforces them** (`enforce_routing`) — e.g. low confidence → chat, missing slots → ask, off-topic → capture.

---

## Examples by flow

### 1. Profile intake (signup chat)

**Purpose:** `profile_intake`

```
Lana:  "Tell me a bit about you — what brought you to the block?"
You:   "From São Paulo, new mom, love morning walks."
Lana:  Highlights your phrases, asks a follow-up or two.
You:   (when enough is shared)
App:   POST /complete → claims extracted → user_identity_claims + embeddings
```

**What gets saved:** structured identity threads (not raw chat) — chat is UX, claims are truth.

---

### 2. Event hosting

**Purpose:** `event_draft`

```
You:   "Sunday brunch for new parents, 11am at the park."
Lana:  Saves title / time / place into an event draft.
Lana:  "Got it: Brunch · Sun 11am · the park. *Publish?*"
You:   "Yes"
Lana:  Calls create_event (if phone verified) — publish can happen in chat, not only on /complete.
```

**Rules enforced:** won't publish with empty title, time, or place; won't use placeholder values like "TBD".

---

### 3. Out-of-scope (product signal)

```
You:   "Is there a babysitter on the block?"
Lana:  "I can't book that yet — but I noted it for the block."
       → row in inquiry_signals (category, text, embedding)
```

**Why it matters:** every "we don't do that yet" becomes roadmap input, not a dead end.

---

### 4. Memory — "did I ask about that before?"

We implemented **MemGPT-style memory** (`LANA_AGENT_ARCHITECTURE_v1` §4):

| Layer | Easy analogy | What it holds |
|-------|--------------|---------------|
| **Core block** | Sticky note on the desk | Block name, session goal, event draft, tiers with neighbors, last topic |
| **Archival store** | Filing cabinet | Past messages, claims, captured asks — searched when relevant |

**Every turn:** before Lana replies, we **prefetch** top matches from the user's past (same embedding tech as matching).

```
Week 1:  "Looking for a tutor for my kid"  → captured
Week 2:  "Did I already ask about a tutor?"
Lana:    Can ground the answer in last week's capture (not just the last few lines of chat).
```

**Scopes:**

- `self` — your claims, your chats, your captured asks
- `neighbors` — facts about neighbors (tier-gated for privacy)
- `block` — public block patterns (table ready; content added over time)

---

### 5. Safety (crisis path)

```
You:   "I can't go on like this"
Lana:  Crisis template + 988 / support resources (no normal tool routing)
       → flag_sensitive logged in audit
```

Basic keyword safety is live. Full moderation stack (Llama Guard, tier-leak checker) is **not** built yet.

---

### 6. Social graph tools (backend ready)

These RPCs + Lana tools exist; **PWA accept UI** is still needed:

| Tool | What it does |
|------|--------------|
| `send_nudge` | Tap-to-wave style nudge to a neighbor |
| `propose_intro` | Suggest two neighbors meet (with reason) |
| `propose_cohost` | Invite someone to co-host an event |
| `update_relationship_tier` | Promote tier after nudge accept, intro, same event, etc. |

---

## What we implemented (checklist)

### Runtime & AI

- [x] Cloud Run worker (`tagalng-lana-worker`) with orchestrator on by default
- [x] **Claude Haiku** — per-turn router (intent, confidence, tool pick)
- [x] **Claude Sonnet** — user-facing replies + extract on `/complete`
- [x] **Gemini** — embeddings only (`text-embedding-005`, 768 dims)
- [x] Legacy fallback: `LANA_ORCHESTRATOR=legacy` → Gemini-only chat

### Routing & discipline

- [x] R / A / T / C pipeline end-to-end
- [x] **Hard routing in Python** — confidence bands, slot validation, purpose guards
- [x] Confirm → publish in chat (`user_confirmed` on `publish_activity`)
- [x] Out-of-scope → `capture_inquiry` (including repair if Lana refuses without capturing)
- [x] Per-turn **audit log** (`lana_audit_log`)

### Memory (MemGPT)

- [x] Core block built every turn, **persisted** on `lana_sessions.core_block`
- [x] Synthesizer **core_patch** (session goal, last topic, last capture id)
- [x] Message **embeddings** on insert
- [x] **Pre-turn prefetch** (self + neighbors)
- [x] **`recall` tool** + `lana_recall_memories` RPC
- [x] Tables: `neighbor_facts`, `block_context` (writes/seeding mostly future)

### Data & tools

- [x] `inquiry_signals` — captured out-of-scope asks
- [x] Profile complete → `user_identity_claims` + embeddings
- [x] Event draft + `create_event` publish path
- [x] Social graph migration (tiers, nudges, intros, co-host invites)

### Docs

- [x] [`LANA.md`](LANA.md) — engineering overview
- [x] This doc — team/manager summary

---

## How this follows our spec docs

| Spec document | What it defines | How we follow it |
|---------------|-----------------|------------------|
| `LANA_AGENT_ARCHITECTURE_v1.md` | Orchestrator, memory, guardrails, tools | Router + synth + pipeline; MemGPT core + archival; partial guardrails |
| `LANA_TOOL_ROUTING_v1.md` | R/A/T/C decision tree, slots, confirm | `enforce_routing()` + `slots.py` in code |
| `LANA_MODUS_OPERANDI.md` | Six capability modules | Profile + activity + companionship partial; discovery/marketplace not full |
| `LANA_OUT_OF_SCOPE_PLAYBOOK.md` | Capture, don't refuse cold | `capture_inquiry` + refusal repair |
| `LANA.md` | Dev reference | Updated for orchestrator + memory |

---

## What's not built yet (honest gaps)

| Area | Status |
|------|--------|
| Discovery module (`find_matches`, joint moments) | Not built |
| Marketplace module (exchange listings) | Not built |
| Full guardrails (moderation API, tier-leak, groundedness judge) | Keywords + one repair only |
| Full XState 8-state session machine | Light state labels only |
| PWA: accept nudge / intro / co-host | RPCs exist, UI needed |
| 500-turn golden eval + nightly regression | Not run |
| Voice (STT/TTS) | Not built |

**Rough completeness vs full v1 spec:** ~50–55% of the written architecture — **~70%** of what the PWA needs for profile + event demo on dev.

---

## Migrations to apply (dev)

Run in order on tagalng-dev:

1. `20260612120000_lana_orchestrator.sql` — inquiry_signals, audit log, core_block column  
2. `20260613120000_social_graph_lana_tools.sql` — tiers, nudges, intros, co-host  
3. `20260614120000_lana_memgpt_memory.sql` — message embeddings, recall RPC, neighbor_facts, block_context  

Then redeploy:

```bash
supabase db push
./scripts/deploy-lana-worker.sh
```

---

## API surface (unchanged for frontend)

Frontend still uses the same endpoints:

| Endpoint | Use |
|----------|-----|
| `POST /lana/sessions` | Start profile or event session |
| `POST /lana/sessions/{id}/messages` | Send a chat turn |
| `POST /lana/sessions/{id}/complete` | Finish → save claims or event |

Optional new fields in responses: `routing` metadata (outcome, tool, confidence) when orchestrator is on.

---

## Suggested demo script (5 minutes)

1. **Profile** — 3–4 turns about heritage / stage / interests → complete → show claims in DB  
2. **Event** — "Coffee Saturday 10am at my place" → confirm publish → show event on block  
3. **OOS** — "Need a nail tech" → show row in `inquiry_signals`  
4. **Memory** — new session: "Did I ask about beauty services?" → Lana references prior capture (after MemGPT migration + a few embedded messages)

---

## One paragraph for leadership

We built Lana as a ** governed agent**, not a free-form chatbot: Claude on Vertex handles language, but **TagAlng’s rules run in code** — when to ask clarifying questions, when to publish an event, and when to capture unmet demand. Memory works in two layers so Lana can remember past asks across sessions without stuffing entire chat logs into every prompt. Profile intake, event hosting, and product-signal capture are **demo-ready on dev** after migration and deploy; discovery, marketplace, and full safety eval are the next milestones.

---

*Questions? See [`LANA.md`](LANA.md) or the v1 spec PDFs in repo root / `docs/`.*
