# Yunchao · AI lead · handoff · v1

*Your tour of the v2 architecture · what you own · what to ship · 2026-06-12*

> Yunchao · this is your operating brief. Read `LANA_BLUEPRINT_v1.md` first for the strategic context · then this for your week-1, week-2, month-1 priorities. The diagram you produced is excellent as a Layer 1 peer-to-peer recommender. The reset is that you now own a 3-layer agentic stack · not just the matchmaker. This document tells you exactly what's yours and what's not.

---

## §1 · What you own

| Area | Scope | Why it's yours |
|---|---|---|
| **Model selection** | Which LLM for router · synthesizer · entity extractor · ranker | You're closest to the model landscape · you know what's good |
| **Prompt engineering** | All system prompts · few-shot examples · forbidden-word lists · refusal patterns | Voice consistency is the product · you guard it |
| **Layer 1 intent classifier** | 23 in-scope intents · 14 fallback categories · confidence thresholds | You designed the schema · you tune it |
| **Layer 3 architecture** | The latent intent engine (3a circumstantial + 3b inferential) · entity extractor · capability matcher · timing engine | This is the moat layer · highest-leverage IP |
| **Capability index** | What Lana CAN help with · embedded descriptions · trigger conditions · privacy tiers | This is YOUR design surface · you iterate it as you learn from production |
| **Ranker** | Heuristic v0.2 · LightGBM v0.3 · transformer v1.0 | You own the model · you own the evals |
| **Evals** | Golden set construction · per-module accuracy · adversarial · voice consistency | What gets measured gets improved · you measure |
| **Reason builder library** | Privacy-safe reason codes · multilingual templates · trust-tier filtering | You ensure recommendations are explainable AND safe |

---

## §2 · What you DON'T own (avoid scope creep)

