# LANA · Blueprint · v1

*The master architecture document · investor-grade · team-actionable · 2026-06-12*
*Authored by: founder + CTO/CAIO (Claude as proxy) · for: Tommaso · Yunchao (AI) · Asjid (backend) · Abdullah (frontend)*

> **The thesis in one sentence:** Lana is not a chatbot. Lana is a local, agentic concierge that compounds in value with every captured signal · every identity claim · every block we map · every match we make. The moat is not in any single model · it is in the data substrate, the proactive intelligence layer, and the per-block memory that no general-purpose AI can replicate.
>
> **The 30-second pitch:** Mom A says "looking for 3T rain boots." Lana finds the mom in her block who has them, drafts the intro, captures the trust ladder transition, and remembers both. A week later when mom A casually mentions "my kid does karate," Lana — without being asked — surfaces three latent matches (karate mom-meet, karate gear swap, recommended dojo). Every conversation grows the graph. Every match trains the ranker. Every captured-but-unfulfilled inquiry tells us what to build next. **The product gets smarter without us shipping code.**

---

## §1 · Mission and wedge

### 1.1 What we are building
A locally-grounded agentic AI concierge that helps real people in real neighborhoods do four things: **find** people like them, **share** what they have, **discover** what's around, and **build relationships** at their own pace. Lana is the always-available, never-judging, memory-keeping friend who happens to know every other mom on the block.

### 1.2 Why mom is the wedge
- **Highest unmet need** (Brazilian moms survey: 87.5% want a local tool · 62% cite lack of time · 75% prefer playground/park playdates)
- **Highest network density** — moms in the same ZIP have ~80% overlap in dimensions Lana cares about (life stage, kid age, language, faith)
- **Highest trust threshold** — if we win moms, we win their families, then their broader networks
- **Highest WTP for time-saving** — this cohort will pay or refer or evangelize, not just visit

### 1.3 Why local
Generic social apps fail at hyperlocal because:
- Trust doesn't scale with size — it scales with **proximity + shared identity**
- Discovery in dense networks needs **block-level granularity**, not city-level
- Safety requires **mediation**, not open chat
- The value object is **the introduction**, not the post

Lana wins by being the introduction-maker, not the timeline-publisher.

### 1.4 The expansion path (wedge → moat → empire)
- **v0.1 (June 2026 · 4 days from now):** Lake Nona East · Brazilian moms · ZIP 32827 · 20 seed users
- **v0.2 (Q3 2026):** Lake Nona corridor + Winter Garden · 5 ZIPs · 200 users
- **v0.3 (Q4 2026):** Orlando metro · 25 ZIPs · 2,000 users · second persona (runners or expats)
- **v1.0 (2027):** Top-10 US metros · multi-persona · 50K users · proactive Layer 3 in production
- **v2.0 (2028):** International (PT-BR + ES-LATAM markets) · 500K users · Lana-for-everyone platform

The product NEVER stops being concierge-first. The expansion is **persona depth**, not feature width.

---

## §2 · The 3-layer intent architecture

This is the central architectural commitment. Every utterance runs **three parallel intent extractions** every turn.

### Layer 1 · Explicit · in-scope · fulfillable
**Detection:** intent classifier (Hermes 4 by Nous · or Claude Sonnet 4 · or GPT-4) returns `(intent, confidence, slots)`.
**Action:** if `confidence ≥ 0.85` AND all required slots filled → call the matching tool. Otherwise ask for the missing slot.
**Catalog:** 23 linear intents documented in `LANA_INTENTS_AND_ROUTING_v1.md §2`.
**Frameworks:** Anthropic tool-use · OpenAI function-calling · Nous Hermes 4 · NVIDIA NeMo.

### Layer 2 · Explicit · out-of-scope · captured
**Detection:** Layer 1 confidence < 0.85 AND not a chat/companionship turn → fallback to `capture_inquiry`.
**Action:** 4-step graceful refusal (ACK underlying need · ENGAGE 1-2 turns · NAME the constraint · CAPTURE the signal) per `LANA_OUT_OF_SCOPE_PLAYBOOK.md`.
**Coverage:** 14 expected categories listed in `LANA_INTENTS_AND_ROUTING_v1.md §3` · but the system is **open-ended** · whatever comes in lands in `out_of_scope_inquiry` and the Signal Aggregator clusters post-hoc.
**Why this matters:** estimated 30-40% of v0.1 traffic will be out-of-scope. This is the **highest-signal data we own** · it's the v0.2 roadmap in disguise.

