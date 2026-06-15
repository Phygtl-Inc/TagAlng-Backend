# Asjid · Backend lead · handoff · v1

*Your tour of the v2 architecture · what you own · what to ship · 2026-06-12*

> Asjid · you've shipped Layer 1 backend (`lana-worker` · discovery routing · unified chat · `ui_intent` · auth handoff). v2 expands the backend along 4 axes: **(1) heterogeneous data substrate**, **(2) impression + feedback pipeline**, **(3) Layer 3 latent intent infrastructure**, **(4) notification dispatch + privacy architecture**. This document tells you exactly what's yours and the order to ship.

---

## §1 · What you own

| Area | Scope |
|---|---|
| **Supabase schema** | All tables · indexes · RLS policies · migrations · backups |
| **`lana-worker` Cloud Run service** | The orchestrator · tool execution · prompt assembly · response shaping |
| **API contracts** | Every endpoint Lana FE consumes · the unified-chat contract per `LANA_UNIFIED_DISCOVERY_FRONTEND.md` |
| **Auth + identity** | Supabase auth integration · anon → signed-up migration · trust tier enforcement at row level |
| **Notification dispatch** | Twilio SMS · Expo/APNS push · Resend email (key just provided by founder) |
| **Signal aggregation pipeline** | The out-of-scope inquiry clusterer · the materialized views · the threshold detector |
| **Recommendation pipeline** | The recall layer · the candidate generator · the ranker host (Yunchao trains, you deploy) |
| **Telemetry + observability** | Structured turn logs · failure dashboards · SLO monitoring |
| **Privacy + RLS** | Per-row access · trust-tier-gated visibility · GDPR/CCPA deletion paths |

---

## §2 · What you DON'T own

| Area | Who owns |
|---|---|
| Model selection · prompt content · evals | Yunchao |
| Frontend code · UX | Abdullah |
| Product spec · cohort selection · brand voice | Tommaso |

You DO own all data + infrastructure decisions. If Yunchao asks for a schema, you push back if it's wrong (and propose alternative). Same for Tommaso — if he asks for a feature that requires a 10-table migration with 2-week downtime, you flag it.

---

## §3 · Week-1 priorities (June 13-19 · pre-Day-Zero June 15)

### Critical · must ship for Day Zero
1. **Apply the schema additions** for v0.2 launch (DDL ready in docs):
   - `local_signals` (per `LANA_INTENTS_AND_ROUTING_v1.md §4.2`)
   - `out_of_scope_inquiry` (same doc · same section)
   - `relationship_tier_state` (same)
   - `block_log_entries` (per `BLOCK_LOG_SPEC_v1.md §4.2`)
   - `reason_codes` (per `LANA_RECOMMENDATION_ENGINE_v2.md §2 Gap 5`)
2. **Wire `capture_inquiry` as orchestrator fallback** · when Layer 1 intent classifier returns confidence < 0.85 OR no in-scope match · auto-invoke this tool · writes to `out_of_scope_inquiry`
3. **Notification dispatch infrastructure**:
   - Twilio SMS integration (you may already have)
   - Expo push for iOS · APNS configured
   - Resend email integration (founder provided key · add to Vercel env · DO NOT commit)
   - `match_notifications` table to log dispatches
4. **API endpoint** `POST /lana/match/notify` triggered when matcher creates a `block_log_entries` row · routes to right channel per `BLOCK_LOG_SPEC_v1.md §6`
5. **RLS policies** on the new tables · per the privacy-tier matrix (every read goes through trust-tier-aware policy)

**Total: ~16-20 hours · realistic for week-1 if focused**

### High priority · ship in v0.2 (Sept 2026)
6. `latent_signals` table + write pipeline (Yunchao populates from worker)
7. `suggestion_queue` table + read API for FE to pull pending suggestions
8. `recommendation_impressions` table + write pipeline (every surface logs)
9. `capability_index` table (Yunchao populates · you provide admin path)
10. Signal Aggregator pipeline (per `LANA_SIGNAL_AGGREGATOR.md`) — nightly cron clusters captures into emerging categories

---

## §4 · Week-2-to-Month-1 priorities

### Heterogeneous recommendation pipeline
1. **Recall service** — given (user_id, target_type, context), return top-K candidate set with multi-source signals:
   - Identity embedding similarity (you already do this via `match_peers_by_claim_vectors`)
   - Block proximity (use existing block/ZIP infrastructure)
   - Connector centrality (graph query · count of acquaintance+ relationships)
   - Collaborative similarity ("moms similar to you matched with...")
