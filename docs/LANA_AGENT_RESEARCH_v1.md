# Lana · agent architecture research (v1)

*Compiled 2026-06-04 · scope: backend-engineering decisions for Lana's 5-capability concierge inside TagAlng · ~5,400 words*

> Lana is the sheep-mascot AI concierge inside TagAlng, the block-network social app for moms in Lake Nona, FL. She handles five capabilities: **Discovery** (find people, blocks, events) · **Activity-planning** (suggest, schedule, coordinate) · **Marketplace** (intent capture for services, swaps, recommendations) · **Relationship-ladder management** (gating Stranger→Nudge→Acq→Direct→IRL transitions) · **Companionship** (warm presence, micro-rapport, check-ins).
>
> This document grounds the architectural decisions in current literature (2023–2026) and production case studies. It is written for backend engineers (Azjit, Aki) deciding how to structure Lana's runtime.

---

## §1 · Recommended pattern for Lana (TL;DR + rationale)

**Recommendation: a single Lana agent with a structured tool layer and a thin deterministic router, NOT a multi-agent system.** Use a Sierra-style "constellation of models" only for non-conversational subtasks (intent classification, content moderation, embedding-based retrieval) — never for the user-facing turn loop.

Three reasons:

1. **Voice consistency is the product.** Lana is a character, not a workflow. Multi-agent handoffs leak persona — production teams that ship companion AI (Pi, Tolan, Replika 2.0) all run a single character loop with retrieved memory and tools, because users notice "stitching" between agents within ~3 turns ([Inflection / Pi](https://hey.pi.ai/), [Tolan / OpenAI case study](https://openai.com/index/tolan/)).
2. **The 15× token tax is not earned here.** Anthropic's multi-agent research system documents that multi-agent architectures use ~15× more tokens than single-agent chats and are only economical when tasks decompose into genuinely parallel branches with bounded context — which Lana's 5 capabilities do not ([Anthropic, "How we built our multi-agent research system"](https://www.anthropic.com/engineering/multi-agent-research-system)).
3. **Five capabilities is below the multi-agent threshold.** The industry rule of thumb from LangChain/LangGraph practitioners is: stay single-agent until one agent's eval accuracy plateaus below ~85% AND you have ≥10 tools with cross-domain confusion ([Focused.io · LangGraph supervisor vs swarm](https://focused.io/lab/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture)). Lana has 5 capability surfaces and a small initial tool set; her bottleneck will be memory and grounding, not orchestration.

The architecture that follows is therefore: **one Lana loop · five capability modules expressed as tool clusters · one out-of-scope capture tool · a separate non-conversational safety/policy layer (NeMo-Guardrails-style) running on input and output.** When a capability outgrows its tool cluster (likely Marketplace first, around v0.3), promote it to a sub-agent behind a handoff tool — but only then.

---

## §2 · Multi-agent orchestration patterns

Ranked by fit for Lana. Each entry: when to use, pros, cons, exemplars.

### 1 · Single-agent-with-tools (Anthropic / Toolformer lineage) — **Lana's pattern**

One LLM loop, a tool registry, a memory layer, a system prompt that encodes persona + doctrine.

- **When:** ≤10 tools, one consistent voice, latency budget <3s/turn, conversation-shaped product (not workflow-shaped).
- **Pros:** lowest latency, easiest to debug, single eval target, persona coherence is automatic.
- **Cons:** breaks down when tool count exceeds the model's selection accuracy, hard to scale across distinct task types.
- **Exemplars:** Anthropic's Claude tool use ([building effective agents](https://www.anthropic.com/research/building-effective-agents)), Toolformer ([Schick et al. 2023](https://arxiv.org/abs/2302.04761)), Pi by Inflection, Tolan, Replika.

### 2 · Orchestrator-and-specialists (supervisor pattern)

A supervisor LLM routes each turn to one of N specialist sub-agents; specialists return to the supervisor which decides whether to keep going.

- **When:** ≥10 tools with cross-domain confusion, distinct evaluation targets per task type, you can absorb 2× LLM calls per domain.
- **Pros:** specialists can be cheaper/smaller models, independent iteration, sharper per-agent evals.
- **Cons:** ~2× call count per turn, supervisor becomes a single point of failure, persona drift across specialists, harder to maintain shared state.
- **Exemplars:** Sierra's [constellation of models](https://sierra.ai/blog/constellation-of-models), LangGraph supervisor ([docs](https://reference.langchain.com/python/langgraph-supervisor)), Anthropic's research system orchestrator/sub-agent split.

### 3 · Swarm / handoff (decentralized)

Agents hand off directly to each other via function calls, no central supervisor.

- **When:** persona-distinct multi-character experiences (handoff is the UX, e.g. "transferring you to billing"), low-latency requirements.
- **Pros:** 1 LLM call per domain after first, lower latency than supervisor.
- **Cons:** no central re-classification, error recovery is hard, agents must each know the routing map.
- **Exemplars:** OpenAI Swarm / Agents SDK handoffs, decentralized peer-to-peer mesh patterns ([Beam.ai · 6 multi-agent patterns](https://beam.ai/agentic-insights/multi-agent-orchestration-patterns-production)).

### 4 · Constellation of models (heterogeneous specialists, non-conversational)

Multiple specialized models — intent classifier, knowledge retriever, action executor, tone validator — orchestrated by code, not by an LLM. Sierra's term.

- **When:** you have a stable workflow shape and want to use small fast models for specific subtasks while keeping a frontier model for the conversational center.
- **Pros:** cost-efficient, parallelizable, each model evaluated independently.
- **Cons:** orchestration code becomes complex, latency from sequential dependencies.
- **Exemplars:** [Sierra Agent OS](https://sierra.ai/blog/constellation-of-models) (15+ models), Hume EVI's [voice/text/expression stack](https://www.hume.ai/empathic-voice-interface), Klarna's GPT-4 + RAG + escalation router via LangGraph ([LangChain blog · Klarna](https://www.langchain.com/blog/customers-klarna)).
- **Relevance to Lana:** use this *inside* the single-agent envelope — e.g. a small classifier model for out-of-scope detection runs in parallel with the main turn, but the user never sees handoffs.

### 5 · Linear chain / pipeline

Fixed sequence of agents (intent → retrieve → respond → validate). Each output feeds the next.

- **When:** deterministic workflows, compliance-heavy domains, where every turn must follow the same checks.
- **Pros:** maximum predictability, easy to audit, cheap to debug.
- **Cons:** rigid, slow for trivial queries, doesn't scale to varied user intents.
- **Exemplars:** Decagon's [AI Agent Engine](https://decagon.ai/resources/the-ai-agent-engine), NeMo Guardrails rail sequence (input → dialog → retrieval → execution → output).

### 6 · Parallel dispatch + synthesis

Fan-out subagents in parallel, a collector LLM merges results.

- **When:** research-shaped tasks, information-gathering with bounded context per branch.
- **Pros:** wall-clock parallelism, can exceed single context window.
- **Cons:** 15× token cost ([Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system)), hard to reason about, irrelevant for conversational UX.
- **Exemplars:** Anthropic's research system, AgentVerse ([Chen et al. 2023](https://arxiv.org/abs/2308.10848)).
- **Relevance to Lana:** only if we add deep-research-style flows in v0.3+ (e.g. "summarize what moms on my block did last week").

### 7 · Hierarchical multi-tier

Supervisor of supervisors. Used in 50+ agent enterprise deployments.

- **When:** very large agent fleets, organization-mirroring task hierarchies.
- **Pros:** scales beyond a single supervisor's context.
- **Cons:** complexity explosion, latency stacking, not appropriate for consumer products.
- **Exemplars:** MetaGPT ([Hong et al. 2023](https://arxiv.org/abs/2308.00352)) software-development agent teams.

### 8 · Agent + skill library (Voyager pattern)

One agent that *writes its own tools* over time, storing executable snippets in a retrievable library.

- **When:** open-ended environments where the tool set can't be enumerated upfront.
- **Pros:** unbounded capability growth, compositional skills.
- **Cons:** safety risks, harder to evaluate, requires sandboxed execution.
- **Exemplars:** Voyager ([Wang et al. 2023](https://arxiv.org/abs/2305.16291)).
- **Relevance to Lana:** not now. Could inform a v0.3 "Lana learns block-specific phrases" feature, but premature for v0.1.

**Verdict for Lana:** pattern #1 envelope, with #4 used invisibly for safety/intent/retrieval subtasks. Reserve #2 for when (not if) Marketplace outgrows a single tool cluster.

---

## §3 · Modus operandi doctrine

Every production agent system that ships well has a written contract per agent. Sierra calls it the agent's "doctrine," Decagon calls it the "operating procedure," Anthropic calls it the system-prompt-plus-tools spec. The structure converges on six sections.

### Template (use this for each of Lana's 5 capability modules)

```
# AGENT: <name>
# VERSION: <semver>
# OWNER: <eng lead>

## 1. Purpose (1 sentence)
What this agent exists to do, in the user's words.

## 2. Capabilities (tools)
- tool_name(args) → return type
- tool_name(args) → return type
For each tool: when to call · when NOT to call · failure mode.

## 3. Out-of-scope (the "not for me" list)
Enumerate inquiries that look in-scope but aren't. Each entry:
- pattern (regex or semantic description)
- response shape (graceful refusal + capture)
- escalation target (other agent, human, capture tool)

## 4. Edge case taxonomy
- ambiguous input → clarify how
- missing data → fallback behavior
- contradictory user goals → resolution rule
- safety triggers → who handles, what user sees

## 5. Success metrics
- per-turn: groundedness, on-topic rate
- per-session: task completion, user satisfaction proxy
- per-capability: signal volume, escalation rate

## 6. Voice constraints
- 3 must-do phrases / patterns
- 3 banned phrases / patterns
- tone anchors (link to LANA_BRAND_GUIDELINES.md)
```

This is grounded in **Agent Contracts** ([arXiv 2601.08815](https://arxiv.org/abs/2601.08815)), a formal framework that extends design-by-contract thinking to LLM agents: pre-conditions, post-conditions, resource bounds, and success criteria as one coherent governance object. **AgentSpec** ([arXiv 2503.18666](https://arxiv.org/abs/2503.18666)) and the **DbC-inspired neurosymbolic layer** ([arXiv 2508.03665](https://arxiv.org/abs/2508.03665)) extend this to runtime enforcement — rules with triggering events, predicate conditions, and enforcement actions.

### Sample: Marketplace agent contract (skeleton)

```
# AGENT: lana.marketplace
# VERSION: 0.1.0
# OWNER: aki

## 1. Purpose
Help a mom find a service, swap, or recommendation from her block-network
— or capture the inquiry if no match exists yet.

## 2. Capabilities
- search_block_offers(block_id, category) → [Offer]
  · call when: user names a service category Lana has indexed
  · don't call when: query is fuzzy ("anyone good?") — clarify first
  · failure: empty result → DO NOT say "no results" → invoke capture_inquiry
- capture_inquiry(category, free_text, urgency) → InquiryID
  · call when: in-scope category but no match, OR out-of-scope but
    semantically nearby (rent, nails, tutoring, etc.)
  · always confirm capture aloud: "I'll let our team know"
- recommend_neighbor(need_vector) → [Person]
  · gated by relationship_ladder · never expose Direct-tier without consent

## 3. Out-of-scope
- pattern: real estate ("rent a house", "lease", "buy a home")
  response: "That's not something I broker yet — but I'll note it.
  Mind if I ask what you're looking for?" → capture_inquiry
- pattern: paid medical/legal services
  response: hard route to human moderation queue
- pattern: childcare for hire (sensitive)
  response: defer to relationship_ladder gating + safety review

## 4. Edge cases
- two needs in one turn → handle most-recent, queue other
- price negotiation → out of scope, defer to in-app messaging
- safety red flags (violence, abuse keywords) → safety layer takes over

## 5. Success metrics
- match rate (matched / total search calls) target ≥ 60%
- capture rate (captured / no-match) target ≥ 95% — every miss should
  become a signal, not a dead end
- inquiry → signal latency: <60s end-to-end
- weekly capture volume → reviewed by product

## 6. Voice
must-do: name the underlying need · acknowledge time pressure · cite
the block ("a few moms on Whisper Park have...")
banned: "I can't do that" · "I don't have access to" · "unfortunately"
```

Sierra and Decagon both publish that their agents are built from these contracts ("modular task abstractions that isolate responsibilities" per Sierra; "operating procedures loaded with knowledge and actions" per Decagon). The contract is the eval surface — when you fix a bug, you fix the contract, then the prompt, then re-evaluate.

---

## §4 · Out-of-scope inquiry pattern · the graceful-refusal-and-capture playbook

This is the most under-documented but highest-leverage pattern for Lana. The literature is converging on a 4-step structure.

### Why it matters

Research on guardrails ([arXiv 2506.00195 · "Let Them Down Easy"](https://arxiv.org/abs/2506.00195)) shows **partial compliance — providing general engagement without actionable details — reduces negative user perceptions by over 50% compared to flat-out refusals**. Refusals unfold in five phases: expectation formation → intent recognition → refusal framing → resource provision → post-refusal outcomes. Each phase is a UX opportunity.

Separately, intent classification research ([arXiv 2507.22289](https://arxiv.org/abs/2507.22289), [arXiv 2507.01541](https://arxiv.org/abs/2507.01541)) treats out-of-scope (OOS) detection as a first-class signal: hybrid sentence-transformer + LLM approaches close the OOS accuracy gap to ~2% while cutting latency 50%. The point is that OOS is not failure — it is data.

### The 4-step Lana playbook

**Step 1 — Acknowledge the underlying need, not the literal request.**

User: *"I'm looking to rent a house in Lake Nona."*
Lana (bad): *"I can't help with rentals."*
Lana (good): *"That's a big move — Lake Nona has gotten popular fast. What's the trigger — outgrowing your space, or moving closer to a school?"*

Grounded in Reeves & Nass's CASA framework: people respond to acknowledgment of need the way they respond to a friend asking a follow-up, not the way they respond to a kiosk.

**Step 2 — Engage with the underlying need (1–2 turns).**

Don't just capture and bail. Demonstrate the agent *understood*. This builds reciprocity (Altman & Taylor's social penetration theory, [Springer 2024 lit review](https://link.springer.com/article/10.1007/s00779-024-01823-7)): when Lana shows interest, users self-disclose more, which makes the capture richer.

**Step 3 — Surface the constraint with warmth and capture explicitly.**

Lana: *"I don't broker rentals yet — but I'll flag this for our team. A few other moms have asked. Want me to ping you if we line something up?"*

Three things happen here:
- The constraint is named (not hidden).
- The user is told their inquiry has weight ("a few other moms").
- An explicit opt-in for follow-up is offered.

**Step 4 — Persist the structured signal.**

Behind the scenes, Lana calls `capture_inquiry(category, free_text, urgency, opt_in_followup, user_id, block_id, embedding)`. This is the part that turns conversation into product roadmap intelligence.

### Data model for capture (recommended)

```sql
create table inquiry_signals (
  id uuid primary key,
  user_id uuid references users(id),
  block_id uuid references blocks(id),
  captured_at timestamptz default now(),
  category text not null,              -- 'rental', 'nails', 'tutor', etc.
  free_text text not null,             -- verbatim user phrasing
  embedding vector(1536),              -- pgvector for clustering
  urgency text,                        -- 'now', 'this_week', 'someday'
  opt_in_followup boolean default false,
  source_agent text default 'lana.v0.1',
  cluster_id uuid,                     -- populated by nightly job
  status text default 'open'           -- 'open', 'addressed', 'launched'
);

create index on inquiry_signals using ivfflat (embedding vector_cosine_ops);
create index on inquiry_signals (category, block_id);
```

### Signal aggregation thresholds

Patterns that surface in production AI products (Inkeep, Decagon, Sierra):

- **N=3 same block, same category in 7 days** → notify product, surface as "validated unmet need"
- **N=10 same category across blocks in 14 days** → roadmap candidate
- **Embedding cluster of >20 free-text entries with <0.3 cosine distance** → emerging category alert
- **Per-user repeat inquiry** → trigger Lana to acknowledge memory ("you asked about this last week — still relevant?")

### Production examples to study

- **Inkeep** publishes that capability-gap analytics from unanswered queries are the primary input to their support agent's knowledge-base prioritization ([Inkeep blog](https://inkeep.com/blog/agent-frameworks-platforms-overview)).
- **Decagon** describes a "data flywheel" where every conversation, including non-resolvable ones, feeds back into intent detection and prompt grounding ([Decagon · AI Agent Engine](https://decagon.ai/resources/the-ai-agent-engine)).
- **Sierra**'s validator agents check policy compliance on every action — out-of-scope is just one policy class ([Sierra · constellation](https://sierra.ai/blog/constellation-of-models)).

The lesson: out-of-scope handling is not a corner case. It is the data engine. Lana should be designed with the assumption that **~30–40% of v0.1 inquiries will be out-of-scope** (Lake Nona moms will ask Lana for things we haven't built yet) and that this is the *most valuable* data she produces.

---

## §5 · Communication structure best practices

Twelve rules, each grounded in research or production case study.

1. **Reciprocate self-disclosure proportionally.** When the user shares something personal, Lana acknowledges with one personal-flavored line ("I love that — sounds like a real morning"), not a probing question. Over-reciprocation reads as fake. Source: [Springer 2024 self-disclosure literature review](https://link.springer.com/article/10.1007/s00779-024-01823-7), Altman & Taylor social penetration theory.

2. **Take turns; don't soliloquize.** A single Lana turn should be 1–3 sentences in the conversational tiers (Stranger, Nudge); slightly longer (3–5) in Direct. Grounded in Clark & Brennan grounding theory: long turns prevent the user from establishing common ground. Source: [Frontiers · dialogue repair in virtual assistants](https://www.frontiersin.org/articles/10.3389/frobt.2024.1356847/full).

3. **Repair, don't double down.** When Lana misunderstands, the next turn must acknowledge the miss before retrying. Pattern: *"Oh — I read that wrong. You meant <restatement>?"* Source: [arXiv 2503.13975 · Navigating Rifts in Human-LLM Grounding](https://arxiv.org/abs/2503.13975), [Liao et al. CHI 2019 · Resilient Chatbots](http://qveraliao.com/chi19-1.pdf).

4. **"I don't know" is honest; "I don't have access to" is jargon.** When grounding fails, say so plainly. Avoid backstage language. Source: Anthropic guidance on [writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents).

5. **One question per turn, max.** Multi-question turns degrade response quality on mobile and overload the user. Pi follows this rule rigorously; Tolan rebuilds context per turn partly to avoid stacking questions.

6. **Anchor to the block.** Lana is verticalized to a geographic neighborhood. Reference it. ("On Whisper Park lately..." / "A few moms two blocks over...") This is brand-specific but maps to the "verticalized agents are concierges of a domain" principle ([Sendbird · vertical AI agents](https://sendbird.com/blog/what-is-an-ai-agent/vertical-ai-agents)).

7. **Use the user's name sparingly, not every turn.** Over-naming feels like a phishing email. Industry-standard: 1 name use per ~5–7 turns max in companion contexts.

8. **Voice consistency >> response length.** Pi's design philosophy is "the most present, not the smartest." A short on-voice reply beats a long generic one. Source: [Inflection · Pi design](https://hey.pi.ai/).

9. **Capture before pivoting.** If you can't help, capture the structured signal *before* trying to redirect. Otherwise the redirect feels like dismissal.

10. **Never expose the agent boundary.** Don't say "let me hand you off to..." unless the handoff is the UX. Internally orchestrate; externally one voice. Source: Tolan's [rebuild-context-each-turn](https://openai.com/index/tolan/) architecture exists specifically to keep voice coherent across switches.

11. **Use cues from the relationship tier.** Stranger-tier responses are more formal and bounded; Direct-tier can reference shared history. This maps to the 5-tier ladder defined in MEMORY. Don't break tier without an event-driven trigger (intro accepted, IRL met).

12. **Show, don't claim, empathy.** Don't say "I understand how you feel." Acknowledge the specific. ("That's a lot to hold while running between school pickups.") Source: research on chatbot reciprocal self-disclosure ([AODR · "I'm Hurt Too"](https://aodr.org/_PR/view/?aidx=38536&bidx=3479)).

---

## §6 · Success metrics

Three levels.

### Per-turn metrics

| Metric | Definition | Target (v0.1) | Source |
|---|---|---|---|
| Groundedness | % of factual claims supported by retrieved context / tool output | ≥ 0.9 | [deepset · groundedness](https://www.deepset.ai/blog/rag-llm-evaluation-groundedness) |
| On-topic rate | % turns that stay on user's last intent | ≥ 0.95 | [Confident AI · agent eval](https://www.confident-ai.com/blog/llm-agent-evaluation-complete-guide) |
| Refusal-without-capture rate | % refusals that don't trigger `capture_inquiry` | ≤ 5% | derived from [arXiv 2506.00195](https://arxiv.org/abs/2506.00195) |
| Tool call accuracy | % tool selections that match a human-labeled correct tool | ≥ 0.9 | [τ-Voice benchmark](https://arxiv.org/abs/2603.13686) |
| Turn latency p50 / p95 | time from user send → first Lana token | <1.5s / <3s | Tolan reports 0.7s reduction was "noticeable" |
| Voice-consistency score | LLM-judge rating against persona rubric | ≥ 4/5 | Sierra validator pattern |

### Per-session metrics

| Metric | Definition | Target (v0.1) |
|---|---|---|
| Task completion rate (TSR) | % sessions where stated user intent resolved | ≥ 60% (high for a v0.1) |
| Goal-shift recovery time | turns to re-orient when user changes intent | ≤ 2 |
| Session containment | % sessions resolved without human escalation | ≥ 80% |
| Self-disclosure depth | LLM-judge score on Altman-Taylor breadth/depth | trend up week-over-week |
| Repair frequency | repair-turns / total-turns | ≤ 10% |
| Return rate (7-day) | % users who return within 7 days | ≥ 40% (companionship benchmark) |

Sources: [AgentChangeBench](https://arxiv.org/abs/2510.18170) for goal-shift, [Hamming AI](https://hamming.ai/resources/voice-agent-evaluation-metrics-guide) for voice agent benchmarks, [MS Copilot Studio analytics docs](https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-improve-agent-effectiveness).

### Per-capability (per agent module) metrics

| Capability | Primary metric | Secondary metric |
|---|---|---|
| Discovery | match precision @ top-5 | search-to-action rate |
| Activity-planning | plan completion rate | invite acceptance rate |
| Marketplace | match rate · capture rate · signal-to-roadmap latency | weekly captured-inquiry volume |
| Relationship-ladder | tier-transition acceptance rate | inappropriate tier-skip rate (target: 0) |
| Companionship | session length · 7-day return · disclosure depth | sentiment trend |

**System-wide KPIs** (the "trust your agent" framing Sierra promotes):

- **Eval-set pass rate**: golden set of 100 hand-curated turns per capability, scored weekly.
- **Production drift alarm**: if any per-turn metric drops >10% week-over-week, trigger review.
- **Reliability SLO**: 99.5% uptime per [Growth Hakka · AI agent KPIs](https://www.growthhakka.co.uk/2026/06/03/ai-agent-kpis-essential-performance-benchmarks/).

---

## §7 · Research papers · annotated bibliography

Twelve papers, ordered roughly by relevance to Lana's architecture decisions.

1. **Anthropic (2024) · "Building Effective Agents"** — [link](https://www.anthropic.com/research/building-effective-agents). The most-cited industry text on when to use workflows vs agents. The single most important takeaway for Lana: start with the simplest pattern that works, escalate complexity only when evals demand it.

2. **Anthropic (2025) · "How we built our multi-agent research system"** — [link](https://www.anthropic.com/engineering/multi-agent-research-system). Concrete numbers on the 15× token cost of multi-agent, and a decision rubric for when parallelism earns it. Our negative case for going multi-agent.

3. **Yao et al. (2023) · "ReAct: Synergizing Reasoning and Acting in Language Models"** — [arXiv 2210.03629](https://arxiv.org/abs/2210.03629). The foundational pattern for interleaving thought and tool use. Lana's loop is fundamentally a ReAct loop.

4. **Shinn & Labash (2023) · "Reflexion: Language Agents with Verbal Reinforcement Learning"** — [OpenReview link](https://openreview.net/pdf?id=vAElhFcKW6). Self-reflection over previous turns to improve subsequent ones. Useful for Lana's relationship-ladder agent: reflect on whether previous tier transitions worked before proposing the next.

5. **Schick et al. (2023) · "Toolformer: Language Models Can Teach Themselves to Use Tools"** — [arXiv 2302.04761](https://arxiv.org/abs/2302.04761). Establishes that an LLM can learn when to invoke external APIs. Underpins single-agent-with-tools.

6. **Packer et al. (2023) · "MemGPT: Towards LLMs as Operating Systems"** — now Letta — [Letta docs](https://docs.letta.com/concepts/letta/). Memory hierarchy (in-context working memory + archival memory + external store). Direct relevance: Lana needs both working memory ("Sarah asked about a tutor 3 days ago") and archival memory ("Sarah lives on Whisper Park, has a 4-year-old, runs early") with controlled paging.

7. **Hong et al. (2023) · "MetaGPT: Meta Programming for a Multi-Agent Collaborative Framework"** — [arXiv 2308.00352](https://arxiv.org/abs/2308.00352). Standardized operating procedures (SOPs) as the contract between agents. Source for the doctrine template in §3.

8. **Wu et al. (2023) · "AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversation"** — [arXiv 2308.08155](https://arxiv.org/abs/2308.08155). Conversable agents with mixed LLM / human / tool participation. Reference for v0.2+ when we may need human-in-the-loop escalation flows.

9. **Wang et al. (2023) · "Voyager: An Open-Ended Embodied Agent with LLMs"** — [arXiv 2305.16291](https://arxiv.org/abs/2305.16291). Skill library + automatic curriculum + iterative prompting. Relevance: deferred, but the skill-library pattern is how Lana could learn block-specific colloquialisms over time.

10. **Zhu et al. (2025) · "MultiAgentBench: Evaluating the Collaboration and Competition of LLM Agents"** — [arXiv 2503.01935](https://arxiv.org/abs/2503.01935). Milestone-based KPIs beyond task completion, including coordination quality. Methodology source for §6.

11. **Liu et al. (2025) · "Intent Recognition and Out-of-Scope Detection using LLMs in Multi-party Conversations"** — [arXiv 2507.22289](https://arxiv.org/abs/2507.22289). Hybrid intent + OOS detection methodology. Directly informs Lana's out-of-scope module.

12. **"Let Them Down Easy! Contextual Effects of LLM Guardrails on User Perceptions"** (2025) — [arXiv 2506.00195](https://arxiv.org/abs/2506.00195). Empirical evidence that partial compliance + capture beats flat refusal by 50%+ on user satisfaction. Foundational for §4.

**Supporting reads** (not in the core 12 but cited):

- Toolformer-lineage tool use → [Anthropic · writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- Agent contracts formal framework → [arXiv 2601.08815](https://arxiv.org/abs/2601.08815)
- AgentSpec runtime enforcement → [arXiv 2503.18666](https://arxiv.org/abs/2503.18666)
- Reeves & Nass CASA → [Grokipedia · grounding in communication](https://grokipedia.com/page/grounding_in_communication)
- Self-disclosure to AI → [ScienceDirect 2025](https://www.sciencedirect.com/science/article/pii/S2949882125000647)
- Dialogue repair → [Frontiers 2024](https://www.frontiersin.org/articles/10.3389/frobt.2024.1356847/full)

---

## §8 · Production benchmarks

Eight verticalized AI agent products, architecture inferred from public sources.

### Pi (Inflection AI)

- **Pattern:** single empathetic agent, persistent cross-session memory, no exposed tools.
- **Architecture inference:** one frontier model + memory store + persona-heavy system prompt. No multi-agent. Optimized for EQ over IQ.
- **Lana takeaway:** companion AI ships as one voice. Don't fragment Lana into specialists where the user can perceive the seam.
- Source: [Pi product page](https://hey.pi.ai/), [Microsoft Azure case study](https://www.microsoft.com/en/customers/story/1666598146786087377-inflection-ai-partner-professional-services-azure-ai-infrastructure).

### Tolan (Portola)

- **Pattern:** voice-first companion, single agent, context rebuilt per turn from memory store, persona scaffolds layered in.
- **Architecture inference:** GPT-5.1 Responses API + vector memory + dynamic emotional adjustment. Notable: they *rebuild* context every turn rather than caching, to preserve personality.
- **Lana takeaway:** stateless turn loops + stateful memory store is a viable production pattern at ≥100k paying users. Memory is the asset, not the conversation history.
- Source: [OpenAI case study](https://openai.com/index/tolan/), [Homebrew investor brief](https://homebrew.co/blog/2025/07/08/building-a-different-type-of-ai-companion-tolan-developer-portola-raises-usd20-million-series-a).

### Hume EVI

- **Pattern:** voice-to-voice with emotional expression measurement orthogonal to text. Speech-language model handles tone simultaneously with content.
- **Architecture inference:** STT + expression-measure extractor + LLM (any of Claude/GPT/Gemini) + emotion-aware TTS. The constellation is heterogeneous models, but presented as one persona.
- **Lana takeaway:** for v0.2+ voice mode, this is the pattern. Don't sequentialize tone after text — model them together.
- Source: [Hume EVI docs](https://dev.hume.ai/docs/speech-to-speech-evi/overview).

### Sierra

- **Pattern:** constellation of 15+ specialized models. Planner → Executor → Validator agents, orchestrated by code.
- **Architecture inference:** intent classification model · knowledge retrieval model · action execution model · tone validator. Each invocation routes by job-to-be-done.
- **Lana takeaway:** when we mature to v0.3+, we can adopt the planner/executor/validator split *behind* the single Lana persona. The user shouldn't see the validator.
- Source: [Sierra · constellation of models](https://sierra.ai/blog/constellation-of-models).

### Decagon

- **Pattern:** AI Agent Engine — five interconnected components forming a data flywheel. Single "brain" per enterprise, loaded with knowledge + workflows + actions.
- **Architecture inference:** Core agent + RAG + action layer + analytics + continuous improvement loop. Single conversational front; specialized back.
- **Lana takeaway:** the data flywheel framing — every conversation, including unresolved ones, feeds back — is exactly what Lana's `inquiry_signals` table is for.
- Source: [Decagon · AI Agent Engine](https://decagon.ai/resources/the-ai-agent-engine).

### Klarna (with OpenAI)

- **Pattern:** GPT-4-class agent + LangGraph routing + RAG over help center + backend API tools. 2.3M chats in 30 days = 700 agents' worth.
- **Architecture inference:** LangGraph supervisor routing between FAQ-answering, account-reading, and action-taking sub-agents. Low-confidence → human.
- **Lana takeaway:** confidence-based escalation is a must. Klarna walked back AI-only claims by 2025 and re-introduced humans — Lana should have a clean human-escalation path from day one.
- Source: [OpenAI · Klarna case study](https://openai.com/index/klarna/), [LangChain blog · Klarna](https://www.langchain.com/blog/customers-klarna).

### Inkeep

- **Pattern:** RAG-first agent + tool use + visual builder. Hybrid no-code + SDK.
- **Architecture inference:** knowledge-aware agent with retrieval augmentation + multi-agent collaboration optional + human escalation. Capability-gap analytics built into the product.
- **Lana takeaway:** the "capability-gap" analytic surface is a productized version of Lana's inquiry-signal table. Use Inkeep's UX as a model for how to present captured signals to the team.
- Source: [Inkeep · agent frameworks blog](https://inkeep.com/blog/agent-frameworks-platforms-overview).

### Letta

- **Pattern:** stateful agents with editable memory blocks, MemGPT-style memory hierarchy, REST-API-deployable.
- **Architecture inference:** the framework itself is the architecture — context window as RAM, archival store as disk, the model manages paging via tool calls.
- **Lana takeaway:** even if we don't adopt Letta, we should steal the model. Lana's memory should have **(a) core block** (always in context: name, block, ladder tier, last-talked-about) and **(b) archival store** (paged in on demand: past conversations, captured inquiries, neighbor list).
- Source: [Letta · agent memory](https://www.letta.com/blog/agent-memory), [Letta · rearchitecting the agent loop](https://www.letta.com/blog/letta-v1-agent).

### Replika 2.0

- **Pattern:** rebuilt memory architecture in April 2026 release. Tracks emotional patterns + facts. Single-agent loop with 3D avatar.
- **Lana takeaway:** the public lesson from Replika 1→2 is that "remember the facts" is not enough — pattern-level memory (how you talk when you're stressed, what topics you return to) is the harder, more valuable signal. Build for this in the schema from day one.
- Source: [RoboRhythms · Replika 2.0](https://www.roborhythms.com/replika-2-0-explained/).

---

## §9 · Recommendations for Lana (TagAlng backend team)

Seven concrete actions, prioritized.

**1. Build Lana as a single agent with five capability modules expressed as tool clusters.**
Module = a contract + a tool subset + an out-of-scope list. The user perceives one Lana; the router perceives five modules. Use LangGraph or the Claude Agent SDK as the runtime — both support this pattern natively. Do not adopt a multi-agent framework yet.

**2. Make `capture_inquiry` a first-class tool, not an afterthought.**
Wire it into every module's out-of-scope branch. Persist to `inquiry_signals` with embedding, category, free-text verbatim, urgency, opt-in flag. Build a dashboard for it before launch — moderating captured signals is the product-discovery loop for v0.2.

**3. Adopt a two-tier memory model (MemGPT/Letta-style) from v0.1.**
Core block (always in context: user id, block, ladder tier, last 3 turns, last topic, last captured-inquiry id). Archival store (pgvector + Postgres tables): past sessions, captured inquiries, neighbor graph, relationship history. Lana invokes archival retrieval as a tool, not as a cache.

**4. Run NeMo-Guardrails-style input/output rails OUTSIDE the persona loop.**
Five-rail layer: input moderation → topic/policy → retrieval grounding → execution policy (e.g. no exposing Direct-tier without consent) → output moderation. Use small specialized models (Llama Guard, content classifiers) for each. The user never talks to these — they wrap Lana.

**5. Define the doctrine document per module in version-controlled markdown, NOT in the system prompt directly.**
The system prompt is a compiled artifact derived from the doctrine + tool schemas + a few-shot eval set. When you fix a bug, fix the doctrine, regenerate the prompt, re-run the golden eval. This is how Sierra, Decagon, and Anthropic's research team all work.

**6. Instrument from day one — per-turn, per-session, per-module metrics.**
Use Langfuse or PromptLayer for tracing. Hand-curate a 100-turn golden eval set per module (500 total) before launch. Run it weekly. Alert on >10% drift on any per-turn metric. This costs ~$200/mo and saves a v0.2 reset.

**7. Plan the v0.2 escalation paths now, even if you don't build them.**
Write down: (a) when Marketplace becomes its own sub-agent (likely first), (b) when voice mode adopts Hume-style emotional channel, (c) when a Sierra-style validator wraps the persona loop for compliance review. Sketch these in the doctrine document under "future modules" so the v0.1 architecture doesn't paint us into a corner.

**Sequencing for the engineering team:**
- Week 1 (Azjit + Aki): doctrine docs for 5 modules + `inquiry_signals` schema + capture_inquiry tool.
- Week 2 (Aki): two-tier memory model + archival retrieval tool.
- Week 3 (Aki + Tommaso): golden eval set per module + Langfuse instrumentation.
- Week 4 (Azjit): NeMo-style rails layer + Llama-Guard integration.
- Launch readiness gate: 0.85+ on all golden evals · refusal-without-capture rate ≤5% · turn latency p95 <3s.

---

*v1 · 2026-06-04 · compiled from ~25 web sources + cited papers. To extend: re-run §7 bibliography against 2026-Q3 arXiv pulls; revisit §8 production benchmarks quarterly (Sierra/Decagon publish architecture posts roughly every 4 months).*
