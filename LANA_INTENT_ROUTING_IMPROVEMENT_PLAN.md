# LANA Intent Routing Improvement Plan

## Context

Our latest intent evaluation shows a low pass rate, but the failures are not all the same kind of problem. Some are true classifier mistakes, while others are routing or evaluation gaps where the Layer 1 classifier output is never written.

Before the hierarchical migration, the system used a large flat intent catalog, an LLM-based discovery classifier, deterministic routing gates, and an orchestrator router. This gave us flexibility, but it also created overlap between intents and made the system hard to evaluate consistently. The current migration keeps legacy `linear_intent` for compatibility while adding hierarchical fields that future classifiers can target directly.

We should learn from Simular's agent design pattern: do not rely on one general model to do everything. Simular's Agent S / S2 approach uses a generalist model for high-level planning and specialist components for narrower tasks such as grounding, memory, and execution. For LANA, that means keeping the LLM as the planner/synthesizer, while introducing smaller specialist classifiers and deterministic tool routing for intent decisions.

References:
- Simular Agent S: https://arxiv.org/abs/2410.08164
- Simular Agent S2: https://arxiv.org/abs/2504.00906
- Agent-S GitHub: https://github.com/simular-ai/Agent-S

## 1. Replace the Flat Intent Catalog With a Hierarchical Router

### Current Approach Before Migration

LANA classified directly into many flat `linear_intent` labels, such as:

- `looking.swap`
- `sharing.swap`
- `looking.tip`
- `sharing.tip`
- `discovery.find_peers`
- `discovery.find_activities`
- `tier.send_nudge`
- `social.propose_intro`
- `system.out_of_scope`

### Drawback

Many labels are semantically close. The model has to choose one exact label too early, which causes mistakes such as:

- `sharing.swap` being classified as `looking.swap`
- `sharing.tip` being confused with `looking.tip`
- `tier.send_nudge` being confused with `social.propose_intro`
- out-of-scope service requests being forced into the nearest valid in-scope intent

### Improvement

Move to a hierarchical schema while retaining `linear_intent` as a compatibility field during rollout:

```json
{
  "lane": "local_signal",
  "action": "swap",
  "direction": "offer",
  "confidence": 0.91
}
```

Suggested top-level lanes:

- `auth`
- `help`
- `settings`
- `identity`
- `discovery`
- `recommendation`
- `local_signal`
- `social`
- `safety`
- `out_of_scope`
- `chat`

This reduces the burden on one classifier. Instead of choosing between many near-duplicate labels, the system first chooses the broad lane, then resolves action and direction with smaller specialist logic.

## 2. Use DistilBERT as a Specialist Classifier, Not as a Full LLM Replacement

### Current Approach

The discovery classifier is LLM-based and returns a broad structured payload, including intent, goal, signal intent, confidence, clarification type, and some slots.

### Drawback

LLM classification is flexible but can be inconsistent, slower, more expensive, and harder to calibrate. It also tends to choose the nearest valid label instead of abstaining when the taxonomy is crowded.

### Improvement

Use DistilBERT for narrow, repeatable classification tasks:

- top-level lane classification
- local signal action classification
- offer vs seek direction
- out-of-scope boundary detection
- confidence scoring

Keep the LLM for:

- conversation quality
- planner decisions
- ambiguous cases
- complex slot extraction
- final Lana response synthesis

Recommended runtime flow:

```text
User message
  -> deterministic safety/auth gates
  -> DistilBERT lane classifier
  -> specialist action/direction classifier
  -> deterministic backend tool routing if confidence is high
  -> LLM fallback or clarification if confidence is low
  -> LLM synthesizer writes Lana's reply
```

CPU inference should be acceptable for production because DistilBERT is small. GPU is useful for faster training and iteration, but the bigger requirement is labeled data and a reliable eval framework.

## 3. Separate Out-of-Scope Detection From In-Scope Intent Classification

### Current Approach

Out-of-scope examples are mixed into the same flat classifier as valid LANA actions. The model often maps unsupported requests into nearby valid intents, such as:

- service requests -> `looking.tip`
- housing/rental requests -> `discovery.find_peers`
- third-party event requests -> `discovery.find_activities`

### Drawback

The classifier is forced to pick the closest in-scope intent, even when the correct behavior is to decline, clarify, or offer a safe LANA-supported alternative.

### Improvement

Add a dedicated supportability classifier:

```json
{
  "supported": false,
  "oos_category": "services.health",
  "safe_redirect": "offer_local_tip",
  "confidence": 0.88
}
```

This should run before detailed intent classification. If the request is unsupported, LANA should either:

- decline safely
- ask a clarification question
- redirect to a supported local action, such as asking the block for a tip

This makes the system more reliable than trying to encode every unsupported category as another flat intent.

## 4. Fix Directionality With a Dedicated Offer-vs-Seek Classifier

### Current Approach

Direction is encoded inside the flat label:

- `looking.swap`
- `sharing.swap`
- `looking.tip`
- `sharing.tip`

### Drawback

The eval report shows severe directionality failure. `sharing.swap` is classified as `looking.swap` every time. This means the model understands the domain but misses who is offering versus asking.

### Improvement

Make direction a separate field:

```json
{
  "lane": "local_signal",
  "action": "swap",
  "direction": "offer"
}
```

Then train or prompt a small specialist classifier on contrastive pairs:

