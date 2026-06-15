# LANA · Recommendation Engine · v2

*The heterogeneous recommender · response to Yunchao's v1 diagram · 2026-06-12*
*For: Yunchao (AI lead · author of v1) · Asjid (backend) · Tommaso (founder)*

> **Frame:** Yunchao's v1 diagram (User Context → Recall → 4 channels → Privacy Filter → Rank → Recommendation → 4 actions) is correct as a **peer-to-peer recommender**. But Lana is not a peer-to-peer recommender. She's a **heterogeneous, multi-target, intent-aware, memory-augmented concierge**. This document re-scopes the architecture to that thesis · validates the 5 gaps Yunchao flagged · and adds the 4 blind spots his linear model misses.
>
> **Bottom line:** the v1 diagram is a feature inside v2 · not the whole thing.

---

## §1 · What Yunchao got right (don't break this)

The v1 diagram's strengths:

| Strength | Why it matters |
|---|---|
| **4 recall channels** (similar people · nearby people · connectors · collaborative signals) | This is canonical hybrid recommendation (content-based + collaborative + graph-based + geographic). It maps to LinkedIn's "People You May Know" model and is solid. |
| **Privacy & Safety Filter as a layer** | Treating privacy as a filter pass (not a property of each channel) is the RIGHT architectural choice · centralizes the trust-tier enforcement |
| **Rank as a separate stage** | Decoupling recall from rank is the modern best practice (vs. one-shot retrieval) · enables independent tuning · matches YouTube, TikTok, Pinterest patterns |
| **Reason-aware recommendation** | Yunchao identified this as a gap (the privacy-safe reason builder) · the fact that he flagged it means he KNOWS recommendations need explainability · this is wise |

**These stay.** The v2 architecture builds on this skeleton · not replaces it.

---

## §2 · The 5 gaps Yunchao identified · all valid · all addressed in v2

### Gap 1 · "No general recommendation storage yet"
**Yunchao:** *Current peer_matches are computed live and returned in the response. Only `joint_moment_impressions` stores one specific onboarding card flow.*

**v2 answer:** introduce `recommendation_impressions` table (per `LANA_LATENT_INTENT_ENGINE_v1.md §6.3`). Every surfaced recommendation logs (recommendation_type · ranker_score · user_action · context). This becomes the labeled training data for the ranker.

**Why this is more than Yunchao asked for:** he framed it as "storage." We frame it as the **training-data pipeline**. Same write · different intent.

---

### Gap 2 · "No community-integration ranking layer"
**Yunchao:** *Existing matching is mostly same-block + public identity-claim embedding similarity. It does not yet rank by connector value, responsiveness, trust, activity fit, or likelihood to help a user integrate.*

