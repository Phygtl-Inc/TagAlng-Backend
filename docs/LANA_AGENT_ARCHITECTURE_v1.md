# Lana · Agent Architecture · v1
*spec for backend AI team · 2026-06-04*

> Canonical architecture document for the engineers building Lana (Azjit, Aki, Tommaso). This is the runtime spec — what Lana *is* on the wire — derived from `LANA_AGENT_RESEARCH_v1.md` (the research) and `AI_PIPELINE_v1.md` (the pre-existing pipeline draft). When this doc disagrees with the pipeline draft, this doc wins; the pipeline draft predates the production-benchmark research and is treated as input, not authority.

---

## §1 · TL;DR

Lana is a single conversational agent with a fixed persona, a thin deterministic router, five capability modules expressed as **tool clusters**, a two-tier memory model in the MemGPT/Letta tradition, and a NeMo-Guardrails-style policy wrapper on input and output. The user perceives one voice; the system perceives five capabilities plus a cross-cutting inquiry collector. Out-of-scope handling is not a corner case — it is a first-class data path; every refusal calls `capture_inquiry` and every captured inquiry becomes product-roadmap signal.

We ship as one agent, not five, for three reasons documented in `LANA_AGENT_RESEARCH_v1.md` §1: voice consistency is the product (multi-agent handoffs leak persona within ~3 turns per Pi/Tolan/Replika post-mortems), the 15× token tax of multi-agent (Anthropic research-system numbers) is not earned at five capabilities, and the industry rule of thumb is to stay single-agent until one module plateaus <0.85 on evals with ≥10 tools. We are below that threshold and intend to stay there for v0.1 and v0.2.

In code: one persona-anchored LLM loop (Sonnet for synthesis hero moments, Haiku for router and guardrail), tools grouped into five Zod-schema clusters with a shared `capture_inquiry`, a Postgres + pgvector memory layer with an always-in-context "core block" plus an archival store retrieved on demand, and an out-of-loop policy layer (input rails → output rails) the user never talks to. The state machine from `AI_PIPELINE_v1.md` §3 is retained as the rails on top of the router — the LLM proposes, XState disposes.

---

## §2 · System architecture diagram

```
                                ┌────────────────────────────┐
USER  ── text/voice ──▶         │   INPUT LAYER              │
                                │   - Whisper STT (voice)    │
                                │   - text passthrough       │
                                │   - PII scrubber           │
                                └──────────────┬─────────────┘
                                               │ utterance
                                               ▼
                                ┌────────────────────────────┐
                                │   GUARDRAILS · INPUT RAILS │
                                │   - Llama Guard moderation │
                                │   - safety-trigger detect  │
                                │   - off-topic pre-classify │
                                └──────────────┬─────────────┘
                                               │ utterance + flags
                                               ▼
                ┌──────────────────────────────────────────────────────┐
                │            ORCHESTRATOR  (Claude Haiku · router)      │
                │  ┌──────────────────┐  ┌───────────────────────────┐ │
                │  │ STATE MACHINE    │  │ MEMORY LOADER             │ │
                │  │ (XState · 8 ste) │  │ - core block (always)     │ │
                │  └──────────────────┘  │ - archival retrieve top-k │ │
                │  ┌──────────────────┐  └───────────────────────────┘ │
                │  │ INTENT CLASSIFY  │  ┌───────────────────────────┐ │
                │  │ - on/off topic   │  │ PERSONA PREFIX            │ │
                │  │ - capability     │  │ (static · §2 AI_PIPELINE) │ │
                │  └──────────────────┘  └───────────────────────────┘ │
                └────────────────────┬─────────────────────────────────┘
                                     │ structured tool call(s)
            ┌────────────┬───────────┼─────────────┬────────────────┐
            ▼            ▼           ▼             ▼                ▼
      ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌──────────────┐
      │ IDENTITY │ │DISCOVERY │ │ ACTIVITY │ │ MARKETPLACE  │ │ TIER MANAGER │
      │ /intake  │ │          │ │ /planning│ │              │ │              │
      │          │ │          │ │          │ │              │ │              │
      │ extract_ │ │ find_    │ │ publish_ │ │ search_      │ │ propose_intro│
      │ claims   │ │ matches  │ │ activity │ │ block_offers │ │ check_consent│
      │          │ │ propose_ │ │ propose_ │ │ list_item    │ │ advance_tier │
      │          │ │ intro    │ │ cohost   │ │              │ │              │
      └──────────┘ └──────────┘ └──────────┘ └──────────────┘ └──────────────┘
            │            │           │             │                │
            └────────────┴───────────┼─────────────┴────────────────┘
                                     │             ▲
                                     │             │
                                     │      ┌──────┴─────────┐
                                     │      │ CAPTURE_INQUIRY│ ◀── available
                                     │      │  (cross-cut)   │     to all clusters
                                     │      └────────────────┘     and to default fallback
                                     │
                                     ▼
                                ┌────────────────────────────┐
                                │   COMPANIONSHIP / DEFAULT  │
                                │   (no tool · synthesis only)│
                                │   warm acknowledgment turn │
                                └──────────────┬─────────────┘
                                               │ structured response
                                               ▼
                                ┌────────────────────────────┐
                                │   SYNTHESIZER  (Sonnet)    │
                                │   - persona prefix         │
                                │   - tool results           │
                                │   - core + retrieved mem   │
                                │   - structured JSON output │
                                └──────────────┬─────────────┘
                                               │ LanaResponse JSON
                                               ▼
                                ┌────────────────────────────┐
                                │   GUARDRAILS · OUTPUT RAILS│
                                │   - groundedness check     │
                                │   - persona drift check    │
                                │   - PII / tier leak check  │
                                │   - refusal-w/o-capture    │
                                └──────────────┬─────────────┘
                                               │ validated response
                                               ▼
                                ┌────────────────────────────┐
                                │   OUTPUT LAYER             │
                                │   - tts-1 TTS (opt-in)     │
                                │   - structured UI render   │
                                └──────────────┬─────────────┘
                                               │
USER  ◀── text/voice ──                        ▼
                                ┌────────────────────────────┐
                                │   MEMORY WRITE-BACK        │
                                │   - lana_messages insert   │
                                │   - inquiry_signals if cap │
                                │   - identity_claims if new │
                                │   - audit log              │
                                └────────────────────────────┘
```

