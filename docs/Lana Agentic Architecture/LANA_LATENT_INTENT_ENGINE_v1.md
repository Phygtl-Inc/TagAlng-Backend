# LANA · Latent Intent Engine · v1

*The Layer 3 architecture · the moat layer · 2026-06-12*
*Spec for: Yunchao (AI lead) · Asjid (backend) · Tommaso (founder review)*

> **Why this exists:** Layer 1 (explicit fulfillment) and Layer 2 (explicit out-of-scope capture) are what every competent chatbot does. **Layer 3 is what separates a chatbot from a concierge** — it's the engine that infers intent from context and surfaces value without being asked.
>
> **The thesis:** mom mentions "my kid does karate" — she didn't ask for anything. A chatbot saves the identity claim and moves on. A concierge surfaces three things she would have asked for if she'd known to. This document specifies how Lana does that.
>
> **State of the art reference:** Letta (formerly MemGPT) memory architecture · Sierra "constellation of models" · Anthropic Claude with tool use + retrieval · "Proactive Conversational Agents with Anticipatory Intent" (Lin et al · 2024) · Decagon's proactive customer success patterns.

---

## §1 · The gap this fills

The current Lana architecture has two intent layers:

1. **Layer 1** · explicit intent → tool call (e.g., "find me 3T rain boots" → `looking.swap`)
2. **Layer 2** · out-of-scope intent → graceful capture (e.g., "I need a nail tech" → `capture_inquiry`)

There is **no system today** for the third case:

3. **Layer 3** · the mom didn't ask for anything, but her utterance carries signals that map to capabilities she might find valuable

Example utterances Layer 1 + 2 handle poorly:
- *"My kid does karate"* — Layer 1 saves identity.add_claim · system moves on · misses 3 latent opportunities
- *"We're new to Lake Nona"* — Layer 1 saves "new to area" · misses welcome-meet · neighborhood orientation · housing tips
- *"I'm exhausted, kid was up all night"* — Layer 1 treats as companionship turn · misses sleep-tip thread · pediatrician-rec opportunity · empathy-driven nudge to mom who shared similar
- *"We celebrated his birthday last weekend"* — Layer 1 saves "has young child" · misses birthday-rituals thread · gift-swap opportunity

In **every case**, mom didn't explicitly ask · the latent value is invisible to Layer 1 + 2.

The cost of NOT having Layer 3: each turn that contains a latent intent is a missed conversion · an engagement gap · a moat that doesn't compound.

---

## §2 · The two sub-layers · 3a and 3b

Latent intent has two fundamentally different trigger mechanisms. They share infrastructure but require distinct architectures.

### 2.1 · Layer 3a · Circumstantial latent intent (flow-driven)
**Definition:** intent that emerges from the user's **position in a flow** + accumulated context.

**Trigger:** rule engine on the state machine + counters/predicates.

**Examples:**
- *During FTUE, mom has expressed a looking intent + 2 turns of value provided + no phone verified yet* → state rule fires: time to ask for phone
- *7 days since mom's last active session* → rule fires: time for proactive check-in
- *Mom completed her first nudge → recipient accepted → 0 days post-IRL-meet* → rule fires: time to suggest the next interaction
- *Mom's 4th identity claim added · only 1 dimension remaining* → rule fires: nudge to complete profile

**Architectural pattern:** deterministic finite state machine (XState) with conditional edges that fire on guards. Same pattern as `AI_PIPELINE_v1.md §3` but enriched with multi-turn counters.

**Frameworks:**
- **XState** — what we already use · canonical FSM library
- **LangGraph** — LangChain's graph orchestrator · conditional edges fire on predicates
- **Sierra's step-tracker** — proprietary but well-documented architecture

**Why it's CRITICAL for v0.1:** the FTUE signup chime-in (C-SIGNUP-1 frame triggered automatically) is a Layer 3a moment we already designed in the mockup. We just haven't named it. Many of the canonical UX moments are Layer 3a in disguise.

### 2.2 · Layer 3b · Inferential latent intent (content-driven)
**Definition:** intent that emerges from **the content of what mom said** mapping to capabilities she didn't invoke.

**Trigger:** entity extraction + capability matching + suggestion ranking.