2. **Privacy + trust-tier filter** — bidirectional per-pair check (does A trust B at level X · does B trust A at level Y)
3. **Ranker host** — Yunchao trains the ranker · you deploy it as a Cloud Run service · keep latency < 100ms p95
4. **Reason builder service** — given a (target, requester, trust_tier) tuple · return `reason_codes[]` + `reason_text` (localized)
5. **Recommendation impression logger** — every surfaced recommendation writes a row · high-volume write path · batch every 1-5s

### Model abstraction layer (CTO/CAIO concern · §10.4 of Blueprint)
6. **Model proxy** — single entry point for all LLM calls · routes to Hermes / Claude / GPT / Gemini based on per-call config · enables vendor swap without touching worker code
7. **Fallback chain** — if primary model is unavailable, fall back to secondary · retries with exponential backoff · logs failures

### Observability (CTO/CAIO concern · §10.5 of Blueprint)
8. **Structured turn logging** — every Lana turn logs: timestamp · user_id · session_id · turn_text · intent · confidence · tools_called · model_used · latency_ms · output_text
9. **Failure dashboard** — Grafana / Metabase view on top of turn logs · alert when error rate > 1%
10. **Latency SLOs** — p50 < 1s · p95 < 3s · p99 < 5s · alert when SLOs breached
11. **"Lana said something weird" queue** — user-flagged turns surface in a review queue · admin UI for triage

---

## §5 · Month-2-to-Quarter priorities (v0.2 → v0.3)

### Real-time recommendation pipeline
1. **Sub-200ms p95 recommendation latency** — caching layer · pre-computed candidate pools per block · async impression logging
2. **Event-driven match creation** — when a new `local_signal` is inserted, trigger a matcher job that scores against active signals in the same block, writes `block_log_entries` rows, fires notifications

### Privacy architecture (§10.9 of Blueprint)
3. **GDPR/CCPA deletion cascades** — when mom deletes her account, all her signals · claims · impressions · messages are surgically removed · downstream materialized views recomputed
4. **Differential privacy on aggregates** — the Signal Aggregator publishes per-block stats · ensure no individual is reidentifiable
5. **Anonymization in research data** — when we share insights externally, ensure k-anonymity ≥ 5

### Federated learning prep (v1.0+ · §9 of Blueprint)
6. **Per-ZIP data isolation** — design schema so we CAN train per-ZIP rankers later without re-architecting
7. **Embedding privacy** — ensure identity claim embeddings don't leak claim text reverse-engineering paths

---

## §6 · The schema migration plan · in dependency order

These are the tables you ship between week 1 and quarter 1. Order matters because some depend on others.

### Tier 1 · week 1 (ship for Day Zero)

```sql
-- §6.1 · local_signals · what mom asks/offers/hosts
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
  status TEXT DEFAULT 'listening',
  source_surface TEXT DEFAULT 'lana',
  embedding VECTOR(1536),
  contact_permission BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);
CREATE INDEX idx_local_signals_block_intent ON local_signals(block_id, intent) WHERE status = 'listening';
CREATE INDEX idx_local_signals_embedding ON local_signals USING ivfflat (embedding vector_cosine_ops);

-- §6.2 · out_of_scope_inquiry · the roadmap-from-data pipeline
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

-- §6.3 · relationship_tier_state · the 5-stage ladder
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

-- §6.4 · block_log_entries · the missing match-log surface
CREATE TABLE block_log_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  for_user_id UUID REFERENCES users(id) NOT NULL,
  match_type TEXT NOT NULL CHECK (match_type IN (
    'inbound_for_my_seek','inbound_for_my_offer',
    'meet_invite_potential','meet_attendee_potential',
    'fellow_overlap_high','tip_match'
  )),
  my_signal_id UUID REFERENCES local_signals(id),
  peer_signal_id UUID REFERENCES local_signals(id),
  peer_user_id UUID REFERENCES users(id),
  block_id UUID REFERENCES blocks(id) NOT NULL,
  match_strength REAL NOT NULL,
  match_reasons TEXT[],
  created_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  user_acted_at TIMESTAMPTZ,
  action_taken TEXT,
  notification_sent_to_peer BOOLEAN DEFAULT FALSE,
  notification_sent_at TIMESTAMPTZ
);
CREATE INDEX idx_block_log_user_active ON block_log_entries(for_user_id, created_at DESC)
  WHERE action_taken IS NULL AND expires_at > NOW();

-- §6.5 · reason_codes · privacy-safe explanation library
CREATE TABLE reason_codes (
  code TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  privacy_tier INT NOT NULL,
  template TEXT NOT NULL,
  template_locale_pt TEXT,
  template_locale_es TEXT
);
```

