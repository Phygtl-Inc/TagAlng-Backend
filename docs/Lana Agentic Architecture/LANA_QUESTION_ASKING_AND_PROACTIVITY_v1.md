# LANA · How Lana Asks Questions · Reactive, Follow-up & Proactive

*How Lana decides what to ask, how she deepens a conversation, and how she will ask
unprompted questions without irritating the user · v1 · 2026-07-02*

> **TL;DR** — Today Lana only asks **reactively**: she answers what you started, and asks
> follow-ups to complete a task or deepen your profile. We are adding the first **proactive**
> question (Layer 3a): after profile setup, Lana opens the next chat with one warm, relevant
> question drawn from something you said. Everything is gated so it stays helpful, never nagging.

---

## 1 · The three ways Lana asks questions

| Kind | Who starts it | Example | Status |
|---|---|---|---|
| **Reactive — task** | User (explicit request) | "find a pizza place" → *"kid or adult?"* | Shipped |
| **Reactive — deepen** | User (mentions something) | "I'm a triathlete" → *"training for a race?"* | Shipped (in profile intake) |
| **Proactive** | **Lana**, from stored state | *opens chat:* "You're a triathlete — training for something?" | **New (Layer 3a, v1)** |

The first two are what every good assistant does. The third — Lana starting a turn nobody
asked for — is the concierge layer, and the part that must be handled carefully so it feels
like a thoughtful friend, not a pushy salesperson.

---

## 2 · How Lana asks questions **today** (reactive)

Lana has no single "ask" function — question-asking is spread across purpose-built flows.
Each flow owns a small state machine and decides its own next question. This is deliberate:
it keeps each flow on-script and prevents loops.

### 2.1 · Deciding *what to ask next*

| Flow | Mechanism | Where |
|---|---|---|
| **Signup / FTUE** | Deterministic state machine (`guest_step`) | `app/guest_intake.py` |
| **Discovery funnel** (ZIP → identity → name) | Phase field (`routing_phase`) + AI intent classifier | `app/discovery_route.py` |
| **Hosting an event** | Stage machine (`host_stage`: review → setup → confirm → publish) | `app/lana_unified_pipeline.py` |
| **Finding a meet** | LLM extraction emits the next question in an `ask` field | `app/look_meet.py` |
| **Swap / tip capture** | Deterministic per-field confirm prompts | `app/signal_capture.py` |
| **Profile enrichment** | LLM, one question per turn | `app/profile_intake.py` |

Two patterns do the work:
- **Deterministic**: a stage/phase field advances step-by-step; each step has a fixed question.
- **LLM-driven**: the model reads the conversation and proposes the single most useful next
  question (e.g. profile intake, meet-seek). Capped to **one question per turn** so Lana never
  interrogates.

### 2.2 · How follow-up (deepening) works

The clearest example lives in **profile intake** (`app/profile_intake.py`):

1. The LLM synthesizer reads the conversation and the claims captured so far, and asks **at
   most one** follow-up per turn, quoting a phrase the user said.
2. A **gap detector** decides when a follow-up is *owed*. `needs_kids_followup()` is the
   canonical example: if the user signals they're a parent but never gave kid detail, Lana asks
   one clarifying question before wrapping up. This "detect a gap → ask once more" pattern is
   the template the proactive layer reuses.
3. Stop rules (`apply_profile_stop_rules()`) cap the whole intake (wrap up by ~5 turns, or once
   heritage + one more thread + a name are captured) so it never drags.

Each answer becomes a structured **identity claim** (`user_identity_claims`) — typed, embedded
for semantic recall, and tagged with a disclosure tier (public / mutual / private).

### 2.3 · Reactive personalization (recommendations)

Recently added: when a user asks for a recommendation and no neighbor has vouched one, Lana
personalizes the fallback using the user's own claims —

> *"Since you mentioned you're vegetarian, I focused on veg-friendly spots. These are from
> Google (not a neighbor vouch)… Want me to widen the search?"*

- The LLM shapes the **search query + phrasing** only; venue names come solely from Google
  Places (no invented places). `app/rec_personalize.py`, wired into `app/discovery_route.py`.
- The offer is **actionable**: a "See all …" chip re-runs the search unfiltered, so the user is
  never trapped in the personalized view. `app/ui_actions.py`.
- This is still **reactive** — it only fires because the user asked. It is *not* Layer 3a.

---

## 3 · Proactive questions (Layer 3a) — how they will work

**Goal:** occasionally, Lana asks a question the user didn't request, drawn from what she
already knows, to deepen the relationship and the profile.

### 3.1 · The first moment we're shipping

**Post-intake deepen.** When a user finishes profile setup having mentioned an interest/activity
(e.g. "I'm a triathlete") but never went deeper, Lana **opens the next chat** with one warm
question instead of the generic "How can I help you today?":