**Examples:**
- *"My kid does karate"* → entity:karate(type:activity) → match {looking.meet · looking.swap · looking.tip}
- *"We're new to Lake Nona"* → entity:Lake Nona(type:place), claim:new-to-area → match {looking.tip(welcome-rec), looking.meet(welcome-coffee)}
- *"My husband works long hours"* → claim:single-parenting-shift → match {looking.meet(evening-mom-meet)}

**Architectural pattern:** every turn runs entity extraction *in parallel* with intent classification. Entities are queried against a capability index (each capability has a vector embedding). Matches above threshold are scored, ranked, filtered through privacy/trust gates, and queued for timing-aware delivery.

**Frameworks:**
- **Letta (formerly MemGPT)** — memory-augmented agents · the "archival recall" pattern fits Layer 3b exactly
- **Anthropic Claude with retrieval-augmented tool selection** — the architecture used in Claude's computer-use feature
- **Sierra "constellation of models"** — Haiku for extraction + Sonnet for ranking
- **Decagon · proactive customer success** — recommendation-by-context patterns

**Why it's NOT in v0.1:** we need real conversational data to train the entity extractor + tune the ranker. We scaffold the architecture · train and ship in v0.3.

---

## §3 · The detection pipeline

```
USER UTTERANCE
     │
     ▼
┌─────────────────────────────┐
│ PARALLEL EXTRACTION (every turn) │
│                              │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐
│  │ Intent      │  │ Entity      │  │ Memory recall    │
│  │ classifier  │  │ extractor   │  │ (Letta archival) │
│  │ (Haiku)     │  │ (Haiku)     │  │                  │
│  │             │  │             │  │ Pulls relevant   │
│  │ → L1 / L2   │  │ → entities[] │  │ past turns       │
│  │             │  │  with types  │  │ that match       │
│  └─────────────┘  └─────────────┘  └──────────────────┘
└───────┬───────────────┬───────────────────┬──────────┘
        │ in-scope?     │ entities          │ historical
        ▼               ▼                   │ context
┌──────────────────┐    │                   │
│ L1 fulfill OR    │    │                   │
│ L2 capture       │    │                   │
└──────────────────┘    │                   │
                        ▼                   ▼
                  ┌──────────────────────────────┐
                  │ LAYER 3 LATENT INTENT ENGINE │
                  │                              │
                  │  3a Circumstantial:          │
                  │  - state machine evaluator   │
                  │  - flow + counter rules      │
                  │                              │
                  │  3b Inferential:             │
                  │  - entity → capability_index │
                  │  - cosine match · threshold  │
                  │  - score + rank              │
                  │                              │
                  │  COMBINED:                   │
                  │  - merge 3a + 3b candidates  │
                  │  - apply frequency caps      │
                  │  - apply trust-tier filter   │
                  │  - timing engine: when to    │
                  │    surface (now/next-break/  │
                  │    next-session)             │
                  └──────────┬───────────────────┘
                             │
                             ▼
                  ┌──────────────────────────────┐
                  │ SUGGESTION QUEUE             │
                  │                              │
                  │ {                            │
                  │   suggestion_id: uuid,       │
                  │   user_id: uuid,             │
                  │   trigger_layer: '3a'|'3b',  │
                  │   trigger_context: jsonb,    │
                  │   capability: text,          │
                  │   confidence: real,          │
                  │   surface_at: 'now'|'next'   │
                  │     |'next_session',         │
                  │   surfaced_at: null,         │
                  │   action_taken: null,        │
                  │ }                            │
                  └──────────┬───────────────────┘
                             │ at the right moment
                             ▼
                  ┌──────────────────────────────┐
                  │ LANA SURFACES                │
                  │                              │
                  │ "By the way · 3 moms in your │
                  │  block have karate kids and  │
                  │  meet Saturdays. Want me to  │
                  │  find you a fit?"            │
                  └──────────┬───────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼              ▼              ▼
         accepted         dismissed       ignored
              │              │              │
              └──────────────┴──────────────┘
                             │
                             ▼
                  recommendation_impressions
                  (feedback loop → ranker training)
```

---

## §4 · The capability index

The capability index is a queryable store of "what Lana CAN help with." Every in-scope intent (from `LANA_INTENTS_AND_ROUTING_v1.md §2`) has a record here.