### Tier 2 · v0.2 (Sept 2026)

```sql
-- §6.6 · latent_signals · entity extraction per turn
CREATE TABLE latent_signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  session_id UUID REFERENCES lana_sessions(id) NOT NULL,
  turn_id UUID REFERENCES lana_messages(id) NOT NULL,
  utterance_excerpt TEXT NOT NULL,
  entity_text TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_confidence REAL NOT NULL,
  embedding VECTOR(1536) NOT NULL,
  extracted_at TIMESTAMPTZ DEFAULT NOW()
);

-- §6.7 · suggestion_queue · Layer 3 output buffer
CREATE TABLE suggestion_queue (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  trigger_layer TEXT NOT NULL CHECK (trigger_layer IN ('3a','3b')),
  trigger_context JSONB NOT NULL,
  capability_id TEXT,
  suggestion_text TEXT,
  confidence REAL NOT NULL,
  surface_when TEXT NOT NULL,
  expires_at TIMESTAMPTZ,
  surfaced_at TIMESTAMPTZ,
  user_action TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- §6.8 · capability_index · what Lana CAN help with
CREATE TABLE capability_index (
  capability_id TEXT PRIMARY KEY,
  capability_name TEXT NOT NULL,
  description TEXT NOT NULL,
  embedding VECTOR(1536) NOT NULL,
  entity_triggers TEXT[],
  identity_claim_triggers TEXT[],
  required_state TEXT[],
  surface_priority INT DEFAULT 5,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- §6.9 · recommendation_impressions · the feedback flywheel
CREATE TABLE recommendation_impressions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id) NOT NULL,
  recommendation_type TEXT NOT NULL,
  target_id UUID,
  target_type TEXT NOT NULL,
  surfaced_at TIMESTAMPTZ NOT NULL,
  user_action TEXT,
  action_at TIMESTAMPTZ,
  context JSONB,
  ranker_version TEXT,
  ranker_score REAL,
  feature_vector VECTOR(64)
);
```

### Tier 3 · v0.3+ (Q4 2026)

10. Materialized views on `out_of_scope_inquiry` for the Signal Aggregator (per `LANA_SIGNAL_AGGREGATOR.md §3`)
11. Materialized views on `recommendation_impressions` for ranker training data
12. Audit log table for admin actions (capability_index edits · reason_codes edits)

---

## §7 · API contracts you ship

### v0.2 ship

**1. `POST /lana/sessions/{id}/messages`** — already shipped per `LANA_UNIFIED_DISCOVERY_FRONTEND.md` · enrich response with new fields:
```json
{
  "assistant_message": "...",
  "ui_intent": "...",
  "active_intent": "...",
  "routing_phase": "...",
  "auth_action": null,
  "peer_matches": [],
  "activity_previews": [],
  "recommendations": [],          // NEW v0.2 · heterogeneous
  "latent_suggestion": null,      // NEW v0.2 · 3a or 3b output if surfacing
  "phone_verified": false,
  "home_block_assigned": false
}
```

**2. `GET /lana/users/{user_id}/block-log`** (per `BLOCK_LOG_SPEC_v1.md §4.3`) — pulls `block_log_entries` for mom's RADAR Block tab

**3. `POST /lana/block-log/{entry_id}/action`** — mom dismisses / saves / nudges from the Block-Log

**4. `POST /lana/match/notify`** — internal · triggered by matcher · dispatches Channel 1 push/SMS

**5. `GET /lana/recommendations?type=...&limit=...`** — heterogeneous recommendation endpoint per `LANA_RECOMMENDATION_ENGINE_v2.md §2 Gap 3`

**6. `POST /lana/recommendations/{id}/impression`** — logs an impression (surfaced · viewed · dismissed · accepted · converted)

---

## §8 · The model abstraction layer (§10.4 of Blueprint)

You need a thin proxy so Yunchao can swap models without touching `lana-worker` business logic.

```python
# services/lana-worker/app/model_proxy.py
class ModelProxy:
    def __init__(self):
        self.providers = {
            'claude-sonnet-4': AnthropicClient(),
            'claude-haiku-4': AnthropicClient(),
            'hermes-4': NousClient(),
            'gpt-4o': OpenAIClient(),
            'gemini-2-flash': GeminiClient(),
        }
        self.fallback_chain = ['claude-sonnet-4', 'gpt-4o', 'hermes-4']
    
    async def complete(self, prompt, model='claude-sonnet-4', schema=None, **kwargs):
        """Single entry point · routes to provider · handles retries."""
        try:
            return await self.providers[model].complete(prompt, schema, **kwargs)
        except (ProviderUnavailable, RateLimited) as e:
            # Fall back to next in chain
            for fallback in self.fallback_chain:
                if fallback != model:
                    return await self.providers[fallback].complete(prompt, schema, **kwargs)
            raise
```