Five capability modules sit horizontally, orchestrator and synthesizer sit vertically, guardrails wrap both sides. `capture_inquiry` is drawn into the capability strip because it is **available to every module** — not a sixth module but a cross-cutting tool any capability invokes when its in-scope branch fails. Companionship is the default fallback when no capability tool is selected; it has no tools, just synthesis.

---

## §3 · The orchestrator

### Role

The brain stem. Per turn it loads memory, classifies intent, picks a tool (or none), enforces the state-machine transition, and hands context to the synthesizer. Runs on **Claude Haiku 4.5** — latency dominates UX and Haiku is the right cost/quality point for routing. The synthesizer is a separate call (Sonnet for hero moments, Haiku otherwise). This cheap-router / smart-synthesizer split is the production pattern at Sierra, Klarna, and Decagon (`LANA_AGENT_RESEARCH_v1.md` §8) and lets us scale either independently.

### Inputs

Per turn: user utterance (post-STT, post-PII-scrub) · core memory block · archival retrieval (top-k pgvector hits on the utterance) · session state (XState state + context from `lana_sessions`) · surface context (per `AI_PIPELINE_v1.md` §10.5 C3) · persona prefix (static, compiled at deploy) · guardrail flags (severity, OOS hint, safety triggers).

### Decision logic

Single Haiku call with structured output:

```typescript
{
  intent_class: 'identity' | 'discovery' | 'activity' | 'marketplace' | 'tier' | 'companionship' | 'off_topic',
  confidence: 0..1,
  tool_to_call: string | null,
  tool_args: Record<string, unknown> | null,
  should_capture_inquiry: boolean,
  capture_args: { category, free_text, urgency } | null,
  proposed_state_transition: string | null
}
```

Validated against (a) the proposed tool's Zod schema, (b) the state machine's allowed transitions, (c) guardrail policy. No auto-retry on validation failure — fall back deterministically (see below).

Intent classification is **hybrid**: a sentence-transformer model runs in parallel returning a soft prior per capability; Haiku reads that prior in its prompt. Hybrid closes the OOS gap to ~2% with 50% lower latency than LLM-only (`LANA_AGENT_RESEARCH_v1.md` §4 · arXiv 2507.22289).

### Outputs

Selected tool call(s) executed against backend (or none for companionship turns) · state transition (applied via XState before synthesizer runs) · memory writes queued (deferred until output rails pass — never persist a turn we end up suppressing) · telemetry event with `intent_class`, `confidence`, `tool_call`, latency-to-decision.