### 4.1 · Schema (proposed)

```sql
CREATE TABLE capability_index (
  capability_id TEXT PRIMARY KEY,          -- e.g., 'looking.meet'
  capability_name TEXT NOT NULL,           -- 'Find a meet or playgroup'
  description TEXT NOT NULL,               -- semantic description used for embedding
  embedding VECTOR(1536) NOT NULL,         -- OpenAI text-embedding-3-large
  entity_triggers TEXT[],                  -- e.g., ['activity', 'sport', 'hobby']
  identity_claim_triggers TEXT[],          -- e.g., ['has_school_age_kid']
  required_state TEXT[],                   -- e.g., ['phone_verified', 'in_block']
  surface_priority INT DEFAULT 5,          -- 1-10, higher = more prominent
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_capability_embedding ON capability_index USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_capability_entity_triggers ON capability_index USING GIN (entity_triggers);
```

### 4.2 · Example rows

```
capability_id: 'looking.meet'
description: "Find a mom-with-similar-kids meet · playgroup · activity group"
embedding: [...]
entity_triggers: ['activity', 'sport', 'hobby', 'class', 'lesson']
identity_claim_triggers: ['has_kid', 'life_stage:pre-K|elementary|toddler']
required_state: []
surface_priority: 8

capability_id: 'looking.swap'
description: "Find someone in your block willing to swap kids' gear, clothes, equipment"
entity_triggers: ['gear', 'equipment', 'clothes', 'item', 'outgrew', 'size']
identity_claim_triggers: ['has_kid']
required_state: []
surface_priority: 7

capability_id: 'looking.tip'
description: "Find a mom-tested recommendation for a service, professional, or place"
entity_triggers: ['recommendation', 'dentist', 'doctor', 'tutor', 'gym', 'restaurant']
identity_claim_triggers: []
required_state: []
surface_priority: 6
```

### 4.3 · The match algorithm

Given an utterance:
```python
def match_latent_capabilities(utterance, user_context):
    # Step 1: extract entities (Haiku call)
    entities = entity_extractor(utterance)
    # → [{text: 'karate', type: 'activity', confidence: 0.95}, ...]
    
    # Step 2: embed each entity
    entity_embeddings = [embed(e.text) for e in entities]
    
    # Step 3: for each entity, find top-k capability matches
    candidates = []
    for entity, emb in zip(entities, entity_embeddings):
        matches = capability_index.query(
            embedding_similarity=emb,
            entity_type_filter=entity.type,
            limit=5,
            min_score=0.65
        )
        candidates.extend(matches)
    
    # Step 4: filter by user_context (required_state, identity_claims)
    valid = [c for c in candidates if user_meets_requirements(c, user_context)]
    
    # Step 5: enrich each with block-density signal
    for c in valid:
        c.block_density_score = count_active_signals_in_block(c.capability_id, user_context.block_id)
    
    # Step 6: combined score = embedding_similarity × surface_priority × block_density
    scored = [(c, c.embedding_score * c.surface_priority * (1 + log(c.block_density_score))) for c in valid]
    
    return sorted(scored, key=lambda x: -x[1])[:3]  # top 3
```

---

## §5 · The timing engine

Surfacing a suggestion at the wrong moment is worse than not surfacing at all. The timing engine decides WHEN to deliver.

### 5.1 · Surface timing rules

| Trigger | Surface when |
|---|---|
| User mid-flow (e.g., signup step) | NEVER mid-flow · queue for after flow completes |
| User just finished a task | At the END of the current Lana turn ("by the way ...") |
| User just opened the app | At session start, if a queued suggestion is high-priority |
| User is in a chat with another mom | NEVER · don't interrupt the human-to-human moment |
| User is venting / emotional | NEVER until emotional resolution detected |
| User asked a follow-up question | Hold the suggestion · prioritize the response · re-queue |

### 5.2 · Frequency caps

Per `BLOCK_LOG_SPEC_v1.md §6` channel-aware caps · plus latent-specific:
- Max **2 latent suggestions per session** (cumulative across 3a + 3b)
- Max **5 latent suggestions per week** (per mom)
- Hard reset on user-initiated dismissal of a category ("not interested in karate-related") for 30 days
- Cool-down 24h on a same-capability suggestion if dismissed