| Area | Who owns | What to do if you have an opinion |
|---|---|---|
| Product spec · UX · brand voice | Tommaso | Bring it up · don't ship it |
| Backend infrastructure · Supabase · Twilio · push dispatch | Asjid | Define the interface you need · let him implement |
| Frontend code · mobile UI · the mockup | Abdullah | Give him the `ui_intent` contract · trust him to render |
| Schema design (after you've specified what you need) | Asjid | You say "I need these fields" · he writes the DDL |
| Day Zero ops · seed cohort selection · founding moms | Tommaso | Tell him what data you need · don't recruit yourself |

---

## §3 · Week-1 priorities (June 13-19 · pre-Day-Zero)

### Critical · must ship before Day Zero (June 15)
1. **Lock the intent classifier prompt** for the 23 Layer 1 intents · ensure confidence calibration is right · 5 hours
   - Read `LANA_INTENTS_AND_ROUTING_v1.md §2`
   - Build a 50-utterance test set covering each intent
   - Tune the confidence threshold (default 0.85)
2. **Seed the `capability_index` table** · write descriptions for all 23 capabilities + 14 fallback categories · embed them · ship · 4 hours
   - Schema in `LANA_LATENT_INTENT_ENGINE_v1.md §4.1`
   - Format: capability_id · capability_name · description (50-100 words for embedding) · entity_triggers · privacy_tier
3. **Seed the `reason_codes` library** · 20-30 codes covering proximity · identity · behavior · history · 2 hours
   - Schema in `LANA_RECOMMENDATION_ENGINE_v2.md §2 (Gap 5)`
   - Each code needs EN / PT / ES variants
4. **Ship the `capture_inquiry` tool** as the universal fallback when Layer 1 confidence < 0.85 · 3 hours
   - When invoked, runs the 4-step ACK / ENGAGE / NAME / CAPTURE dialogue
   - Writes to `out_of_scope_inquiry` table

**Total: ~14 hours · split with prompt iteration · realistic for week 1**

### High priority · ship in v0.2 (Sept 2026)
5. Build the entity extractor (Haiku or spaCy-based · prototype with both)
6. Build the heuristic ranker v0.2 (handcrafted feature weights)
7. Build the timing engine logic (decide when Layer 3b surfaces)

### Medium priority · v0.3 prep
8. Design the impression logging schema (with Asjid · he writes the DDL)
9. Design the gradient-boosted ranker training pipeline (LightGBM)
10. Build the Layer 3 eval set (200 opportunity turns + 100 negatives)

---

## §4 · Week-2-to-Month-1 priorities (June 20 - July 15)

### v0.2 architectural buildout
1. **Layer 3a state machine** — wire the FTUE chime-in (signup at value-delivered moment) · the re-engagement (7-day quiet) · the milestone celebration (first match · first IRL meet)
2. **Layer 3b prototype** — entity extractor running on all production turns · `latent_signals` logged · NOT yet surfacing
3. **Capability index iteration** — based on real production data · expand descriptions · add new capabilities as out-of-scope captures cluster into roadmap candidates
4. **Reason builder v1** — given a recommendation + trust tier + identity claims · return reason_codes[] + reason_text · multilingual
5. **Ranker v1** — heuristic with 6 features (similarity · proximity · connector value · responsiveness · activity fit · integration likelihood) · weighted sum · tune weights on labeled data when available

### Evals & quality
6. **Golden eval set v1** — 100 manually-labeled turns covering each intent · use as regression test before any prompt change
7. **Adversarial eval set** — 20-50 turns of jailbreak attempts · predator attempts · scam attempts · ensure refusal patterns hold
8. **Voice consistency eval** — sample 50 random turns · score on Lana voice rules (per `LANA_BRAND_GUIDELINES.md`) · ensure < 5% drift

### Architecture extensions
9. **Memory layer integration** — wire Letta archival memory · ensure Lana recalls last-week's conversation when relevant THIS week
10. **Model abstraction layer** — write a thin wrapper so router can swap between Hermes 4 · Claude Sonnet 4 · GPT-4o · ensures we're not locked in

---

## §5 · Month-2-to-Quarter-1 priorities (July 15 - Sept 30 · v0.2 → v0.3)

### Layer 3b production
1. **Train ranker v2 (LightGBM)** on 8 weeks of impression data
2. **A/B test ranker v1 vs v2** · measure acceptance rate
3. **Turn on Layer 3b surface** with feature flag (10% of users) · iterate based on data
4. **Roll to 100%** if metrics positive (suggestion precision ≥ 65%, false positive ≤ 5%)

### Personalization
5. **Per-mom ranker** — additive features on top of global ranker · captures per-mom preferences (does she accept identity-based recs more than proximity-based?)
6. **Cold-start strategy** — what does a brand-new mom see? Use population-level priors from her ZIP + her identity claims

### Multi-language production
7. **PT-BR fine-tuning** — system prompts · reason codes · capability descriptions all in PT-BR · production-quality
8. **ES fine-tuning** — same for Spanish

---

## §6 · Decision points · what I need from you before week 1 starts

### D1 · Model stack for v0.2
You currently have unified Lana running on Claude Sonnet (per `LANA_UNIFIED_DISCOVERY_FRONTEND.md`). Question:
- Stay on Claude Sonnet for router + synthesizer?
- Move router to Hermes 4 (function-calling specialist) · keep synthesizer on Sonnet?
- Move synthesizer to Hermes 4 too?

**Recommendation:** for v0.2, keep router on Sonnet (well-tested) · move synthesizer to Haiku (cost) · evaluate Hermes 4 for router in v0.3 based on benchmarks.

### D2 · Entity extractor
- Option A: another Sonnet/Haiku call per turn (high cost, easy to integrate)
- Option B: spaCy or BERT-NER (low cost, requires hosting, less context-aware)
- Option C: hybrid — Haiku for novel turns + cached NER for common entities

**Recommendation:** Option A for v0.2 (simpler), Option C in v0.3 once volume justifies caching.

### D3 · Ranker v0.2 weighting
The 6 features need initial weights. Suggestion:
- similarity: 0.30
- proximity: 0.20
- activity_fit: 0.20
- connector_value: 0.10
- responsiveness: 0.10
- integration_likelihood: 0.10

You'll re-tune based on impressions. Want to ship with these as defaults?

### D4 · Layer 3b initial entity types
Don't try to cover everything. Pick 5 entity types for v0.2:
- `activity` (karate · yoga · running)
- `life_event` (moved · new baby · started school)
- `professional_service` (dentist · doctor · tutor)
- `consumer_good` (stroller · jacket · books)
- `relationship` (husband · grandma · in-laws)

This covers ~70% of high-value latent triggers based on the Modus Operandi doctrine.

### D5 · Reason code privacy-tier defaults
Each reason code has a privacy tier (1 = always safe, 5 = only fully-verified pair). Default tiers:
- proximity codes: tier 1
- identity codes: tier 2 (need mom to be phone-verified)
- behavior codes: tier 1-2 depending on specificity
- history codes: tier 2 (requires prior interaction)

Want to adjust?

---

## §7 · How you and Asjid stay in sync

The boundary is sharp: **you define the data CONTRACTS · Asjid implements the storage and pipelines.**

Process:
1. You spec a new table or RPC in a doc (`LANA_*_v1.md`)
2. Asjid reviews · pushes back if implementation is infeasible · agrees on schema
3. He writes the migration · ships the API
4. You consume the API from the worker

Weekly sync: 30 min · Mondays · agenda = (a) blockers (b) new schema asks (c) production data observations.

### Things that fall in the cracks (own them jointly)
- The capability index admin UI — you need to iterate without engineering tickets · Asjid needs to support read+write paths · figure out the contract
- Telemetry / observability — you need to see your prompts fail in production · Asjid needs to log structured turn data · jointly design the dashboard
- The model abstraction layer — you select models · Asjid wires the inference endpoints · jointly design the swap mechanism

---

## §8 · How you and Abdullah stay in sync

Abdullah consumes the `ui_intent` contract (per `LANA_UNIFIED_DISCOVERY_FRONTEND.md`). You produce it. Boundary:

You produce per turn:
```json
{
  "assistant_message": "string",
  "ui_intent": "chat | collect_phone | collect_otp | show_peer_preview | ...",
  "peer_matches": [...],
  "activity_previews": [...],
  "auth_action": null | {type: ..., ...},
  "active_intent": "string",
  "routing_phase": "string",
  "recommendations": [...]  // NEW in v0.2
}
```

Abdullah renders. Boundary:
- You DON'T tell him where to put the bubble
- He DOESN'T tell you what to say

Process: whenever you add a new `ui_intent` value, ping Abdullah · he wires the UI shell.

---

## §9 · Reading list (in priority order)

| # | Doc | Why |
|---|---|---|
| 1 | `LANA_BLUEPRINT_v1.md` | The strategic context for everything below |
| 2 | `LANA_LATENT_INTENT_ENGINE_v1.md` | Your moat layer · read carefully |
| 3 | `LANA_RECOMMENDATION_ENGINE_v2.md` | Response to your v1 diagram · what extends, what stays |
| 4 | `LANA_INTENTS_AND_ROUTING_v1.md` | The 23 in-scope + 14 fallback catalog |
| 5 | `LANA_AGENT_RESEARCH_v1.md` | The research grounding (Anthropic · Sierra · Letta · Hermes) |
| 6 | `LANA_TOOL_ROUTING_v1.md` | The R/A/T/C decision logic per turn |
| 7 | `LANA_MODUS_OPERANDI.md` | Per-module doctrine |
| 8 | `LANA_OUT_OF_SCOPE_PLAYBOOK.md` | The 4-step graceful refusal |
| 9 | `LANA_SIGNAL_AGGREGATOR.md` | Where captured signals feed the roadmap |
| 10 | `LANA_AGENT_ARCHITECTURE_v1.md` | The runtime spec |

Skim 5-10 · read 1-4 in depth.

---

## §10 · Open questions back to Tommaso (for your week-1 sync)

These are blocking for you. Get answers fast:

1. **Brand voice for Layer 3 suggestions** — "by the way..." (light) vs "I noticed..." (observant) vs "may I..." (deferential)?
2. **Default suggestion frequency** — conservative (1/week) vs aggressive (3/day)?
3. **Opt-in vs opt-out for proactive Lana** — should mom explicitly consent, or is it default-on with settings off-switch?
4. **Confidence display** — show mom how confident Lana is in a recommendation, or hide?
5. **Model spend ceiling** — what's our $/mom/month target so you can pick the right inference tier?

---

*Yunchao · this is yours. Read it twice. Ask Tommaso the §10 questions before Monday. Build the §3 critical items by Day Zero. Then we sprint v0.2 together.*

*— Tommaso (via Claude as CTO/CAIO proxy) · 2026-06-12*
