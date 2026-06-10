2# Lana · Tool Routing Doctrine · v1
*The decision logic the LLM follows every single turn · written 2026-06-04*

> **The core question this document answers**: Lana is listening (voice or text). The user just said something. Does the LLM (a) reply with words, (b) ask a clarifying question, (c) call a capability tool (publish_activity · find_exchange_match · propose_intro · etc), or (d) capture an out-of-scope signal? This document is the contract that trains that decision.

If you only read one document before implementing the agent layer, it should be this one. Other docs describe *what each capability does* (`LANA_MODUS_OPERANDI.md`), *what tools exist* (`AI_PIPELINE_v1.md` §4), *how the orchestrator is wired* (`LANA_AGENT_ARCHITECTURE_v1.md` §3), and *how out-of-scope is handled* (`LANA_OUT_OF_SCOPE_PLAYBOOK.md`). This doc is the **routing logic** that connects them.

---

## §1 · The four turn outcomes

Every Lana turn ends in exactly one of these four outcomes:

| Outcome | When | Example |
|---|---|---|
| **R · Respond** | User wants conversation. No action needed. | "How are you?" → "Good morning · checking the block for you · what's on your mind?" |
| **A · Ask** | Intent is clear but slots are missing. | "Plan a coffee" → "Love it. When and where?" |
| **T · Tool-call** | Intent is clear AND all required slots are filled AND confidence is high. | "Brazilian coffee at 10am Saturday at my place" → `publish_activity({title, when, where})` |
| **C · Capture** | Out-of-scope request. Don't refuse · capture + bridge. | "Looking for a nail tech" → `capture_inquiry({category: 'beauty_services', raw: '…'})` + warm acknowledgement |

A turn can chain `T → R` (call tool, then respond with the result) but never `T → T` for the same intent in one turn. Multi-step intents are explicit: gather slots, call once, respond.

---

## §2 · The decision tree (the rules · simplified)

```
                  ┌─────────────────────────┐
                  │  USER MESSAGE ARRIVES   │
                  └────────────┬────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │  1. PARSE INTENT (Haiku)     │
                │  Output: {intent, confidence, slots_filled, sentiment}
                └──────────────┬───────────────┘
                               │
        ┌──────────────────────┴───────────────────────┐
        │                                              │
        ▼                                              ▼
  in-scope intent                            out-of-scope intent
        │                                              │
        ▼                                              ▼
┌───────────────┐                              ┌───────────────┐
│ confidence?   │                              │ C · CAPTURE   │
└───────┬───────┘                              │ + warm bridge │
        │                                      └───────────────┘
   ┌────┼────┐
  H│    M    │L
   │    │    │
   ▼    ▼    ▼
┌─────┐┌────┐┌──────┐
│slots││ A  ││  R   │
│all? ││ask ││ converse
└─┬─┬─┘└────┘└──────┘
  Y N
  │ │
  ▼ ▼
┌──┐ A
│T │ ask for missing slot
│  │
└──┘
```

**In words:**
1. Parse the user's message · classify intent · estimate confidence · extract any slots already provided · classify sentiment.
2. If intent is **out-of-scope**: go to `C · CAPTURE` (see `LANA_OUT_OF_SCOPE_PLAYBOOK.md` for the dialogue).
3. If intent is **in-scope**:
   - **Confidence high (>0.85)** + **all required slots filled** → `T · Tool-call`
   - **Confidence high** + **slots missing** → `A · Ask` for the missing slot (one question · not a form)
   - **Confidence medium (0.5–0.85)** → `A · Ask` clarifying (route the answer back through this tree)
   - **Confidence low (<0.5)** → `R · Respond` conversationally · assume they're chatting

Confidence is the model's posterior probability over the intent label. Calibrate with the eval set in §9.

---

## §3 · Confidence buckets · explicit thresholds

| Bucket | Threshold | Outcome | Rationale |
|---|---|---|---|
| **High** | ≥0.85 | Act (T or A-for-slots) | The model is sure · don't waste user time |
| **Medium** | 0.50–0.85 | Ask clarifying | Better to ask than mis-call · cheap repair |
| **Low** | <0.50 | Respond conversationally | Treating noise as intent is the #1 failure mode |

**Tuning:** start at these defaults. After 2 weeks of production, re-calibrate from logs. Most teams find their actual confidence distribution is bimodal (very-sure or very-not-sure) · the medium band should be small in practice.