### 5.3 · Position in turn

```
LANA TURN:
  1. Acknowledge user's last message (1-2 sentences)
  2. Fulfill explicit intent (Layer 1 tool call OR Layer 2 capture)
  3. [OPTIONAL] Layer 3 latent suggestion · ONLY if:
     - Suggestion queue has high-confidence item (score > 0.75)
     - This turn isn't emotional
     - User hasn't received N suggestions today
     - The suggestion makes sense BEFORE next user turn
  4. Soft close (open-ended invitation to continue)
```

---

## §6 · Data model · what Asjid needs to ship

### 6.1 · `latent_signals` table
Every entity extracted in every turn lands here. Independent of intent classification.

```sql
CREATE TABLE latent_signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  session_id UUID REFERENCES lana_sessions(id) NOT NULL,
  turn_id UUID REFERENCES lana_messages(id) NOT NULL,
  utterance_excerpt TEXT NOT NULL,           -- the substring the entity came from
  entity_text TEXT NOT NULL,                  -- 'karate'
  entity_type TEXT NOT NULL,                  -- 'activity'
  entity_confidence REAL NOT NULL,
  embedding VECTOR(1536) NOT NULL,            -- entity embedding
  extracted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_latent_signals_user_time ON latent_signals(user_id, extracted_at DESC);
CREATE INDEX idx_latent_signals_entity_type ON latent_signals(entity_type, extracted_at DESC);
CREATE INDEX idx_latent_signals_embedding ON latent_signals USING ivfflat (embedding vector_cosine_ops);
```

### 6.2 · `suggestion_queue` table
The Layer 3 output buffer · timing-aware.

```sql
CREATE TABLE suggestion_queue (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  trigger_layer TEXT NOT NULL CHECK (trigger_layer IN ('3a','3b')),
  trigger_context JSONB NOT NULL,              -- e.g., {'entity': 'karate', 'utterance': '...'}
  capability_id TEXT REFERENCES capability_index(capability_id),
  suggestion_text TEXT,                        -- the Lana voice line · pre-templated
  confidence REAL NOT NULL,
  surface_when TEXT NOT NULL CHECK (surface_when IN ('now','end_of_turn','next_break','next_session')),
  expires_at TIMESTAMPTZ,                      -- suggestions expire if not surfaced in time
  surfaced_at TIMESTAMPTZ,                     -- null if not yet surfaced
  user_action TEXT CHECK (user_action IN ('accepted','dismissed','ignored','converted')),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_suggestion_queue_user_unsurfaced ON suggestion_queue(user_id, created_at DESC)
  WHERE surfaced_at IS NULL AND expires_at > NOW();
```

### 6.3 · `recommendation_impressions` table
Every Layer 3 surface logged · the labeled training data for the ranker.

```sql
CREATE TABLE recommendation_impressions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  suggestion_id UUID REFERENCES suggestion_queue(id),
  surfaced_at TIMESTAMPTZ NOT NULL,
  user_action TEXT,                            -- 'accepted','dismissed','ignored','converted'
  action_at TIMESTAMPTZ,
  context JSONB,                               -- session state · trust tier · time-of-day · etc.
  ranker_version TEXT,                         -- which version of the ranker produced this
  ranker_score REAL,                           -- the score the ranker assigned
  feature_vector VECTOR(64)                    -- inputs to the ranker for replay/training
);
```

---

## §7 · The training loop

Layer 3b's ranker improves with usage. The loop:

1. **Collect impressions** — every surfaced suggestion logs to `recommendation_impressions` with the ranker's feature vector
2. **Label** — user action becomes the label (accepted=positive · dismissed=negative · ignored=weak negative · converted=strong positive)
3. **Retrain** — weekly batch job re-trains the ranker (gradient boosted model on feature vector → action)
4. **Deploy** — new ranker version goes to production via feature flag
5. **A/B** — compare old vs new ranker · measure acceptance rate · roll back if regression

This is **continuous learning**. The system gets better with every mom who uses it. **Investors love this slide.**

---

## §8 · Evaluation framework for Layer 3

A new layer needs a new eval. You can't measure proactive intelligence with traditional intent-classification accuracy.