### Layer 3 · Latent · we infer · the moat
This layer runs **in parallel** with Layers 1 and 2, every turn. It is the difference between a chatbot and a concierge.

#### 3a · Circumstantial · flow-state driven
- **Trigger:** mom's position in a state machine + accumulated context → system infers next-best action without mom asking.
- **Examples:** mid-FTUE "time to ask for phone" · post-match "time to send the introduction" · 7-days-quiet "time to re-engage with a check-in".
- **Detection:** rule engine on the XState chart in `AI_PIPELINE_v1.md §3` · enriched with counters (turns_since_value, days_since_active) and predicates (has_active_signal, has_pending_nudge).
- **Frameworks:** XState (deterministic FSM) · LangGraph conditional edges · Sierra's "step-tracker" pattern.

#### 3b · Inferential · content-driven
- **Trigger:** mom mentions an *entity* (noun, activity, life event) in a turn → entity matches one or more capabilities she didn't explicitly invoke.
- **Examples:** "my kid does karate" → 3 suggestions (karate-mom meet, karate gear swap, dojo recommendation) · "my husband's stationed at Patrick AFB" → 2 suggestions (military spouses meet, deployment support tip thread).
- **Detection:**
  1. Entity extractor runs on every turn (separate from intent classifier · captures nouns/topics regardless of explicit ask)
  2. Embedding similarity against the **capability index** (each capability has a vector representation)
  3. Suggestion ranker filters by relevance, block density, and per-mom history
  4. Timing engine decides WHEN to surface (never mid-flow · only at natural breaks or session-resume)
- **Frameworks:** Letta (formerly MemGPT) memory architecture · Sierra "constellation of models" · Anthropic Claude tool-use with retrieval-augmented tool selection · Decagon's proactive customer success patterns · `Proactive Conversational Agents with Anticipatory Intent` (2024 research).

### Why this isn't in any competitor
- **Generic LLMs (ChatGPT, Claude, Gemini)** do Layer 1 well, Layer 2 partially, Layer 3 not at all
- **Nextdoor, Bumble, Peanut** don't have a conversational layer · the recommendation logic is timeline-feed based · no agentic memory
- **Hermes 4 by Nous Research** is excellent at Layer 1 function calling but provides no Layer 3 architecture · that's the system we build on top
- **Sierra, Decagon** do enterprise customer-support agents · their Layer 3 is single-context (one ticket) · we do it across MOM's entire conversational history across MONTHS

This is the IP. This is what investors buy. This is what gets harder for any competitor to copy as our data scales.

---

## §3 · The four capability surfaces

Lana is structured as **four capability clusters** (per `LANA_AGENT_ARCHITECTURE_v1.md` and `LANA_MODUS_OPERANDI.md`), each a tool group within the single-agent envelope.