- "I need a stroller" -> seek
- "I have a stroller to give away" -> offer
- "Any good pediatrician?" -> seek
- "Dr. Lee is great" -> offer/share

This is a small, high-impact fix and should be one of the first specialist models we build.

## 5. Treat Recommendation as Its Own Lane

### Current Approach

Generic recommendation asks can be intercepted by discovery and narrowed into peers, activities, or local tips. We recently added a backend handoff so broad recommendation asks can reach `recommend_value`, but the schema still treats recommendation as a special case inside discovery/orchestrator routing.

### Drawback

Generic value-seeking questions are not the same as peer search or activity browsing. A user asking "got any recommendations for me?" may want the best next community action across neighbors, events, swaps, and local signals.

### Improvement

Add a first-class recommendation lane:

```json
{
  "lane": "recommendation",
  "action": "recommend_value",
  "scope": "mixed",
  "query": "got any recommendations for me?"
}
```

Suggested scopes:

- `neighbor`
- `event`
- `local_signal`
- `mixed`

This makes the recommendation system easier to trigger, evaluate, and improve.

## 6. Fix the Eval Framework Before Using Accuracy as the Main Metric

### Current Approach

The current eval reads `_discovery_slots.linear_intent` as the main source of truth.

### Drawback

Many failures are `null`, meaning `_discovery_slots` was not written. That may happen because the turn bypassed the Layer 1 classifier through auth, help, settings, or other deterministic handlers. These are not necessarily classifier errors.

### Improvement

Split evals by routing layer:

- Layer 1 classifier eval: checks classifier output only
- deterministic handler eval: checks actual route and response behavior
- orchestrator eval: checks tool choice and final response
- end-to-end eval: checks whether the user-visible behavior was correct

This will give us a more honest accuracy number and prevent us from training against mislabeled failure modes.

## 7. Add Confidence-Based Fallback and Clarification

### Current Approach

The LLM classifier often returns a best guess, and downstream routing uses intent-specific thresholds. Some ambiguous turns still get routed into a concrete lane too early.

### Drawback

This creates loops and wrong flows. For example, ambiguous local service, event, or neighbor requests can be forced into a lane even when the right behavior is to ask a short clarification.

### Improvement

Use confidence bands:

- high confidence: route directly to tool/handler
- medium confidence: ask a targeted clarification
- low confidence: use LLM planner or safe fallback

Example:

```json
{
  "lane": "recommendation",
  "action": "recommend_value",
  "confidence": 0.62,
  "clarify": "neighbor_event_or_swap"
}
```

This mirrors Simular's generalist-specialist pattern: specialist models handle clear cases; the generalist LLM handles ambiguity.

## 8. Build a Feedback and Learning Loop

### Current Approach

We have some action logs and recommendation impressions, but intent classification outcomes are not yet consistently tied to user corrections, dismissals, accepted recommendations, or successful conversions.

### Drawback

We cannot easily learn which routing decisions were actually useful. This limits our ability to improve DistilBERT training data, thresholds, and routing policies.

### Improvement

Log a compact routing trace for each turn:

```json
{
  "utterance": "...",
  "predicted_lane": "recommendation",
  "predicted_action": "recommend_value",
  "confidence": 0.91,
  "tool_called": "recommend_value",
  "user_followup": "accepted",
  "conversion": "intro_requested"
}
```

This becomes LANA's equivalent of Simular's experience memory: not just memory for conversation, but memory for improving agent behavior.

## Proposed Phased Plan

### Phase 1: Clean Up Measurement

- Fix eval buckets so `null` is not counted as a classifier failure when Layer 1 was bypassed.
- Split classifier eval from end-to-end route eval.
- Add accepted aliases where behavior is correct, such as emergency content routing to safety.

### Phase 2: Simplify the Schema

- Introduce hierarchical fields: `lane`, `action`, `direction`, `scope`, `confidence`.
- Keep old `linear_intent` as a compatibility field during migration.
- Map old intents to new structured outputs.

### Phase 3: Add Specialist Classifiers

- Train DistilBERT lane classifier.
- Train direction classifier for offer vs seek.
- Train out-of-scope boundary classifier.
- Keep LLM fallback for low-confidence cases.

### Phase 4: Integrate With Tools

- Route high-confidence structured intents directly to backend tools.
- Make `recommendation` a first-class lane.
- Keep LLM as planner and synthesizer, not as the only classifier.

### Phase 5: Add Feedback Loop

- Store routing decisions, confidence, tool calls, and user outcomes.
- Use this data to retrain classifiers and tune thresholds.
- Add regression tests for the top failure classes from the eval report.

## Resource Needs

We do not need to train a foundation model.

We need:

- labeled intent examples
- a corrected eval framework
- DistilBERT training scripts
- CPU-compatible inference path
- optional GPU access for faster training iteration
- routing telemetry and feedback storage
- time to migrate from flat intents to hierarchical outputs

## Summary

The main lesson from Simular is not "train our own LLM." The lesson is to build a compositional agent system:

```text
LLM planner + specialist classifiers + deterministic tools + memory + eval loop
```

For LANA, the strongest next step is to stop treating intent classification as one flat LLM decision. We should use the LLM for planning and language, while using smaller specialist classifiers and deterministic routing for the repeatable intent decisions that currently cause loops and misclassification.