**v2 answer:** the ranker becomes multi-signal, not single-similarity. Inputs:
- **Similarity** (embedding-based · what Yunchao has today)
- **Connector value** (how many tier-2+ relationships does this peer have? · graph centrality)
- **Responsiveness** (avg time-to-respond · acceptance rate)
- **Trust delta** (where is THIS peer in the requesting mom's tier ladder?)
- **Activity fit** (does this peer have signals matching the mom's active intents?)
- **Integration likelihood** (predicted P(introduction → IRL meet) from historical data)

**Architecture:** ranker is a gradient-boosted tree (XGBoost or LightGBM) on these 6 features. Initial heuristic (handcrafted weights) for v0.2 cold start · ML model from v0.3 once impressions accumulate.

---

### Gap 3 · "No reusable recommendation RPC/tool"
**Yunchao:** *There is `match_peers_by_claim_vectors`, but no dedicated `get_recommended_neighbors` / `find_recommended_neighbors` layer for Lana to call.*

**v2 answer:** introduce a unified recommendation tool with **heterogeneous targets**:

```python
def recommend(
    user_id: UUID,
    target_type: Literal['neighbor', 'meet', 'item', 'tip', 'event', 'activity'],
    context: dict,  # current session state, recent utterance, trigger
    limit: int = 3,
    trust_tier_max: Literal['stranger','nudge','acquaintance','direct','irl'] = 'stranger'
) -> List[Recommendation]:
    """
    Single tool · heterogeneous targets · trust-tier-aware · returns ranked recommendations
    with privacy-safe reason codes.
    """
```

A Recommendation object:
```python
@dataclass
class Recommendation:
    target_id: UUID
    target_type: str
    target_preview: dict       # redacted until trust earned
    score: float                # the ranker's score
    reason_codes: List[str]    # ['same_block', 'shared_brazilian_thread', 'active_connector']
    reason_text: str            # privacy-safe natural language ("a mom in your block who hosts coffees")
    surface_priority: int       # how prominent in the UI
    expiry: datetime            # recommendations rot · this is the half-life
```

This becomes Yunchao's `find_recommended_neighbors` AND generalizes to all 6 target types. **One tool · six surfaces.**

---

### Gap 4 · "No feedback/impression loop for learning"
**Yunchao:** *The system records nudges/intros after action, but not every recommendation shown, dismissed, accepted, or converted.*

**v2 answer:** **every surface logs.** The `recommendation_impressions` table records:
- The full feature vector that produced the recommendation
- Surfaced timestamp · context · ranker version
- User action (none / viewed / accepted / dismissed / converted)
- Time-to-action
- Downstream conversion (did the recommendation lead to a real-world outcome?)

This is the labeled training data. The ranker retrains nightly on this data. Per-mom personalization emerges from this loop.

**Why this is bigger than "feedback":** it's the **continuous-learning** moat. Every mom-using-Lana-day produces ~50-200 labeled examples. After 30 days × 1,000 moms, we have ~2-6M labeled examples. After 90 days × 10,000 moms, we have ~50-100M. **This is what trains Layer 3b's ranker over time.**

---

### Gap 5 · "No privacy-safe reason builder"
**Yunchao:** *Lana needs backend-provided reason codes like `same_block`, `shared_activity`, `active_connector`, so it can explain recommendations without exposing sensitive claims.*

**v2 answer:** the **reason code library** is a first-class component. Schema:

```sql
CREATE TABLE reason_codes (
  code TEXT PRIMARY KEY,
  category TEXT NOT NULL,         -- 'proximity', 'identity', 'behavior', 'history'
  privacy_tier INT NOT NULL,      -- 1 (always safe) ... 5 (only shown to verified-trust mom)
  template TEXT NOT NULL,         -- "a mom in your {block_name}"
  template_locale_pt TEXT,
  template_locale_es TEXT
);

-- Example seed data:
INSERT INTO reason_codes (code, category, privacy_tier, template) VALUES
  ('same_block', 'proximity', 1, 'a mom in your block'),
  ('same_zip', 'proximity', 1, 'a mom in your ZIP'),
  ('walking_distance', 'proximity', 1, 'a mom less than 5 min walk away'),
  ('shared_heritage', 'identity', 2, 'a mom who shares your {heritage_value} background'),
  ('shared_life_stage', 'identity', 2, 'a mom whose kids are the same {life_stage_value}'),
  ('shared_activity', 'identity', 3, 'a mom who does {activity_value} like you do'),
  ('active_host', 'behavior', 1, 'a mom who hosts {N} meets per month'),
  ('high_responder', 'behavior', 1, 'a mom who responds fast'),
  ('trusted_connector', 'behavior', 2, 'a mom who has introduced {N} other moms'),
  ('passed_swap', 'history', 1, 'a mom who passed you the {item_name}'),
  ('shared_meet', 'history', 1, 'a mom who attended {meet_name}');
```

Privacy tiers gate what reasons can be shown when. Stranger-level recommendations only use tier-1 codes. Acquaintance-level can use tier-2. Direct-level can show tier-3+.

**This is also the i18n surface.** Each reason has PT/ES variants.

---

## §3 · Yunchao's blind spots · the 4 things his linear model misses

### Blind spot 1 · Recommendations are NOT just peer-to-peer

Yunchao's diagram is a peer-to-peer recommender (find someone for someone). Reality is **heterogeneous**: mom wants to find ANY of these things:

| Target | Triggered by | Example utterance |
|---|---|---|
| **Neighbor** (person) | identity overlap · proximity · trust ladder gap | "find people like me" |
| **Meet** (event) | meet-seeker intent · ongoing meets in block · activity match | "looking for a playgroup" |
| **Item** (thing) | swap-seeker intent · item available in block | "anyone have a 3T jacket?" |
| **Tip** (knowledge) | tip-seeker intent · category match | "looking for a pediatrician" |
| **Event** (gathering) | local event · timing match | "what's happening this weekend?" |
| **Activity** (interest) | latent inference · activity in mom's claims | "my kid does karate" → 3 latent suggestions |

**The v2 recommender returns heterogeneous results.** The UI shows different cards based on `target_type`. This is the Pinterest/TikTok/Airbnb pattern · NOT the LinkedIn pattern.

### Blind spot 2 · Recommendations have THREE trigger sources · not just direct query

Yunchao's diagram triggers only on explicit query ("user context → recall"). Reality:

1. **Explicit query** (Layer 1) — mom said "find me X" → recall fires
2. **Out-of-scope query** (Layer 2) — mom said "find me Y" but we don't do Y → capture + bridge · NO recommendation fired (correct behavior · but worth noting)
3. **Latent context** (Layer 3 · the moat) — mom didn't query · but utterance/state implies a recommendation opportunity → recall fires on the inferred query

**The recommender must be called from all three trigger sources.** Same tool · different upstream callers.

### Blind spot 3 · Recommendations are TIME-AWARE · the same recommendation today vs in 6 days is different

Yunchao's diagram computes live. But:
- A recommendation surfaced at 10am may be irrelevant by 4pm (the host filled the slot · the swap got taken · the event passed)
- A recommendation surfaced today vs in 6 days has different decay scores (recency matters)
- A recommendation NOT acted on within N days should expire (`recommendation_impressions.expires_at`)

**The v2 recommender has TTLs.** Every recommendation has an `expires_at`. The `block_log_entries` table (per `BLOCK_LOG_SPEC_v1.md §4.2`) already has this. Generalize.

### Blind spot 4 · The trust ladder is BIDIRECTIONAL · not just A-to-B but A-with-B and B-with-A

Yunchao's "Privacy & Safety Filter" treats trust as a one-way attribute (does A trust B enough to see B's details?). Reality is bidirectional:
- A has tier-2 relationship with B (sent a nudge, awaiting)
- B has tier-1 with A (received nudge, hasn't responded)
- The recommendation system must show DIFFERENT things to A vs B

**The v2 trust filter is per-pair · not per-target.** Each `relationship_tier_state` row encodes both directions.

---

## §4 · The v2 architecture diagram (replaces Yunchao's v1)

```
┌────────────────────────────────────────────────────────────────────────┐
│                          TRIGGER SOURCES                                │
│                                                                         │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────────┐ │
│  │ LAYER 1         │  │ LAYER 3a        │  │ LAYER 3b                │ │
│  │ Explicit query  │  │ Flow-driven     │  │ Content-driven          │ │
│  │ "find me X"     │  │ state machine   │  │ entity → capability     │ │
│  └────────┬────────┘  └────────┬────────┘  └────────────┬────────────┘ │
└───────────┼────────────────────┼────────────────────────┼──────────────┘
            │                    │                        │
            └─────────┬──────────┴────────────────────────┘
                      │
                      ▼
            ┌─────────────────────┐
            │ recommend(          │
            │   user_id,          │
            │   target_type,      │
            │   context,          │
            │   limit,            │
            │   trust_tier_max    │
            │ )                   │
            └──────────┬──────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          RECALL (multi-source)                        │
│                                                                       │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ │
│  │ Identity     │ │ Proximity    │ │ Connector    │ │ Collaborative│ │
│  │ embedding    │ │ block / ZIP  │ │ graph        │ │ behavior     │ │
│  │ similarity   │ │ proximity    │ │ centrality   │ │ (others like │ │
│  │              │ │              │ │              │ │  you matched │ │
│  │              │ │              │ │              │ │  with this)  │ │
│  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ │
│         │                │                │                │         │
│         └────────────────┴────────────────┴────────────────┘         │
│                                 │                                     │
│                                 ▼                                     │
│                       Candidate pool (top-K)                          │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│              PRIVACY & TRUST-TIER FILTER (per-pair bidirectional)    │
│                                                                       │
│  - Stranger-level: hide names, IDs, avatars (preview cards only)      │
│  - Nudge-pending: show first name, redact location specifics          │
│  - Acquaintance: show name, neighborhood (not exact address)          │
│  - Direct: show full preview                                          │
│  - IRL: full access                                                   │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          RANKER (multi-signal)                        │
│                                                                       │
│  features = [                                                         │
│    similarity_score,                                                  │
│    proximity_score,                                                   │
│    connector_value,                                                   │
│    responsiveness,                                                    │
│    activity_fit,                                                      │
│    integration_likelihood,                                            │
│    recency_decay,                                                     │
│    block_density,                                                     │
│    per_mom_personalization_score (v0.3+)                              │
│  ]                                                                    │
│                                                                       │
│  v0.2: handcrafted weighted average                                   │
│  v0.3: gradient boosted (LightGBM) trained on impressions             │
│  v1.0+: deep ranker (transformer or two-tower)                        │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                   REASON BUILDER (privacy-safe)                       │
│                                                                       │
│  Inputs: top-3 recommendations · trust tier · privacy tier            │
│  Output: each recommendation gets reason_codes[] + reason_text         │
│                                                                       │
│  Example:                                                              │
│  - Target: Maria (peer_user_id)                                       │
│  - Reason codes: ['same_block', 'shared_brazilian_thread',            │
│                   'shared_life_stage']                                │
│  - Reason text: "a mom in your block who shares your Brazilian        │
│    heritage and whose kids are the same age"                          │
│                                                                       │
│  PT-BR locale: "uma mãe do seu bloco que compartilha sua herança      │
│  brasileira e cujos filhos têm a mesma idade"                         │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          SURFACE                                      │
│                                                                       │
│  - Block-Log (per BLOCK_LOG_SPEC_v1.md): in-app pull surface          │
│  - RADAR popover: Fellows tab + Block tab                             │
│  - Latent surface ("by the way..."): inline conversational            │
│  - Push notification (Channel 1): when threshold met                  │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│              IMPRESSION LOG (recommendation_impressions)              │
│                                                                       │
│  Every surface writes a row · ranker_score · feature_vector ·         │
│  user_action · time-to-action · downstream conversion                 │
└─────────────────────────────────┬─────────────────────────────────────┘
                                  │
                                  ▼
                       FEEDBACK FLYWHEEL →
              retrains ranker · improves capability index
```

---

## §5 · The decision matrix · 7 recommendation types · per-trigger

| Trigger | Output | Surface | Example |
|---|---|---|---|
| Layer 1 · `looking.meet` (explicit) | Meet candidates | Inline (during the looking cascade) | "Here are 3 meets that fit your toddler" |
| Layer 1 · `looking.swap` (explicit) | Item candidates | Inline | "3 moms in your block have what you're looking for" |
| Layer 1 · `looking.tip` (explicit) | Tip candidates | Inline | "Sara just shared a pediatric dentist rec" |
| Layer 1 · `discovery.find_peers` (explicit) | Neighbor candidates | RADAR · Fellows tab | "12 moms nearby · here are your top fits" |
| Layer 3a · circumstantial (flow) | Action prompt | Lana voice turn | "Time to sign up to text you when there's a fit" |
| Layer 3b · inferential (content) | Latent capability suggestions | Inline ("by the way...") | "Moms in your block with karate kids meet Saturdays" |
| Block-Log proactive | Mixed (match-results) | RADAR · Block tab | "Potential matches · 3 in your block last 7 days" |

---

## §6 · Migration plan from current state

### Phase 1 · v0.2 ship (Sept 2026)
- Asjid: ship the 8 new tables (`local_signals` · `out_of_scope_inquiry` · `block_log_entries` · `relationship_tier_state` · `latent_signals` · `suggestion_queue` · `capability_index` · `recommendation_impressions` · `reason_codes`)
- Yunchao: heuristic ranker · seed capability_index · seed reason_codes
- Yunchao: extend `match_peers_by_claim_vectors` → unified `recommend()` tool · adds 5 more target types
- Frontend (Abdullah): Block-Log surface · inline latent suggestions hidden behind feature flag

### Phase 2 · v0.3 ship (Q4 2026)
- Yunchao: train gradient-boosted ranker on first 8 weeks of impression data
- Yunchao: turn on Layer 3b suggestions to 10% of users with feature flag
- Yunchao: A/B test ranker_v1 vs ranker_v2 · roll out winner
- Asjid: real-time recommendation pipeline (sub-200ms p95)

### Phase 3 · v1.0 ship (Q2 2027)
- Yunchao: per-mom personalization layer on top of global ranker
- Yunchao: cross-block cold-start strategies (new ZIPs benefit from population-level priors)
- Asjid: capability_index admin UI (Yunchao can iterate without engineering tickets)

---

## §7 · What changes for the team

### For Yunchao
- The v1 diagram becomes a **subsection** of v2 · don't redo work · extend it
- New tools to build: `recommend(target_type=...)` generalized API · entity extractor · capability matcher
- New evals to design: recommendation precision · surface rate · per-mom personalization detection
- Read `LANA_BLUEPRINT_v1.md §2-5` for context · `LANA_LATENT_INTENT_ENGINE_v1.md` for Layer 3 architecture

### For Asjid
- 8 new tables (full schemas in `LANA_INTENTS_AND_ROUTING_v1.md §4.2` + `BLOCK_LOG_SPEC_v1.md §4.2` + `LANA_LATENT_INTENT_ENGINE_v1.md §6`)
- `recommend()` RPC endpoint · target_type-aware
- Impression logging pipeline (high-volume write path)
- Reason code library + i18n table

### For Abdullah
- The Block-Log UI surface (per `BLOCK_LOG_SPEC_v1.md §3`)
- Render heterogeneous recommendation cards (different layouts per `target_type`)
- Render latent suggestions inline as "by the way" Lana voice turns
- Trust-tier-aware UI gating (some content visible only at certain tiers)

---

## §8 · Open questions

1. **Yunchao:** v0.2 ranker — fully handcrafted heuristic OR do we ship LightGBM from day 1 with synthetic data + cold-start priors?
2. **Yunchao:** capability_index seeding — manual (23 capabilities) or auto-generate descriptions from intent docs?
3. **Asjid:** can the recommendation pipeline be sub-200ms p95 at v0.2 scale (50 daily moms)? Or do we cache aggressively?
4. **Tommaso:** should recommendation cards show the ranker score? (Transparent · investor-friendly · maybe confusing to mom)
5. **All:** when does the feature flag turn on for Layer 3b surface? Need explicit go/no-go criteria.

---

*v2 · 2026-06-12 · authored as a response to Yunchao's v1 diagram and 5-gap list · supersedes the linear peer-to-peer model · v1 stays as the historical reference.*