### Fallback chain

1. **Low-confidence intent (<0.6)** → Companionship · one clarifying question · no state transition.
2. **Tool arg validation fails** → log, retry once with stricter prompt + Zod error embedded, then Companionship.
3. **State machine refuses transition** → router told the refusal reason; re-plans under the constraint (e.g., "user unverified, collect phone first").
4. **No capability tool fits AND intent ≠ pure companionship** → auto-trigger `capture_inquiry`, then Companionship.
5. **Guardrail hard block (safety)** → suppress all tool execution, render safety-response template, log audit event.

Invariant: every turn ends in either (tool + response) or (no tool + response). Silence is a bug.

---

## §4 · Memory architecture (2-tier · MemGPT-style)

Two tiers mirroring Letta (`LANA_AGENT_RESEARCH_v1.md` §8). `AI_PIPELINE_v1.md` §5 specified three tiers (working/session/long-term); we collapse working into the core block (it's just the most-recent slice of core context) and reframe session as the audit log behind archival, not a tier the LLM sees. Three tiers was an artifact of treating the context window as a tier rather than a budget.

### Tier 1 · Core block (always in context · ~800 tokens)

Loaded into every router and synthesizer call. Schema:

```json
{
  "user": {
    "id": "uuid",
    "first_name": "string | null",
    "block_id": "uuid",
    "block_label": "string",          // 'Whisper Park'
    "language": "en | pt | es",
    "verified": "boolean",
    "joined_at": "iso8601"
  },
  "tier_ladder": {
    "current_tier_with_lana": "stranger | nudge | acquaintance | direct | irl",
    "tier_with_neighbors": {           // sparse map · only populated peers
      "neighbor_id": "tier"
    }
  },
  "session": {
    "id": "uuid",
    "state": "greeting | listening | matched | ...",   // current XState state
    "last_3_turns": [                  // last 3 user-Lana turn pairs · verbatim
      { "role": "user", "content": "..." },
      { "role": "lana", "content": "..." }
    ],
    "current_goal": "string | null",   // free-form, what Lana thinks the user is trying to do
    "last_topic": "string | null",
    "last_captured_inquiry_id": "uuid | null"
  },
  "active_signals": {
    "pending_joint_moment": "joint_id | null",     // from jointSuggester cron
    "open_marketplace_inquiry": "inquiry_id | null"
  }
}
```

The core block is a single editable JSON document. The synthesizer writes back proposed updates (`core_patch`) as part of its structured output; the orchestrator applies them post-validation.

### Tier 2 · Archival store (pgvector · retrieved on demand)

Persistent memory retrieved explicitly via a tool, not via implicit context stuffing — the Letta pattern, avoiding the "memory bloat" failure where every turn loads everything. Tables:

| Table | Holds | Vector field |
|---|---|---|
| `user_identity_claims` | Structured claims per user (existing table) | `embedding` |
| `lana_messages` | Verbatim turn-by-turn log (per `AI_PIPELINE_v1.md` §10.5 C2) | `embedding` |
| `inquiry_signals` | Captured out-of-scope and unmet-need signals | `embedding` |
| `neighbor_facts` | Per-neighbor learned facts (with consent flags) | `embedding` |
| `block_context` | Block-level patterns (recurring events, language mix) | `embedding` |

Retrieval signature: `recall(query, scope: 'self' | 'neighbors' | 'block', k=5) → Memory[]`. Scopes: `self` = only the speaking user's memories; `neighbors` = respects tier-ladder gating (Stranger cannot retrieve Direct-tier facts even if they exist); `block` = block-wide aggregates, always public-safe.

### Write rules · who writes what when

| Writer | Writes to | When |
|---|---|---|
| `intakeSkill` extraction | `user_identity_claims` (confidence ≥0.7) | turns yielding claims |
| Orchestrator | `lana_messages` | post-output-rails pass |
| `capture_inquiry` tool | `inquiry_signals` | every fire |
| Tier-manager tool | `neighbor_facts` (with consent) | intro accepted or IRL met |
| `jointSuggester` cron | `active_signals.pending_joint_moment` | every 5 min |
| Synthesizer `core_patch` | `lana_sessions.core_block` | post-output-rails pass |

Never write before output rails pass. If a response is suppressed for safety or groundedness, its memory writes are discarded — we don't persist hallucinations.

### Forgetfulness mitigation · explicit recall pattern

Per `LANA_AGENT_RESEARCH_v1.md` §8 Replika 2.0 lesson, pattern memory matters more than fact memory. Three mechanisms:

1. **Pre-turn retrieval** — on every router call, embed the utterance and prefetch top-5 self + top-5 neighbor memories (~30ms pgvector query). Gives the LLM grounding without burning a tool call.
2. **Recall tool** — synthesizer can request additional memories mid-composition to cite a specific fact ("you mentioned São Paulo two weeks ago"). One recall per turn max.
3. **Pattern memory** — behavioral patterns derived nightly (when the user talks, how she escalates, topics she returns to) surfaced in the core block under `session.pattern_hints` after session 3+.

### Schema sketch (additive · Supabase ref `rjlcyvwogmfmngemhbmn`)

`user_identity_claims` and `lana_messages` already exist (the latter from `AI_PIPELINE_v1.md` §10.5 C2). Additive tables:

```sql
CREATE TABLE inquiry_signals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES users(id),
  block_id uuid REFERENCES blocks(id),
  captured_at timestamptz DEFAULT now(),
  category text NOT NULL,
  free_text text NOT NULL,
  embedding vector(1536),
  urgency text,
  opt_in_followup boolean DEFAULT false,
  source_module text NOT NULL,
  cluster_id uuid,
  status text DEFAULT 'open'
);
CREATE INDEX ON inquiry_signals USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE lana_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES users(id),
  started_at timestamptz DEFAULT now(),
  ended_at timestamptz,
  state text NOT NULL,
  core_block jsonb NOT NULL,
  cost_cents integer DEFAULT 0
);

CREATE TABLE neighbor_facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subject_user_id uuid REFERENCES users(id),
  fact text NOT NULL,
  consent_tier text NOT NULL,           -- 'stranger'..'irl'
  embedding vector(1536),
  source text,                           -- 'self_disclosed' | 'inferred'
  created_at timestamptz DEFAULT now()
);
```

Migrations only (Azjit gatekeeper · no dashboard edits).

---

## §5 · Capability modules

Five capability modules + cross-cutting capture. Each module = contract doc + tool subset + out-of-scope list. The user perceives one Lana; the router perceives five clusters.

| # | Module | Purpose | Tools | Invoked when | Success metric | Doctrine pointer |
|---|---|---|---|---|---|---|
| 1 | **Identity / Intake** | Extract identity claims from user utterances to enable matching | `extract_identity_claims`, `recall` | New user, first 3-6 turns; any turn with disclosure | ≥3 claims per onboarding session, confidence ≥0.7 | `LANA_MODUS_OPERANDI.md#identity` |
| 2 | **Discovery** | Find matching neighbors, events, and joint-moment overlaps | `find_matches`, `check_joint_overlap`, `propose_intro`, `recall` | User asks "who", "anyone like me", "is there...", or after intake completes | match precision @ top-5 ≥0.7, search-to-action ≥0.4 | `LANA_MODUS_OPERANDI.md#discovery` |
| 3 | **Plan-an-activity** | Help user define and publish a block activity, suggest co-host | `publish_activity`, `propose_cohost`, `edit_activity`, `cancel_activity` | User says "I want to host", "let's do", "Saturday" + activity verb | publish completion ≥60% from intent, invite-accept ≥30% | `LANA_MODUS_OPERANDI.md#activity` |
| 4 | **Marketplace** | Surface block offers, list user's items, capture unmet needs | `search_block_offers`, `list_exchange_item`, `find_exchange_match`, `capture_inquiry` | User mentions a need/offer for a thing or service | match rate ≥0.6, capture rate on no-match ≥0.95 | `LANA_MODUS_OPERANDI.md#marketplace` |
| 5 | **Relationship-tier manager** | Gate intro proposals and tier transitions by consent and ladder rules | `propose_intro`, `check_consent`, `advance_tier`, `reveal_with_consent` | Whenever an action would expose data above current tier (intros, joint moments, neighbor facts) | tier-transition acceptance ≥0.7, inappropriate-skip rate = 0 | `LANA_MODUS_OPERANDI.md#tier` |
| · | **Companionship** (default fallback) | Warm presence, micro-rapport, single-question check-ins when no capability matches | none (synthesis only) | Low-confidence routing, post-refusal, idle reopens, emotional disclosure | session length, 7-day return rate, disclosure-depth trend | `LANA_MODUS_OPERANDI.md#companionship` |
| × | **`capture_inquiry`** (cross-cutting) | Persist any unmet need or out-of-scope ask as structured signal | `capture_inquiry` | Any module's no-match branch; off-topic with adjacent need | refusal-without-capture rate ≤5%, weekly capture volume reviewed | `LANA_OUT_OF_SCOPE_PLAYBOOK.md` |

Each module ships with: purpose statement (one sentence in the user's words), tool list with Zod schemas (§7), out-of-scope list with patterns + graceful-refusal copy, edge cases (ambiguous input, missing data, contradictory goals, safety triggers), voice constraints (3 must-do / 3 banned phrases + pointer to `LANA_BRAND_GUIDELINES.md`).

Per-module doctrine is **version-controlled markdown**, not embedded in the system prompt. The system prompt is a compiled artifact regenerated from doctrine + tool schemas + golden eval set (§9). This is how Sierra, Decagon, and Anthropic's research team all work (`LANA_AGENT_RESEARCH_v1.md` §9 #5).

---

## §6 · Guardrails

Three tiers wrapping the persona loop. The user never talks to a guardrail.

### Tier 1 · Input rails (pre-router)

- **Moderation** — Llama Guard 3 8B (Modal/Replicate). Hard-blocks self-harm, abuse, CSAM. Severity 0-3.
- **PII scrubber** — regex + spaCy NER. Strips phone numbers, SSNs, full addresses from the utterance before LLM context (don't teach the router to ask for them by example).
- **Off-topic pre-classifier** — sentence-transformer (~30ms), soft prior fed to router as a hint, not a hard block.
- **Safety-trigger detector** — keyword + classifier hybrid for DV / postpartum / suicidal-ideation markers. Severity ≥2 → safety-response template (no LLM generation) + resource card.

### Tier 2 · Output rails (pre-send · ~50-80ms budget)

- **Groundedness check** — structured Haiku call returning `{claim, supported, source}` per factual claim. Threshold: 0 unsupported. Failure → regenerate once, then drop the unsupported claim.
- **Persona drift check** — LLM-judge rates response 1-5 against `LANA_BRAND_GUIDELINES.md`. Threshold ≥4. Failure → regenerate with persona prefix re-emphasized.
- **Tier-leak check** — deterministic: does the response reveal a fact whose `consent_tier` exceeds the user's current tier with the subject? Hard block, no retry leniency.
- **Refusal-without-capture check** — if response contains a refusal pattern AND no `capture_inquiry` fired this turn, fail closed. Re-run orchestrator with capture mandated.

### Tier 3 · Audit log (post-send)

Every refusal and every capture is logged with full context to `lana_audit_log (id, session_id, event_type, module, utterance_redacted, response_redacted, guardrail_result jsonb, created_at)`. Weekly review by product; refusal-without-capture rate is a top-line metric (§8) and audit log is the source of truth.

### Implementation note

We do not adopt NeMo Guardrails as a framework — its Colang DSL adds runtime weight we don't need at five capabilities. We adopt the *pattern*: input rails → dialog/router → retrieval → execution → output rails, implemented in TypeScript with Zod schemas and small specialist models for moderation. Fuller integration (NeMo, Guardrails AI, or Anthropic's tool-use-with-policies) is v0.3 debt.

---

## §7 · Tool-call contract

Every tool is registered with: Zod schemas for inputs and outputs (compile-time + runtime validation) · idempotency key (side-effect tools) · documented failure modes with deterministic fallbacks · cost class (`free | db | embed | llm`) for telemetry.

### Strict Zod-style schemas

```typescript
// lib/lana/tools/proposeIntro.ts
import { z } from 'zod';

export const ProposeIntroInput = z.object({
  initiator_user_id: z.string().uuid(),
  candidate_user_id: z.string().uuid(),
  reason: z.string().min(10).max(280),       // cited verbatim by Lana
  shared_dimensions: z.array(
    z.enum(['stage', 'activity', 'interest', 'lifestyle', 'language', 'heritage', 'faith'])
  ).min(1),
  joint_moment_id: z.string().uuid().nullable(),
});

export const ProposeIntroOutput = z.discriminatedUnion('status', [
  z.object({ status: z.literal('proposed'), intro_id: z.string().uuid(),
             expires_at: z.string().datetime(), user_facing_message: z.string() }),
  z.object({ status: z.literal('blocked_by_tier'),
             reason: z.enum(['candidate_consent_missing','initiator_unverified','tier_too_low']),
             suggested_remediation: z.string() }),
  z.object({ status: z.literal('duplicate'), existing_intro_id: z.string().uuid() }),
]);
```

### Idempotency rules

Writing tools (`publish_activity`, `list_exchange_item`, `propose_intro`, `capture_inquiry`) take an optional `client_idempotency_key`; retries with the same key return the original result. Read tools (`find_matches`, `recall`, `search_block_offers`) are pure functions of `(args, db_state, time_bucket)` with `time_bucket` snapped to 60s.

### Failure modes per tool

Tools return one of `{status: 'ok' | 'empty' | 'blocked' | 'error', ...}`. Orchestrator handles each deterministically: `empty` → auto-trigger `capture_inquiry` (the data-flywheel substrate); `blocked` → surface remediation in persona voice; `error` retryable → one backoff retry then fallback; `error` non-retryable → Companionship + log.

### Example tool: `propose_intro` (the joint-moment pairing)

The highest-stakes write — bridges the tier ladder and triggers a notification on another user. Behavior contract:

1. Validate input via Zod.
2. Check candidate's `consent_tier ≥ stranger` AND `consent_to_receive_intros = true`.
3. Check initiator is verified AND has tier ≥ nudge in `user_relationships`.
4. Check no existing intro between this pair in last 30d (idempotency dedupe on `(initiator, candidate, day_bucket)`).
5. INSERT into `intros` with `expires_at = now() + 72h`.
6. Trigger notification to candidate (push + in-app).
7. Return `ProposeIntroOutput` with `status='proposed'` + user-facing copy.

Failure modes return `blocked` with remediation: `candidate_consent_missing` → "wait for candidate to opt in"; `initiator_unverified` → "collect phone via signupSkill"; `tier_too_low` → "send a nudge first to build tier"; `duplicate` → return existing intro_id, no new row. Telemetry: increment `proposed_intros` by `(initiator_block, candidate_block)`; if `joint_moment_id` present, increment `cron_attribution`.

Tool documentation lives in TSDoc on each export and is auto-extracted into the module's doctrine document — code and doctrine never drift.

---

## §8 · Definition of success

Three layers, measured continuously from `lana_audit_log`, `lana_sessions`, `inquiry_signals`, and Langfuse traces.

### Per-turn

| Metric | Definition | Target (v0.1) | Block-ship? |
|---|---|---|---|
| Groundedness | % factual claims supported by tool/memory | ≥ 0.9 | yes |
| On-topic rate | % turns Lana stayed on user's intent | ≥ 0.95 | yes |
| Refusal-without-capture rate | refusals lacking `capture_inquiry` / total refusals | ≤ 0.05 | yes |
| Tool-call accuracy | % tool selections matching human-labeled correct | ≥ 0.9 | yes |
| Turn latency p50 / p95 | user-send → first Lana token | <1.5s / <3s | p95 only |
| Voice-consistency | LLM-judge persona rubric (1-5) | ≥ 4.0 | yes |

### Per-session

| Metric | Definition | Target (v0.1) |
|---|---|---|
| Task completion rate | % sessions where stated user intent resolved | ≥ 0.6 |
| "Felt heard" qualitative | post-session 1-tap survey (Pro tier opt-in) | ≥ 4.0 / 5 |
| Session containment | % sessions resolved without human escalation | ≥ 0.8 |
| D1 return rate | % users who return within 24h | ≥ 0.4 |
| D7 return rate | % users who return within 7d | ≥ 0.4 (`LANA_AGENT_RESEARCH_v1.md` §6) |
| Repair frequency | repair turns / total turns | ≤ 0.1 |

### Per-module (per agent)

| Module | Primary metric | Secondary |
|---|---|---|
| Identity | claims/onboarding session (target ≥3) | dimension coverage (target ≥4 of 7) |
| Discovery | precision @ top-5 (target ≥0.7) | search-to-action rate (target ≥0.4) |
| Activity | plan completion rate (target ≥0.6) | invite acceptance (target ≥0.3) |
| Marketplace | match rate (target ≥0.6) | capture rate on no-match (target ≥0.95) |
| Tier manager | tier-transition acceptance (target ≥0.7) | inappropriate-skip rate (target 0) |
| Companionship | session length, return rate | disclosure depth (trend up) |

**Capability-gap signal volume is good news.** A high `inquiry_signals` capture rate per block per week means Lana is learning what to build next. If captures drop, Lana is wrongly forcing in-scope answers — confirm via the refusal-without-capture rate.

---

## §9 · Eval suite design

### Golden test set

500 turns, 100 per module + 50 off-topic / capture stress:

- **Identity (100)** — diverse openings across 7 dimensions in EN/PT/ES. Expect: claims extracted at ≥0.7 confidence on ≥80% of utterances.
- **Discovery (100)** — varied specificity ("who's around" → "Brazilian moms with a kid under 1"). Expect: ≥1 match with calibrated score.
- **Activity (100)** — host intents across the 10-purpose taxonomy. Expect: 3-turn happy path completes; cohost suggestion fires when overlap >0.65.
- **Marketplace (100)** — in-scope offers/needs + 30% adjacent-OOS. Expect: in-scope matches; OOS calls `capture_inquiry`.
- **Tier manager (50)** — intro proposals across all tier boundaries. Expect: 0 inappropriate skips; every blocked refusal has remediation.
- **Companionship + off-topic (50)** — distress signals, off-topic asks, idle reopens. Expect: safety template for distress; capture + warm redirect otherwise.

Stored as JSON in `/eval/golden/<module>/*.json`, format `{id, input: {utterance, user_state}, expected: {tool_to_call, tool_args_contains, response_contains, must_not_contain, ends_turn}}`.

Runner: `npm run eval -- --suite <module>` against a stable model snapshot. Threshold: ≥0.85 per-module pass = ship-ready; <0.85 = block. The 0.85 threshold is also the trigger for considering sub-agent escalation (`LANA_AGENT_RESEARCH_v1.md` §1).

### Weekly drift alerts

Production telemetry feeds a weekly job that scores live traffic. If any per-turn metric drops >10% week-over-week, Slack alert to AI on-call (Aki primary). Playbook: pull 24h of audit log for the regressed metric, cluster failures by module + intent, surface top-3 exemplars to the doctrine owner, patch doctrine + recompile prompt OR open a tool ticket.

### A/B test methodology

Treatment vs control runs on `block_id` cohorts (within a block, all users see the same Lana — don't mix experiences). Minimum: 2 blocks per arm, 4 weeks per test, decision metric = per-session task-completion rate. We do not A/B the persona prompt in v0.1 (voice consistency > optimization); we A/B router prompts, tool descriptions, and tier thresholds.

### Cost target

Instrumentation budget: **~$200/mo** for v0.1 (Langfuse hosted + Modal eval runs + cron compute) per `LANA_AGENT_RESEARCH_v1.md` §9 #6.

---

## §10 · Failure modes + recovery

| Failure | Detection | Recovery |
|---|---|---|
| Stuck in capability loop (same tool, repeated `empty`) | 3 consecutive `empty` results | Companionship + clarifying question; tool benched 5 turns |
| Hallucination on neighbor data | Output rails tier-2 | Suppress claim, regenerate; on second fail, safe template + `capture_inquiry` |
| User-reported error ("that's wrong about Maria") | `report_error` chip | Apologize, reset the `neighbor_facts.fact` entry, flag session for offline review |
| Memory corruption (invalid core block or missing session) | Schema validation on load | Rebuild core block from `user_identity_claims` + last 5 `lana_messages`; if archival also missing, drop to greeting |
| Tier leak post-send (parallel-session race) | Periodic audit cron | Revoke leaked exposure, notify subject, log incident |
| Tool timeout (>2s) | Tool runner | Retryable error; router backoff-retries once; then Companionship with "give me a sec — let me try that differently" |
| Safety trigger (DV / suicidality / abuse) | Input rails tier-1 | Suppress LLM; render safety template + resources; log to incident channel |
| Persona drift (sustained low voice-consistency over 3+ turns) | Drift monitor | Reload persona prefix verbatim next turn; flag session for review |
| Stale joint moment (participant left the block) | Validation at retrieval | Discard, don't surface; log for cron tuning |

Every failure path ends in deterministic recovery — no path where Lana goes silent or freezes the UI.

---

## §11 · Phased rollout

### v0.1 · launch (Lake Nona cohort)

Single agent, 5 modules, core + archival memory, basic guardrails, `capture_inquiry` live everywhere. Persona prefix locked. Sonnet for hero moments only (joint synthesis, reveal moments · ~1 in 5 turns); Haiku for everything else. State machine: 8 states per `AI_PIPELINE_v1.md` §3, no expansion. Eval: 500-turn golden set, ≥0.85 pass per module to ship. Languages: EN/PT/ES from day one. Multi-modal: voice-in default, text-out default, TTS opt-in (per `AI_PIPELINE_v1.md` §10.5 C1). Surface-aware skills for Exchange, Activity, Map (per §10.5 C3). Cost: ~$1.21/MAU baseline, ~$0.09 per session.

### v0.2 · refinement (post-launch ~2-3 months)

Replace LLM-judge persona drift with a fine-tuned tiny classifier. Multi-block memory — neighbor facts retrievable cross-block with consent. Vouching system — explicit `vouch_for_neighbor` tool in tier-manager flows. ElevenLabs TTS on Pro tier; Hume EVI evaluation for emotional voice. Marketplace expands to services. Pattern memory surfaced in core block from session 3+.

### v0.3 · selective fine-tuning (post-launch ~6 months)

Identify any module plateauing <0.85 after prompt iteration. Build DPO dataset (top-100 good / top-100 bad Lana moments). Fine-tune Haiku → "Lana-tuned Haiku" as the routine synthesizer; reserve Sonnet for joint synthesis and reveal moments. **Multi-agent only if** one module crosses the trigger (>10 tools AND <0.85 eval AND cross-domain confusion in audit log) — Marketplace is the likely first candidate; pattern is promote to sub-agent behind a handoff tool (Anthropic supervisor pattern · `LANA_AGENT_RESEARCH_v1.md` §2 #2). Sierra-style validator wraps the persona loop for compliance review on every action.

---

## §12 · Open questions for the team

Five decisions to lock before implementation:

1. **Cron vs streaming for `jointSuggester`** — `AI_PIPELINE_v1.md` §4.6 specifies a 5-min Supabase cron. Use Postgres `LISTEN/NOTIFY` on `user_identity_claims` inserts for sub-minute detection? Trade-off: latency vs Supabase plan limits. Tommaso's lean: cron for v0.1, revisit if surfacing feels stale.
2. **OOS sentence-transformer — hosted or self-hosted?** Modal/Replicate is the path of least resistance; Supabase Edge Functions is cheaper but adds ops. Aki to estimate cost delta at 1k MAU.
3. **Llama Guard vs OpenAI Moderation API** for input rails. Llama Guard is more tunable; OpenAI is free-tier and lower-latency. Recommend: OpenAI Moderation for v0.1, evaluate Llama Guard at v0.2.
4. **Persona-drift judge model?** Haiku is the natural fit but shares lineage with the synthesizer (charity-bias risk). Consider GPT-4o-mini to avoid family-effect bias. Open: who runs the bias eval?
5. **Doctrine ↔ system prompt sync?** Compile step in CI vs runtime template assembly vs manual update. Recommend `pnpm compile-prompts` in CI on every merge to `main`, failing the build if prompts drift from `LANA_MODUS_OPERANDI.md`.

---

## §13 · References

- `AI_PIPELINE_v1.md` — pre-existing pipeline draft; this doc is its successor and supersedes any conflicting choice
- `LANA_AGENT_RESEARCH_v1.md` — deep-research compilation that grounds every choice here (cited inline)
- `LANA_MODUS_OPERANDI.md` — per-module doctrine docs (to be written; one section per capability from §5)
- `LANA_OUT_OF_SCOPE_PLAYBOOK.md` — 4-step graceful-refusal-and-capture playbook (to be written; from `LANA_AGENT_RESEARCH_v1.md` §4)
- `LANA_SIGNAL_AGGREGATOR.md` — cron + clustering spec for `inquiry_signals` (to be written; thresholds from `LANA_AGENT_RESEARCH_v1.md` §4)
- `BRAND.md` — voice and tone source of truth
- `LANA_BRAND_GUIDELINES.md` — mascot voice rubric (used by persona-drift judge)
- `STREAK_MODULE_SPEC_v1.md` — engagement substrate; the streak feeds `active_signals` in the core block
- `PWA_INVENTORY_v1.md` — current production state of Lana's surfaces
- Schema: Supabase project `rjlcyvwogmfmngemhbmn` (Phygtl org) · migrations only

---

*v1 · 2026-06-04 · pending eng review by Azjit + Aki before code execution begins.*

**Change log**
- v1 · 2026-06-04 · initial spec, supersedes architecture choices in `AI_PIPELINE_v1.md` where they conflict.