> *"Loved that you're a triathlete — training for something specific right now?"*
> `[ Training for a race ]` `[ Just for fitness ]` `[ Tell me what you need ]`

Why this moment first:
- **The opening is a zero-risk slot** — there's no user request to derail (they haven't asked
  anything yet).
- It reuses shipped machinery: the gap-detector pattern (`needs_kids_followup`), the tappable
  clarify chips, and the stored claims. No new UI.
- It validates the proactive-question UX before we build a general engine.

### 3.2 · How it decides

1. **Detect** — `pending_deepen_claim()` finds the user's top claim in the `activity` or
   `interest` bucket that hasn't been deepened yet.
2. **Gate** — only at a clean *listening* opening for a fully-onboarded user (never mid-funnel,
   never mid-flow — see §4).
3. **Ask** — an LLM writes the warm one-liner + 2–3 chip answers (template fallback if the LLM
   is unavailable). The question references the *interest*, never a sensitive/private detail.
4. **Mark** — a `followup_asked_at` timestamp is stamped on that claim so **it never asks again**.
5. **Learn** — the user's answer flows through the normal claim-extraction path and deepens
   their profile automatically. No special handling.

### 3.3 · Why NOT the suggestion queue

We deliberately do **not** route this through the `suggestion_queue` (built for Layer 3b:
content→capability matching, ranked and deferred). A deepening follow-up maps to no capability
and needn't be deferred. Layer 3a is **rule-driven** — a live guard is the right primitive.
A queue only earns its place later, if many proactive rules start competing for airtime.

### 3.4 · Where this grows next (not in v1)

- More triggers via the same guard pattern: re-engagement ("you mentioned you're a triathlete —
  how's it going?"), milestones, "new to the area" orientation.
- When several proactive rules exist, a small **rule registry** (predicate → question, priority,
  cap) — and only then a queue if we need to defer/rank across sessions.

---

## 4 · Guardrails — why the user won't get irritated

This is the core of the design. A proactive question at the wrong moment is worse than none.

| Guardrail | What it does | How |
|---|---|---|
| **Never mid-flow** | Won't interrupt a task in progress | Only fires at a clean **listening opening** — no active `signal_draft` / `event_draft` / `host_stage` / sticky flow, and `routing_phase == "listening"` |
| **Never derails a request** | The user's own intent always wins | Fires at the **opening** (before any user turn), not injected into a turn where the user asked for something |
| **Never repeats** | Asks a given thing exactly once | `followup_asked_at` stamped the moment it asks; the detector skips already-asked claims |
| **One question at a time** | No interrogation | All flows are capped to a single question per turn |
| **Offer, don't force** | User can always opt out / widen | Tappable chips include an escape ("Tell me what you need" / "See all …"); it's an invitation, not a gate |
| **Respects privacy** | No leaking sensitive facts | Self-only claims; references the *interest angle*, never a private claim's raw text |
| **Honest provenance** | No pretending | Recommendations say plainly "from Google, not a neighbor vouch" |
| **Fails safe** | A bug never breaks the chat | Every proactive/LLM/DB step is best-effort; on any failure it silently falls back to the normal opening |
| **Off by default** | Controlled rollout | Feature-flagged (`LANA_PROACTIVE_DEEPEN`), shipped dark, enabled gradually, with logs to watch behavior |

### 4.1 · Frequency discipline (roadmap targets)

The `LANA_LATENT_INTENT_ENGINE_v1` spec sets the ceiling for when many proactive nudges exist:
**max 2 per session, max 5 per week**, 24h cool-down on a dismissed category. v1 is well under
this (one question, once per claim, at an opening), but these caps are the guardrail the general
engine must enforce before broad proactivity.

### 4.2 · Emotional safety

Proactive questions must never fire during venting or a crisis. Today's gate (listening opening,
onboarded user) already excludes those states; as proactivity broadens, the crisis/empathy rails
(see `LANA_BLUEPRINT`) take precedence over any nudge.

---

## 5 · Summary

- **Reactive asking** (task + deepen) is shipped and spread across purpose-built flows, each
  capped to one question per turn.
- **Follow-up** works by detecting a gap and asking once more; answers become structured claims.
- **Proactive asking (Layer 3a)** starts with one moment — a warm deepen question at the
  post-intake opening — behind a flag, reusing existing machinery.
- **Guardrails** are the point: never mid-flow, never repeat, never force, self-only, fail-safe,
  and frequency-capped. The rule is simple — *a proactive question should feel like a friend who
  remembered, at a moment when you had nothing else going on.*

---

*v1 · 2026-07-02 · companion to `LANA_LATENT_INTENT_ENGINE_v1.md` (Layer 3 architecture) and the
implementation plan for the first 3a moment. Update as more proactive triggers ship.*