### 8.1 · The Layer 3 eval set
- 200 turns where the mom did NOT explicitly ask, but a latent opportunity exists
- 100 turns where the mom did NOT explicitly ask AND nothing should be surfaced (negative cases — emotional venting, simple chitchat)
- Each turn labeled with: ideal suggestion (if any), expected timing, expected confidence band

### 8.2 · Metrics
- **Suggestion precision** — of N suggestions surfaced, how many were accepted/converted?
- **Surface rate** — what % of opportunity turns got a suggestion?
- **False positive rate** — what % of "should not suggest" turns DID get a suggestion?
- **Timing correctness** — were suggestions delivered at the right moment?
- **Per-mom acceptance** — does the ranker personalize per mom over time?

### 8.3 · Targets (v0.3)
- Suggestion precision ≥ 65%
- Surface rate ≥ 40% on opportunity turns
- False positive rate ≤ 5%
- Timing correctness ≥ 90%
- Per-mom personalization detectable after 4 weeks of usage

---

## §9 · Implementation roadmap

### Phase 1 · Scaffolding (v0.2 · Sept 2026)
- Build `latent_signals` table · entity extractor runs in production · we COLLECT but don't surface
- Build `capability_index` · seed with the 23 capabilities · embed descriptions
- Build `suggestion_queue` · timing engine logic implemented · queue populated · we DON'T surface yet
- 8 weeks of background data collection
- **Why don't surface yet:** we don't have enough labeled data to confidently rank

### Phase 2 · Surface (v0.3 · Q4 2026)
- Turn on Layer 3b surface with feature flag (10% of users)
- Compare control vs treatment on retention + engagement
- Iterate ranker · expand capability index
- Roll out to 100% if metrics positive

### Phase 3 · Personalize (v0.3.5 · Q1 2027)
- Train per-mom personalization layer on top of global ranker
- Add Layer 3a circumstantial extensions (re-engagement nudges · check-ins · milestone celebrations)
- Multi-language coverage (PT-BR + ES production-grade)

### Phase 4 · Proactive (v1.0 · Q2 2027)
- Lana initiates conversations (not waiting for mom to open app)
- Per-week proactive nudges based on Layer 3a + 3b
- Push notification integration with Layer 3 suggestion queue
- Anti-spam guardrails (frequency caps · personalization preference)

---

## §10 · Open questions for the team

1. **Yunchao:** which entity extraction model · Haiku in-house · or a smaller dedicated NER model (spaCy · BERT-NER · prebuilt)?
2. **Yunchao:** initial ranker — heuristic (handcrafted feature weighting) or gradient boosted from day one (small initial training data)?
3. **Asjid:** do `latent_signals` + `suggestion_queue` + `recommendation_impressions` need real-time write paths, or can we batch every N seconds?
4. **Asjid:** capability_index updates — admin UI? config file? GitOps? Where does Yunchao iterate the index without engineering tickets?
5. **Tommaso:** what's the BRAND VOICE rule for proactive suggestions? Are they "by the way..." (light, optional) or "I noticed..." (observant, intimate) or "may I..." (formal, deferential)?
6. **Tommaso:** what's the DEFAULT frequency cap? Conservative (1/week) or aggressive (3/day)?
7. **Tommaso:** ethically — should mom EXPLICITLY consent to Layer 3 ("Lana, you can suggest things") OR opt-out ("Lana suggests by default · turn off in settings")?

---

## §11 · Why this is the moat

Anyone can build Layer 1 with off-the-shelf LLM + function-calling in 2 weeks. Layer 2 is a 4-week build. Layer 3 is:

- **6 months of architecture** (capability index · entity extraction · timing engine · ranker)
- **12 months of data collection** to train the ranker meaningfully
- **Per-block specificity** — a generic Layer 3 isn't useful · we need PER-MOM, PER-BLOCK personalization that only emerges from real usage

The first competitor to ship Layer 3 wins the local-agentic-AI category. We have the architecture ready · the data collection plan ready · the evaluation framework ready. **We just need to execute.**

---

*v1 · 2026-06-12 · Yunchao please review · Asjid please confirm schema · Tommaso please answer §10 questions before week-1 kickoff*