All LLM calls in `lana-worker` go through this proxy. Yunchao picks the model per call type · you don't change code.

---

## §9 · Observability stack (§10.5 of Blueprint)

### Logging
- Structured JSON logs per turn (Cloud Logging on GCP)
- Fields: timestamp · user_id (hashed) · session_id · turn_text (redacted PII) · intent · confidence · tools_called[] · model_used · latency_ms · response_size · error
- Retention: 90 days hot · 1 year cold

### Metrics (Prometheus / Cloud Monitoring)
- Turns per second
- Latency histograms (p50 · p95 · p99) by endpoint
- Error rate by endpoint · by intent
- Model call costs (sum of token costs per day)
- Notification delivery success rate

### Alerts
- Error rate > 1% over 5 min → PagerDuty
- p95 latency > 3s over 5 min → Slack
- Notification delivery success < 95% → Slack
- Tool call failure rate > 5% → Slack

### Dashboards
- Grafana / Metabase: per-intent acceptance rate · per-mom engagement · capture-to-roadmap conversion · ranker performance

---

## §10 · Cost model concerns (§10.3 of Blueprint)

Estimate marginal cost per mom per month at scale (1,000 active moms, 5 turns/day avg):

| Item | Per turn cost | Per mom/month | At 1,000 moms |
|---|---|---|---|
| Claude Sonnet 4 router (300 tokens in, 100 out) | $0.0009 | $0.135 | $135/mo |
| Claude Haiku synthesizer (1k in, 200 out) | $0.0006 | $0.090 | $90/mo |
| OpenAI embeddings (per signal) | $0.0001 | $0.005 | $5/mo |
| Twilio SMS (avg 2/mom/week) | $0.01 | $0.080 | $80/mo |
| Push notifications | $0 | $0 | $0 |
| Supabase storage + reads | (variable) | ~$0.20 | $200/mo |
| Cloud Run inference + worker | (variable) | ~$0.05 | $50/mo |
| **Total** | — | **~$0.56/mom/mo** | **~$560/mo** |

At 10,000 moms = ~$5,600/mo. At 100,000 = ~$56,000/mo. Yunchao + you optimize when this becomes meaningful.

**Watch:** if turn volume explodes (proactive Lana → 10+ turns/day instead of 5), costs ~2x. Have a per-mom turn budget alert.

---

## §11 · Reading list (in priority order)

| # | Doc | Why |
|---|---|---|
| 1 | `LANA_BLUEPRINT_v1.md` | Strategic context · esp §3-5 · §10.4 · §10.5 |
| 2 | `LANA_UNIFIED_DISCOVERY_FRONTEND.md` (your own doc) | The contract you already shipped |
| 3 | `LANA_INTENTS_AND_ROUTING_v1.md` §4 | The routing table for `lana-worker` |
| 4 | `BLOCK_LOG_SPEC_v1.md` | The missing match-log surface · the schema you ship |
| 5 | `LANA_LATENT_INTENT_ENGINE_v1.md` §6 | The v0.2 table additions |
| 6 | `LANA_RECOMMENDATION_ENGINE_v2.md` | The heterogeneous pipeline you wire |
| 7 | `LANA_SIGNAL_AGGREGATOR.md` | The Signal Aggregator pipeline (you build the materialized views) |
| 8 | `LANA_AGENT_ARCHITECTURE_v1.md` | The runtime spec |

---

## §12 · Open questions back to Tommaso

1. **Resend email integration** — when does this go live? Email is tertiary per `BLOCK_LOG_SPEC_v1.md §6` (weekly digests only) · but the API key was provided · do we wire it in week 1 or defer?
2. **Per-mom turn budget** — what's our internal cost ceiling? $1/mom/month? $5? Drives model choice (Sonnet vs Haiku vs Hermes).
3. **Cloud Run autoscaling limits** — what's our peak-concurrent-user target for Day Zero? (20 seed moms × 2 turns × 30 min interval = roughly nothing · but design for 200 moms by v0.2)
4. **Backup strategy** — Supabase point-in-time recovery · what RPO? (1h? 1d?)
5. **GDPR processor agreements** — Twilio · Resend · Anthropic · OpenAI · do we have DPAs in place for the cohorts in scope?

---

*Asjid · ship §3 by Day Zero. Then §4. Then we ramp v0.2. You're the bedrock. — Tommaso (via Claude as CTO proxy) · 2026-06-12*