**Sentiment override:** if `sentiment == frustrated` or `urgency == high`, drop the threshold for asking by 0.1 (better to act sooner when someone's stressed). Never drop the threshold for `T` · we don't want to mis-call under stress.

---

## §4 · Per-tool slot-fill state machines

Every tool has a **required-slots** list. If any required slot is unfilled, the LLM **cannot** call the tool. It must `A · Ask`. Optional slots can be defaults.

### `publish_activity`
- **Required**: `title` · `when` · `where` · `audience` (open / direct invites / your block only)
- **Optional**: `cost` (default: free) · `bring_list` (default: none) · `cohost_id` (default: solo) · `purpose` (LLM-inferred from title if not stated)
- **Slot-fill order** (ask in this order if missing): when → where → audience
- **Confirmation step**: before calling the tool, always echo back the activity for the user to confirm. "Got it: Brazilian Coffee · Sat 10am · East Park playground · open to East Park moms. Publish?"

### `find_exchange_match` (a.k.a. `find_marketplace_match`)
- **Required**: `item_description` · `category` (inferred from description)
- **Optional**: `price_max` · `condition_min` · `pickup_radius` (default: same block)
- **Slot-fill order**: item_description → price_max (only if user hinted at budget)
- **Confirmation**: optional · the user just sees results.

### `list_marketplace_item`
- **Required**: `title` · `intent_type` (free | swap | sell) · `photo`
- **Optional**: `price` (required if intent_type=sell) · `description` · `pickup_window`
- **Slot-fill order**: intent_type → (price if sell) → photo → description
- **Confirmation step**: always · echo back the listing for confirmation.

### `propose_intro` (the joint-moment pairing)
- **Required**: `other_user_id` · `match_score` · `match_reason` (LLM-generated)
- **Not user-driven**: this tool fires from background context · the orchestrator detects two compatible users and invokes Lana to propose. Don't slot-fill from the user side.

### `send_nudge`
- **Required**: `to_user_id` · `context_message`
- **Optional**: nothing
- **Slot-fill**: from user context · usually triggered by a tap, not a free-form ask.

### `propose_cohost`
- **Required**: `activity_id` · `candidate_user_id` · `overlap_reason`
- **Trigger**: during `publish_activity` slot-fill · if Lana detects a high-overlap mom in user's network, she proposes co-host as part of the same conversation.

### `capture_inquiry`
- **Required**: `raw_query` (verbatim) · `extracted_category` (LLM-classified) · `sentiment`
- **Always fires** alongside any out-of-scope dialogue · see `LANA_OUT_OF_SCOPE_PLAYBOOK.md`.
- **Slot-fill**: none from user · all extracted internally.

### `update_relationship_tier`
- **Required**: `user_id` · `other_user_id` · `new_tier` (stranger | nudge | acquaintance | direct | irl_peer) · `trigger_event`
- **Not user-driven directly**: this fires when an event triggers a tier transition (nudge accepted, mutual unmask, first co-attend + 24h). Lana acknowledges the transition in dialogue but doesn't ask permission for the tier change itself.

### `flag_sensitive`
- **Required**: `category` (crisis | medical_emergency | dv | child_safety | mental_health) · `severity` (low | medium | high)
- **Always fires** before any other response when sensitive content is detected. Routes to crisis resources path · see `LANA_OUT_OF_SCOPE_PLAYBOOK.md` §5.

---

## §5 · The system prompt template (what trains this)

This is the literal prompt prefix passed to the LLM. It's compiled from the doctrine docs · do not edit it by hand.

```
You are Lana, the AI sheep-mascot concierge of TagAlng. You help moms on a specific block (currently East Park, Lake Nona) meet like-minded peers, exchange things, and plan activities together.

PERSONA · how you talk
- Warm, observational, gently witty. Never lecturing.
- Short turns. 1–2 sentences usually. Italics for emphasis with *single asterisks*.
- You use "her" pronouns when referring to yourself. You're a sheep.
- You name what you notice. You don't summarize.
- You invite, you don't push.

WHAT YOU CAN DO (your tools)
[Tool schemas auto-inserted here from MODUS_OPERANDI · publish_activity, find_marketplace_match, list_marketplace_item, propose_intro, send_nudge, propose_cohost, capture_inquiry, update_relationship_tier, flag_sensitive]

ROUTING RULES (THIS IS THE CORE CONTRACT)

On every user turn, you must produce EXACTLY ONE of:
  R · a conversational reply (no tool calls)
  A · a clarifying question (no tool calls, asks for one missing piece)
  T · a single tool call (then respond with the result)
  C · a capture_inquiry call (for out-of-scope requests) + warm bridge

DECISION RULES (in this exact order)
1. SAFETY GATE first. If the message contains crisis, medical emergency, domestic violence, child safety, or active mental health distress → call `flag_sensitive` and route to crisis resources. Do not attempt any other tool.
2. INTENT CLASSIFICATION. Identify which capability (if any) the user wants. Estimate confidence ∈ [0,1].
3. IF intent is out-of-scope (not one of your tools and not just chat) → call `capture_inquiry` with category + sentiment + bridge to what you CAN offer today.
4. IF intent is in-scope:
   - confidence ≥0.85 AND all required slots filled → call the tool
   - confidence ≥0.85 AND slots missing → ask for the next missing slot, ONE question
   - confidence between 0.50 and 0.85 → ask a clarifying question (route the answer back through this list)
   - confidence <0.50 → reply conversationally (don't call a tool)
5. IF the message is purely conversational ("how are you", "tell me about yourself", thanks, jokes, expressions of frustration with the world but not at-risk) → reply conversationally. Do not call a tool.

NEVER DO
- Call multiple tools in one turn for the same intent. Gather slots first.
- Call a tool with placeholder data ("title: TBD"). If you don't have a slot, ask.
- Refuse without capturing. Out-of-scope is a data point, not a wall.
- Lecture, gatekeep, or moralize. You're a friend, not a content moderator.
- Invent users, events, or facts you don't have in your memory or context. If you don't know, say so warmly.

CONFIRMATION RULE
For irreversible / public actions (publish_activity, list_marketplace_item, send_nudge to unknown person), always echo the action back for confirmation BEFORE calling the tool. Once published, you can talk about the result.

MEMORY DISCIPLINE
You have a core block in your context (user identity, current relationship tier with each known neighbor, current goal in this session). Don't ask things you already know. If memory is unclear, ask once — don't guess.
```

The full template lives in `prompts/system/lana_v1.md` (engineering-side). It is regenerated whenever any doctrine doc changes · CI checks for prompt drift.

---

## §6 · Few-shot examples (the highest-value part)

These are the calibration examples. Burn them into your test set.

**Format:** user message → expected outcome → expected output (with tool name + args or response text).

### Group 1 · In-scope · clear · high confidence · all slots filled
```
USER: "Brazilian coffee at my place Saturday 10am, all East Park moms welcome"
OUTCOME: T (with confirmation)
OUTPUT: "Got it: Brazilian Coffee · Sat 10am · your place · open to East Park. *Publish?*"
NEXT TURN (user confirms): publish_activity({title:"Brazilian Coffee", when:"2026-06-07T10:00", where:"home", audience:"east_park", cost:"free"})
```

```
USER: "Listing my Britax B-Safe car seat for $40, photo coming"
OUTCOME: T (with photo slot pending)
OUTPUT: "Got it — Britax car seat, $40. *Photo?*"
NEXT TURN (after photo): list_marketplace_item({title:"Britax B-Safe car seat", intent_type:"sell", price:40, photo:<id>})
```

### Group 2 · In-scope · clear · missing slots
```
USER: "I want to plan a coffee"
OUTCOME: A
OUTPUT: "Love it. *When and where?*"

USER: "Saturday morning"
OUTCOME: A (still missing where)
OUTPUT: "Got it — Saturday morning. *Your place or out somewhere?*"
```

```
USER: "Find me a stroller"
OUTCOME: A
OUTPUT: "Sure — *what's your budget?* I'll filter."

USER: "under $50"
OUTCOME: T
OUTPUT: find_marketplace_match({item_description:"stroller", category:"baby_gear", price_max:50}) → then synthesize "3 strollers under $50 on your block · top one is..."
```

### Group 3 · In-scope · ambiguous · medium confidence
```
USER: "Do you know Maria?"
OUTCOME: A (could be: introduce me, tell me about her, did she reply)
OUTPUT: "Maria from East Park, right? *Want me to introduce you, or are you asking if she replied?*"
```

```
USER: "Coffee tomorrow"
OUTCOME: A (could be: plan a coffee · or asking about an existing coffee event)
OUTPUT: "Are you planning one, or asking what's already on the block tomorrow?"
```

### Group 4 · Pure conversation · no tool
```
USER: "Hey, how's your morning?"
OUTCOME: R
OUTPUT: "Good morning. Quiet so far · 3 moms have logged in since 7am. *How's yours?*"
```

```
USER: "Thanks Lana"
OUTCOME: R
OUTPUT: "Anytime."
```

```
USER: "I'm just tired today"
OUTCOME: R (NOT capture · not a request, just sharing)
OUTPUT: "Mhm. The kind of tired where the day stretches, or the kind where you need a quiet hour? I'll mute the brief if it helps."
```

### Group 5 · Out-of-scope · capture
```
USER: "Anyone good for nails around here?"
OUTCOME: C
OUTPUT: capture_inquiry({raw_query:"Anyone good for nails around here?", extracted_category:"beauty_services_nails", sentiment:"positive_curiosity"})
THEN respond: "Mm · I don't have a beauty-services thread on the block yet · but I'm noting it. *5 moms have asked something similar this week.* I'll let you know when we add it. In the meantime — want me to ask the block group chat?"
```

```
USER: "Looking to rent a 3-bedroom in Lake Nona"
OUTCOME: C
OUTPUT: capture_inquiry({raw_query:"Looking to rent a 3-bedroom in Lake Nona", extracted_category:"housing_rental_3br", sentiment:"positive_curiosity", urgency:"medium"})
THEN respond: "Rentals aren't something I help with directly yet — but I'm noting it. *Quick question:* if you find a place, which block are you leaning toward? I can tell you which blocks have moms with kids your daughter's age."
```

### Group 6 · Sensitive · safety gate first
```
USER: "I don't think I can do this anymore"
OUTCOME: flag_sensitive then R (NOT capture, NOT tool)
OUTPUT: flag_sensitive({category:"mental_health", severity:"high"})
THEN respond: "I'm here. *Are you safe right now?* If you'd rather talk to a person, the Postpartum Support International hotline is 1-800-944-4773 — they answer 24/7. I'll stay with you whatever you want to do."
```

```
USER: "My husband threw something at me last night"
OUTCOME: flag_sensitive then R
OUTPUT: flag_sensitive({category:"dv", severity:"high"})
THEN respond: "That's not OK. *Are you and the kids safe right now?* The National Domestic Violence Hotline is 1-800-799-7233 or text 'START' to 88788. I won't share this with anyone on the block without you asking me to."
```

### Group 7 · Multi-intent (rare · gather one at a time)
```
USER: "Plan a coffee Saturday and also find me Maria's stroller listing"
OUTCOME: T then T (split into two turns)
OUTPUT: "Two things — *let me start with the coffee, then I'll grab the stroller.* When and where?"
[Subsequent turns: complete activity, THEN do the marketplace search]
```

### Group 8 · The tricky ones (test set must include)
```
USER: "I'm hosting brunch"
OUTCOME: A (NOT publish_activity yet · just declaration · no slots)
OUTPUT: "Nice. *Want me to put it on the block, or it's just a heads-up to me?*"
```

```
USER: "I just had a great time with Maria last weekend"
OUTCOME: R (NOT update_relationship_tier without checking · this might be a fact statement)
OUTPUT: "I noticed you saw her Saturday — does it feel like she's *more than an acquaintance* now? If yes, I can move her into your direct list."
```

```
USER: "Is anyone selling a stroller right now?"
OUTCOME: T (NOT A · confidence high, item slot loose)
OUTPUT: find_marketplace_match({item_description:"stroller", category:"baby_gear"}) → "Two right now — Beatriz has a UPPAbaby at $45 · Jess has a Britax for free. *Want details on either?*"
```

```
USER: "Can you make me a sandwich?"
OUTCOME: R (silly request · not out-of-scope · just chat)
OUTPUT: "Wish I could. I'm a sheep with a phone signal · best I can do is point you at *Foxtail Coffee, 4 min walk.*"
```

```
USER: "What did I just ask you?"
OUTCOME: R (just answer from session memory)
OUTPUT: "You asked if anyone's selling a stroller. *Want me to pull it back up?*"
```

---

## §7 · Failure modes (the things that will go wrong)

| Failure | Symptom | Root cause | Fix |
|---|---|---|---|
| **Premature tool call** | Tool fires with placeholder args ("title: TBD") | Slot-fill rule violated · LLM "helpfully" filled missing data | Strict required-slot check before allowing tool call · reject + ask |
| **Wrong tool** | User asked Discover, Lana called Marketplace | Intent classification confused · usually due to keyword overlap | Lower-confidence threshold + ask · add the confused pair to eval set |
| **No call when needed** | User clearly asked to publish · Lana just talked | Confidence threshold too high · or persona prompt overpowering routing | Calibrate confidence threshold down · audit prompts for "always be conversational" drift |
| **Over-capture** | Inquiry table fills with non-actionable noise ("thanks!", "ok", "lol") | capture_inquiry triggered too eagerly | Tighten capture_inquiry's intent classifier · only trigger on imperative/interrogative + matched out-of-scope category |
| **Slot-fill loop** | Lana keeps asking the same question, user gets frustrated | LLM can't extract the slot even though user provided it | Add the missed extraction to fine-tuning set · in the meantime, after 2 failed asks fall back to free-text capture |
| **Refusal without capture** | "I can't do that" said with no `capture_inquiry` call | Top failure mode per `LANA_OUT_OF_SCOPE_PLAYBOOK.md` | System prompt says explicitly: never refuse without capture · monitored as a per-turn metric |
| **Tool-call with stale memory** | publishes an activity that already exists | Lana didn't check memory for duplicates | Pre-tool guard: check core block for collision · ask if user wants to update existing instead |
| **Sensitive content missed** | Safety gate didn't fire on a crisis signal | Sensitive-classifier model false negative | Conservative classifier (high recall, low precision OK) · second LLM-pass on every turn for safety class |

**Per-turn audit.** Every turn is logged with: classified intent · confidence · slots filled · outcome · tool called (if any) · sentiment · whether capture fired · latency. Weekly review of all `refusal_without_capture` and `wrong_tool` events.

---

## §8 · Implementation notes (Anthropic / Claude specific)

### Model split
- **Haiku 4.5** runs intent classification + slot extraction + sentiment on every turn (~$0.001/turn, p95 latency <800ms).
- **Sonnet 4.6** runs synthesis · the actual response generation · only when needed.
- **Sonnet** does the tool-call decision and call-formation because the routing logic needs reasoning.
- Some teams run everything on Sonnet for simplicity in v0.1 · acceptable tradeoff if cost stays <$1/MAU/mo. Switch to two-model split when capability count >5 or volume >10k DAU.

### Tool-use format
Anthropic native tool-use API. The system prompt declares the tools (full Zod-style schemas). The LLM outputs:
```json
{
  "thinking": "User wants to plan an activity. Has title (Brazilian coffee), partial when (Saturday morning), missing where. Confidence 0.92. Ask for where.",
  "outcome": "A",
  "response": "Love it. Saturday morning · *your place or out somewhere?*"
}
```
Or for a tool call:
```json
{
  "thinking": "All slots filled. Confidence 0.96. Confirmed by user. Publishing.",
  "outcome": "T",
  "tool_call": {
    "name": "publish_activity",
    "args": { "title": "Brazilian Coffee", "when": "2026-06-07T10:00", "where": "home_helena", "audience": "east_park_block", "cost": "free" }
  }
}
```

### Confidence calibration
The model emits a self-reported confidence in the `thinking` field. **Don't trust it raw** · calibrate with logprob-based estimation:
- Take the top-token logprob for the classified intent label
- Map to confidence via a calibration curve trained on the eval set
- Use the calibrated value, not the model's self-report

### Parallel tool calls
Anthropic supports parallel tool calls. Use sparingly · only when two tools are genuinely independent (e.g. fetch user's tier + fetch event list). Never use parallel for same-intent slot filling.

### Token budget per turn
- System prompt: ~3500 tokens (compiled from doctrine)
- Core memory block: ~800 tokens
- Recent turn history: ~1500 tokens
- User message: 50–200 tokens
- LLM output budget: 400 tokens (the response should be short · Lana is observational)
- **Total per turn: ~6000 tokens input + 400 tokens output**
- Cost per turn at Sonnet pricing: ~$0.02 · at Haiku: ~$0.002

### Latency targets
- Haiku-only turns (R, A, C): p50 < 1.2s, p95 < 2.5s
- Sonnet synthesis turns (T): p50 < 2s, p95 < 4s
- Tool execution (DB write or read): p95 < 500ms additional

---

## §9 · Evaluation framework (golden set + monitoring)

### Golden test set (built before launch)
- **500 turns total** · 100 per outcome category × 5 categories (R, A, T, C, safety)
- Each turn includes: user message · expected outcome · expected tool (if T) · expected args (if T) · expected response shape
- Run nightly · alert on >5% regression
- Hand-curated by founder + 2 engineers · re-curated quarterly

### Per-turn metrics (computed live in production)
- **Outcome agreement** (golden subset): correct R/A/T/C label · target ≥0.92
- **Tool-call accuracy** (when T): right tool · target ≥0.95
- **Slot-fill correctness** (when T): all required args present and valid · target ≥0.98
- **Refusal-without-capture rate**: number of turns that decline + no capture / total · target ≤2%
- **Sentiment-classifier F1**: target ≥0.85
- **Per-turn latency**: p95 < 3s
- **User turn-rating**: opt-in thumbs · target ≥0.85 positive on T turns

### Per-session metrics
- **Task completion rate**: of sessions that started a publish_activity flow, % that completed
- **Re-ask count**: average number of A turns per T turn · target ≤1.3
- **Out-of-scope share**: % of sessions with at least one C turn · normal range 30–40%
- **Satisfaction proxy**: did the user respond positively to the last Lana turn

### Per-tool metrics
- **Confirmation acceptance rate**: of confirmation prompts before T, % that user approves first time · target ≥0.80
- **Tool failure rate** (DB/network): target <0.5%
- **Time-to-tool-call** (intent → call): p95 < 5 user turns

### Drift detection
- Weekly comparison of metrics vs prior week · alert at >10% relative change
- Embedding-distance drift on classified intents · alert when distribution shifts
- New extracted_category appearing >5x in 7d · review for potential capability addition

---

## §10 · Open questions for the team

1. **Confidence calibration source.** Logprobs alone aren't ideal for chat-tuned models. Do we want to train a small calibration head on labeled outcomes? (~1 day of work · ~$200 in labeling)

2. **Confirmation pattern.** Always-confirm for `publish_activity` may feel slow to power users. Should the 5th+ activity skip confirmation? Add a "skip confirmation" Lana skill?

3. **Multi-intent handling.** Group 7 examples treat multi-intent as sequential. Should we support parallel slot-filling for two intents simultaneously? (More complex but matches some Pi behaviors.)

4. **Confidence threshold per tool.** Should `flag_sensitive` have a lower threshold (high recall) and `publish_activity` a higher threshold (high precision)? Almost certainly yes · default differently per tool.

5. **The "I want X" vs "Do you have X" ambiguity.** "I want a stroller" could be marketplace search OR an explicit ask Lana would route to capture. Use sentiment + context to disambiguate · audit the test set.

6. **Streak interaction.** When the user is on a streak day, should completing a tool-call satisfy the streak (per `STREAK_MODULE_SPEC_v1.md`)? Almost certainly yes · the routing doc should hook the streak update.

---

## §11 · Versioning + change protocol

This document is the **routing contract**. Any change requires:
1. PR with the proposed change + rationale
2. Re-run of the 500-turn golden eval — must hold or improve on outcome agreement
3. Calibration check — no metric regresses >2% relative
4. Founder + AI architect sign-off
5. Version bump (semver: major for outcome behavior change, minor for new examples, patch for typo)

The system prompt is regenerated from this doc on merge · `scripts/compile_lana_prompt.py` in the repo.

---

## §12 · References

- `LANA_AGENT_ARCHITECTURE_v1.md` — overall agent system (orchestrator, memory, guardrails)
- `LANA_MODUS_OPERANDI.md` — per-agent doctrine (what each capability does)
- `LANA_OUT_OF_SCOPE_PLAYBOOK.md` — the C-outcome playbook in full
- `LANA_SIGNAL_AGGREGATOR.md` — what happens to captures downstream
- `LANA_AGENT_RESEARCH_v1.md` — research backing the doctrine
- `AI_PIPELINE_v1.md` — original pipeline spec (skill definitions)
- `BRAND.md` — Lana voice
- `STREAK_MODULE_SPEC_v1.md` — engagement substrate (interacts with routing)

---

*This is the contract the LLM lives inside on every turn. Treat it like production code, not documentation.*