| Surface | Role | v0.2 status | v0.3+ extension |
|---|---|---|---|
| **Identity / Intake** | Understand who mom is · 7-dimension claims (stage · activity · interest · lifestyle · language · heritage · faith) | Shipped (per Modus Operandi Agent 1) | Continuous identity refinement · multi-turn identity dialogues |
| **Discovery / Recall** | Find people · things · activities relevant to mom · heterogeneous (not just peer-to-peer) | Partial (peer-only per Yunchao's diagram) | Heterogeneous recall (people · meets · items · tips · events) · cross-graph search |
| **Concierge / Mediation** | Carry messages · draft nudges · advance trust tier · close the loop | Partial (nudge drafting yes · tier ladder yes · closing-the-loop partial) | Multi-party orchestration · 3-way intros · group nudges |
| **Memory / Continuity** | Remember everything · selectively forget · proactively surface · learn from feedback | Partial (Letta-style core+archival per Architecture doc · but no Layer 3 yet) | Layer 3a + 3b in production · per-mom personalization · cross-session continuity |

The model perceives one Lana voice. The system perceives four capability clusters. The user perceives one helpful, remembering friend.

---

## §4 · The heterogeneous knowledge graph

This is the data substrate that makes Layer 3 work. Yunchao's diagram (people → similar people / nearby people / connectors / collaborative signals) describes ONE edge type. Reality is multi-typed.

### 4.1 Node types
- **People** (users · their identity claims · their trust-tier state · their signal history)
- **Places** (blocks · ZIPs · venues · public parks · churches · gyms)
- **Things** (swap items · listed for free · listed for trade · listed for sale)
- **Activities** (karate · yoga · pottery · running · book club · church groups)
- **Events** (meets · playdates · hosted gatherings · classes)
- **Tips** (recommendations · reviews · location-anchored knowledge)
- **Identity claims** (heritage · life stage · language · faith · interests — first-class nodes, not just attributes of people)

### 4.2 Edge types
- **Person ↔ Person** (block proximity · trust tier · co-attendance history · nudge state)
- **Person ↔ Thing** (offers · seeks · once-owned · interested-in)
- **Person ↔ Activity** (does · used to do · wants to try)
- **Person ↔ Event** (hosting · attending · invited · declined)
- **Person ↔ Tip** (shared · saved · referenced)
- **Activity ↔ Place** (location of practice · regular meetup spot)
- **Event ↔ Activity** (the meet IS for an activity)
- **Tip ↔ Activity** (tip is about an activity)
- **Place ↔ Place** (proximity · adjacency · same-ZIP)

### 4.3 Why heterogeneous wins
- **Pinterest** does (user, board, pin) — three node types · their feed is heterogeneous → 70%+ engagement uplift vs flat
- **TikTok** does (creator, sound, video, trend, hashtag) — 5 node types · the For You Page is built on heterogeneous graph attention networks (HGAN)
- **Airbnb** does (host, listing, experience, neighborhood) — 4 node types · the homepage doesn't show only listings, it shows experiences + neighborhood guides
- **Spotify** does (user, song, playlist, artist, album, podcast) — discovery is graph-walked, not list-filtered

We are doing the same pattern for hyperlocal social. **No competitor in our space has this.**

### 4.4 The graph is also the moat
Every captured signal, every match made, every identity claim, every trust-tier transition writes to the graph. The graph compounds with usage. After 100,000 moms across 50 ZIPs, our graph encodes hyperlocal social facts no global model can ever know. **A new competitor cannot bootstrap this** — they'd need to re-collect the data per block.

---

## §5 · The three data flywheels

The product gets better with usage because three loops feed back into the system.

### 5.1 The signal flywheel
- Mom asks for X → in-scope intent → tool call → match found / not found
- If captured (out-of-scope OR latent) → `local_signals` table or `out_of_scope_inquiry` table
- Signal Aggregator clusters semantically → emerging categories surface
- Categories with sufficient density become **roadmap candidates** (per `LANA_SIGNAL_AGGREGATOR.md`)
- Engineering ships the new capability → next time the category is asked, it's in-scope
- **Loop tightens:** more usage → more captured signals → more capabilities → fewer captures, more fulfillment

### 5.2 The identity flywheel
- Mom describes herself → identity claims extracted (7 dimensions)
- Claims feed the recommendation ranker (similarity scoring)
- Mom mentions new things over time → identity refines
- Refined identity improves match quality → mom engages more → more identity claims surface
- **Loop tightens:** richer identity → better matches → more conversations → richer identity

### 5.3 The feedback flywheel (the missing piece Yunchao flagged)
- Recommendation shown → user accepted / dismissed / ignored / converted
- `recommendation_impressions` table logs every surface
- Ranker retrains on impressions → next recommendation is better
- Latent layer 3b learns which entity→capability mappings drive conversions
- **Loop tightens:** every recommendation surface is a labeled training example

These three loops collectively are the **compounding asset**. Investors are not buying our 2026 mockup. They are buying the predicted shape of our 2028 graph.

---

## §6 · Roadmap · v0.1 → v2.0

| Version | When | Wedge | New capabilities | Headcount |
|---|---|---|---|---|
| **v0.1** | June 15 2026 · Day Zero | Lake Nona East · 20 seed moms | Layer 1 (23 intents) · Layer 2 (capture only · no aggregation yet) · single-agent loop · single ZIP | 3 (you · Yunchao · Asjid · Abdullah part-time) |
| **v0.2** | Sept 2026 | + Winter Garden · 200 moms · Brazilian + general mom personas | Layer 2 with Signal Aggregator dashboard · heterogeneous recommendations (people + meets + tips + swaps) · Layer 3a (circumstantial · the FTUE signup chime-in works automatically) · Block-Log surface · feedback impression loop · privacy-safe reason builder | + 1 (data eng) |
| **v0.3** | Q4 2026 | Orlando metro · 2,000 moms · second persona (runners or expats) | Layer 3b (inferential · the karate-suggestion pattern) · proactive Lana (check-ins · re-engagement) · multi-language production-grade (PT/ES/EN) · per-mom personalization based on impressions | + 1 (ML eng) |
| **v1.0** | Q2 2027 | Top-10 US metros · 50K moms | Multi-persona platform · Lana skill marketplace (community-built capabilities) · monetization via concierge-mediated commerce (% on swaps that become sales) | + 5 (mobile · ops · GTM) |
| **v2.0** | 2028 | International (LATAM + UK + Australia) · 500K users | Full agentic platform · per-block fine-tuned models · federated learning · multi-modal (voice-first + image · location-aware) · Layer 3c (predictive · "Lana foresaw mom needed X") | + 15 |

### 6.1 Day Zero scope · what ships June 15
- 20 seed moms in Lake Nona East
- 23 Layer 1 intents working end-to-end
- 14 Layer 2 categories captured (no aggregation yet · founder daily-reviews captures)
- 5 Concierge actions: nudge · intro · join activity · cohost · capture-out-of-scope
- The 4-frame canonical signup + 3-frame sign-in (per v0.2.6)
- The 4-state Lana mascot (per `LANA_STATE_MODEL_v1.md`)
- The RADAR popover with Block-Log preview (per `BLOCK_LOG_SPEC_v1.md`)
- The compass-aggregator nav (per v0.2.10)
- Push + SMS notifications (per `BLOCK_LOG_SPEC_v1.md §6`)

### 6.2 What we DELIBERATELY don't ship at v0.1
- Map view (no density to justify)
- Public marketplace browse (no supply to browse)
- Open peer chat (Lana mediates everything)
- Layer 3 (no production data to train it)
- Multi-persona (mom only)
- Cross-ZIP discovery (single ZIP only)

These are NOT failures · they are the **earned roadmap**. Each unlocks when the data warrants.

---

## §7 · Team org and ownership

### Yunchao · AI / ML
**Owns:** model selection · prompt engineering · Layer 1 intent classifier · Layer 3 architecture (with founder + Claude) · capability index design · evals · ranker training.
**Doesn't own:** product spec · UX · backend infrastructure · frontend code.
**v0.2 priorities:** ship the 23-intent classifier · build the capability index · prototype Layer 3b with the karate example end-to-end. See `YUNCHAO_AI_HANDOFF_v1.md`.

### Asjid · Backend / Infrastructure
**Owns:** Supabase schema · `lana-worker` service · all 24 Drive-listed tables · RLS policies · API contracts · Twilio + push notification dispatch · Signal Aggregator pipeline.
**Doesn't own:** model selection · UX · prompt engineering.
**v0.2 priorities:** add `local_signals` · `out_of_scope_inquiry` · `block_log_entries` · `relationship_tier_state` · `latent_signals` · `capability_index` · `recommendation_impressions` tables · ship the Block-Log API · wire `capture_inquiry` as orchestrator fallback. See `ASJID_BACKEND_HANDOFF_v1.md`.

### Abdullah · Frontend / Mobile
**Owns:** PWA + native mobile UI · the v0.2.10 mockup → production parity · the 4-state Lana mascot integration · the rail-collapse compass interaction · the canonical signup/signin · the RADAR popover with CSS-radio tabs.
**Doesn't own:** AI integration logic (only consume the unified-chat contract) · backend schema · model behavior.
**v0.2 priorities:** ship the mockup-to-production parity by Day Zero · wire `ui_intent` switching to render the right input chrome · ensure iOS-safe 16px inputs · handle the auth_action flow for signup/login/logout. See `ABDULLAH_FRONTEND_HANDOFF_v1.md`.

### Tommaso · Founder / Product / CAIO proxy
**Owns:** product spec · brand · GTM · investor narrative · Lana voice / tone / refusal pattern · cohort selection · Day Zero ops.
**Final say on:** product priorities · what ships v0.1 vs deferred · brand voice · safety calls.

---

## §8 · The compounding moat · what investors are buying

### 8.1 What we have today (defensible)
- The **`out_of_scope_inquiry` flywheel** — the per-block roadmap-from-data pipeline. No competitor has this design.
- The **identity-claim graph** — every mom's 7-dimension claims, embedded, queryable. No competitor has hyperlocal identity at this granularity.
- The **trust tier ladder** — Stranger → Nudge → Acquaintance → Direct → IRL. We mediate every transition. This is the safety moat.
- The **Lana persona** — single voice · single character · cross-session memory · brand-protected.

### 8.2 What we add v0.2-v0.3 (defensibility doubles)
- Layer 3 latent-intent inference — proactive suggestion is the **engagement moat** (3-5x DAU vs reactive-only · per Replika data)
- The heterogeneous knowledge graph — recommendations across 6 node types · not just peer-to-peer · this is the **discovery moat**
- The feedback impression loop — every recommendation labeled, every refusal labeled, the ranker improves with usage · this is the **personalization moat**
- The Signal Aggregator dashboard — every captured-but-unfulfilled inquiry becomes roadmap intelligence · this is the **roadmap moat** (we know what to build before competitors do)

### 8.3 What v1.0+ unlocks (the empire)
- Network effects across cities — moms moving between metros bring their identity claims · cross-city introductions
- Concierge-mediated commerce — once a swap becomes a sale, we take a percentage · revenue without ads
- B2B angle — local businesses pay to be IN the tip layer · without polluting the user surface
- Lana-as-platform — communities (runners, hobbyists, expats) get their own Lana variant · same infrastructure
- Federated learning — each ZIP fine-tunes a local ranker · privacy-preserving · cohort-specific

### 8.4 What kills this thesis
Honest risks:
- **Cold start at v0.1** — if the first 20 moms don't activate, no flywheel starts. Mitigation: founder-curated seed cohort · IRL meets within first 2 weeks · direct hand-holding.
- **Safety incident** — a predatory user gets through, trust collapses. Mitigation: strict trust tier · phone-verification gate · mediated chat only · automated abuse detection · human review for first 1,000 nudges.
- **LLM hallucination at scale** — Lana invents a person or a meet that doesn't exist. Mitigation: every response grounded in graph tool calls · no free-form claims · output rails check groundedness (per `AI_PIPELINE_v1.md §6`).
- **Competitor moves fast** — Bumble or Peanut adds a "Lana" feature. Mitigation: we ship Layer 3 BEFORE they do · the data flywheel is harder to copy than the UX.

---

## §9 · The 2027-2028 vision (beyond mom)

### 9.1 Vertical expansion (persona depth)
Once Lana works for moms in Lake Nona, the playbook is provable. We add:
- **Runners** (year 2 wedge · same hyperlocal social need · different identity dimensions)
- **Expats** (cross-cultural · cross-language · longitude-shifted)
- **Empty nesters** (post-kids · activity-shifted)
- **Faith communities** (church groups · synagogue groups · etc.)

Each new persona reuses 80% of the infrastructure. Adds 20% new identity dimensions, new capability cluster names, new fallback categories.

### 9.2 Horizontal expansion (capability depth)
- **Lana for daily logistics** — carpool scheduling · school PTO coordination · neighborhood watch
- **Lana for commerce** — local services discovery · gig coordination · group buy
- **Lana for emergency** — mom-to-mom help in real emergencies (kid sick, locked out, need a sub for soccer practice)
- **Lana for hosting** — block parties · neighborhood events · multi-mom orchestration

Each capability follows the same Layer 1 / 2 / 3 pattern. Same Modus Operandi doctrine. Same out-of-scope playbook.

### 9.3 Platform expansion (Lana-as-OS)
By 2028 we are not "TagAlng" anymore. We are **the local agentic AI platform**. Communities (HOAs, churches, schools, sports leagues) can deploy their own Lana variant. We become infrastructure.

The data substrate (graph + flywheels + memory) is portable. The voice (Lana) is reusable. The Out-of-Scope Playbook generalizes. **This is the path to a $10B+ valuation.**

---

## §10 · What the founder didn't ask but should be asking

I'm putting myself in the shoes of a Silicon Valley CTO / CAIO. Here are the questions you (Tommaso) have NOT yet asked but that I, in your seat, would be losing sleep over.

### 10.1 Evaluation framework — how do we know Lana is getting better?
You don't have an eval set. You have user feedback (anecdotal) and metrics (DAU). For an agentic system, you need:
- **Golden eval set** · 100-1000 manually-labeled turns covering all 23 intents + 14 fallback categories + edge cases
- **Per-module evals** · Identity Intake accuracy · Discovery match quality · Concierge nudge tone · Capture pattern adherence
- **Adversarial evals** · what happens when a user tries to game Lana? When a predator uses sweet-talk? When someone tries to get free services?
- **Voice consistency evals** · does Lana sound like Lana across 10,000 turns?

**Recommendation:** ship `LANA_EVAL_FRAMEWORK_v1.md` in WAVE 2.

### 10.2 Safety architecture — beyond out-of-scope, what about adversaries?
The Out-of-Scope Playbook handles graceful refusal. But:
- What about a predator pretending to be a mom?
- What about minors using a parent's phone?
- What about a competitor scraping our data via fake accounts?
- What about jailbreak attempts on the LLM?
- What about a user spamming nudges?

**Recommendation:** ship `LANA_SAFETY_v1.md` in WAVE 2 covering: identity verification · age gating · rate limits · adversarial robustness · LLM red-teaming · human review tiers.

### 10.3 The cost model — what does it actually cost to run Lana per mom?
You haven't surfaced this. At v0.1 it's negligible. At 50,000 moms?
- LLM costs per turn (Haiku for router, Sonnet for synthesis) × turns/day × DAU
- Vector DB storage × moms × dimensions
- Twilio SMS × notifications × moms
- Supabase storage × signal density
- Whisper STT × voice turns

At v1.0 we need to know our **per-mom marginal cost** to model unit economics. Recommendation: ship `LANA_UNIT_ECONOMICS_v1.md` in WAVE 3.

### 10.4 The model abstraction layer — what if Hermes/Claude/GPT changes pricing 10x?
Today we lock in a model. Tomorrow vendors raise prices, deprecate features, or release a model 100x cheaper. We need:
- An **abstraction layer** so router/synthesizer can swap between Hermes 4 · Claude Sonnet 4 · GPT-4o · Gemini · open-weight alternatives
- A **routing policy** that picks the right model per turn type (cheap router for classifier, premium for synthesis)
- A **fallback chain** when a model is unavailable

**Recommendation:** include in `ASJID_BACKEND_HANDOFF_v1.md` as a tier-1 architectural concern.

### 10.5 Observability — when Lana misclassifies, how do we find it?
Today you have no telemetry. You need:
- Structured logging of every turn (intent, confidence, tools called, latency, model used)
- A dashboard showing failure rates per intent
- A "Lana said something weird" review queue (user-flagged turns)
- Latency SLOs (p50 < 1s, p95 < 3s, p99 < 5s)

**Recommendation:** ship `LANA_OBSERVABILITY_v1.md` in WAVE 2.

### 10.6 Multi-modality — when does voice become the primary input?
Today Lana is text-first with voice optional. But:
- Mom is driving with kids — voice is the ONLY interface
- A meet is happening — voice + image are the inputs
- Lana could BE the household assistant (always-on home speaker?)

**Recommendation:** longer arc · in `LANA_MULTIMODAL_v1.md` WAVE 3.

### 10.7 The trust-tier eval — how do we measure trust escalation correctly?
The 5-stage ladder (Stranger → Nudge → Acquaintance → Direct → IRL) is brand-canon. But how do we measure:
- Time-to-ladder-advance per mom?
- Conversion rate per stage transition?
- Where moms drop off the ladder?
- What signals predict IRL conversion?

**Recommendation:** include in `LANA_EVAL_FRAMEWORK_v1.md` WAVE 2.

### 10.8 The "Lana doesn't know what she doesn't know" problem
What if mom asks about something Lana has no concept of (e.g., "where can I find a doula who speaks Quechua")? Today we capture it. But:
- Should Lana ADMIT she doesn't know vs. confidently mis-route?
- Should Lana ATTEMPT to learn from this turn (active learning)?
- How do we surface "Lana confidence per topic" to mom?

**Recommendation:** include in `LANA_LATENT_INTENT_ENGINE_v1.md` (delivered this batch).

### 10.9 The privacy architecture beyond RLS
Supabase RLS handles per-row access. But:
- What about differential privacy on aggregated stats?
- What about anonymization in the Signal Aggregator?
- What about GDPR/CCPA "right to be forgotten" deletion cascades?
- What about leaked credentials → blast radius limitation?

**Recommendation:** ship `LANA_PRIVACY_ARCHITECTURE_v1.md` in WAVE 2.

### 10.10 The thing nobody asks — Lana's emotional load
This is the question only a CAIO at a companion-AI startup asks: **what does Lana hear that breaks her?** Sad moms. Lonely moms. Moms with sick kids. Moms in abusive relationships.

- Does Lana have escalation paths to real human support (Tommaso? a crisis line?)?
- Does Lana mark conversations that show distress signals?
- Does Lana protect mom from her own venting (e.g., "you said you're going to do X · I worry about that")?

This is product · safety · ethics · brand · ALL at once. **Recommendation:** dedicated section in `LANA_SAFETY_v1.md` WAVE 2 · titled "Holding the weight."

---

## §11 · Deliverable plan · 10 documents

This document is #1. The other nine sequence as follows:

| # | Doc | Wave | Owner-ish |
|---|---|---|---|
| 1 | `LANA_BLUEPRINT_v1.md` (this one) | 1 (today) | Founder + CTO/CAIO |
| 2 | `LANA_LATENT_INTENT_ENGINE_v1.md` (Layer 3 spec) | 1 (today) | Yunchao + Claude |
| 3 | `LANA_RECOMMENDATION_ENGINE_v2.md` (heterogeneous · responding to Yunchao) | 1 (today) | Yunchao |
| 4 | `YUNCHAO_AI_HANDOFF_v1.md` | 1 (today) | Yunchao |
| 5 | `ASJID_BACKEND_HANDOFF_v1.md` | 1 (today) | Asjid |
| 6 | `ABDULLAH_FRONTEND_HANDOFF_v1.md` | 1 (today) | Abdullah |
| 7 | `LANA_KNOWLEDGE_GRAPH_v1.md` (heterogeneous data model) | 2 (this week) | Asjid + Yunchao |
| 8 | `LANA_EVAL_FRAMEWORK_v1.md` (golden set · per-module evals · adversarial) | 2 (this week) | Yunchao + founder |
| 9 | `LANA_SAFETY_v1.md` (adversarial robustness · trust escalation · "holding the weight") | 2 (this week) | Founder + safety lead (TBD) |
| 10 | `LANA_OBSERVABILITY_v1.md` (telemetry · dashboards · SLOs) | 3 (next week) | Asjid |

Plus three subsequent docs as the team needs them: `LANA_UNIT_ECONOMICS_v1.md` · `LANA_PRIVACY_ARCHITECTURE_v1.md` · `LANA_MULTIMODAL_v1.md`.

---

## §12 · How to use this document

**Tommaso (founder):** read end to end. Push back on any thesis. Re-prioritize the roadmap if needed. Identify what's missing in your own gut.

**Yunchao (AI):** §2 + §4 + §5 are your strategic context. Then read `YUNCHAO_AI_HANDOFF_v1.md` for week-1 priorities.

**Asjid (backend):** §3 + §4 + §10.4 + §10.5 are your strategic context. Then read `ASJID_BACKEND_HANDOFF_v1.md`.

**Abdullah (frontend):** §3 + §6 + §10.6 are your strategic context. Then read `ABDULLAH_FRONTEND_HANDOFF_v1.md`.

**Investors (when shown):** §1 (mission) + §2 (3-layer architecture) + §4 (heterogeneous graph) + §5 (flywheels) + §6 (roadmap) + §8 (moat) + §9 (vision). The IP story lives here.

---

*v1 · 2026-06-12 · Tommaso · Yunchao · Asjid · Abdullah · this is the document that makes the next 18 months coherent.*
