# Lana · State Model · v1

*Compiled 2026-06-12 · for Abdullah (frontend) · Aki (AI · when back) · brand/illustration · Tommaso (founder review)*

> **Purpose:** define the visual-state model for Lana the mascot. Lana is not one static sheep · she's a character with named states that map to backend signals. This doc specifies the **4 baseline states** for v0.2 launch, the **SVG symbol library contract** for engineering, the **backend → state mapping**, and the **v0.3+ extension paths** (custom dress · walking · proactive dialogue).
>
> **Why now:** founder asked for multi-state Lana to "bridge the wait time" and make her feel alive. Today's mockup uses a single `<use href="#sheep-brand"/>` reference everywhere. The v0.2.9 mockup adds `data-lana-state="..."` as scaffolding so the symbol swap is one inline change away when the new poses ship.
>
> **Grounded in:** founder verbatim transcript (2026-06-12) · v0.2.9 walkthrough state-hook · `LANA_UNIFIED_DISCOVERY_FRONTEND.md` `routing_phase` table · `LANA_TOOL_ROUTING_v1.md`.

---

## §1 · The 4 baseline states (ship-able at v0.2)

| State ID | When Lana enters it | Visual cue | Animation |
|---|---|---|---|
| `idle` | Default · waiting · ready · between turns · session opened with no active prompt | Eyes open · neutral expression · sunglasses on · hands relaxed | Subtle breathing pulse (2-second sine · 1px scale) · blink every 4-6s |
| `talking` | Lana is sending an assistant message · bubble streaming OR just-set · the speaking moment | Mouth subtly open · slight head bob · paws gesture (one open palm) | Talking jaw cycle (~0.4s loop while bubble is rendering) · stops when message done |
| `listening` | User mic is active OR composing in chat input · Lana is taking in input | Ear forward · head tilted ~5° · eyes attentive · sunglasses on | Slow "hearing" pulse around the ears (0.8s · 4% scale) |
| `thinking` | Compute reasoning between user input and Lana response · tool calls running · embedding lookups · the **bridging wait moment** | Hand on chin · slight tilt downward · sparkle dots above head · paws subtly busy | Sparkle/dots fade-in cycle (0.6s) · stops when assistant_message arrives |

### Why these 4 first

- **Maps 1:1 to the backend's routing_phase / turn lifecycle** — see §3 below
- **Covers the entire conversation loop** with zero gaps the user perceives as "frozen"
- **Sets the asset contract for everything else** — once the 4 poses exist, all future variants (custom dress, walking, etc.) follow the same symbol pattern
- **Cheapest first build** — 4 SVG illustrations · CSS-only animations · no Lottie / no game engine

### Why NOT more states at v0.2

Adding more states (e.g., "celebrating", "concerned", "sleeping") before validation = over-engineering. Ship the 4-state baseline, watch which moments feel flat, expand from there.

---

## §2 · SVG symbol library contract

### §2.1 · The current state (v0.2.9)

Today every frame uses one symbol:
```html
<symbol id="sheep-brand" viewBox="0 0 1024 1024">
  <!-- The current Lana illustration (cream/teal/gold concierge sheep) -->
  ...
</symbol>
```

And every frame uses:
```html
<div class="lana-svg-wrap" data-lana-state="talking">
  <svg viewBox="0 0 1024 1024"><use href="#sheep-brand"/></svg>
</div>
```

The `data-lana-state` attribute is the **state hook** · already in place across ~49 wraps in the walkthrough + ~60 wraps in the explorer.

### §2.2 · The target state (v0.3.0 or whenever poses ship)

Four distinct symbols · same wrapper:
```html
<symbol id="lana-idle" viewBox="0 0 1024 1024">...</symbol>
<symbol id="lana-talking" viewBox="0 0 1024 1024">...</symbol>
<symbol id="lana-listening" viewBox="0 0 1024 1024">...</symbol>
<symbol id="lana-thinking" viewBox="0 0 1024 1024">...</symbol>
```

The `<use>` reference becomes state-driven:
```html
<div class="lana-svg-wrap" data-lana-state="talking">
  <svg viewBox="0 0 1024 1024">
    <use class="lana-state-idle" href="#lana-idle"/>
    <use class="lana-state-talking" href="#lana-talking"/>
    <use class="lana-state-listening" href="#lana-listening"/>
    <use class="lana-state-thinking" href="#lana-thinking"/>
  </svg>
</div>
```

Or simpler: a single `<use>` whose `href` is dynamically swapped by JS based on `data-lana-state`.

### §2.3 · CSS to drive state-swap

Pure CSS, no JS for static mockups · JS for the production app:

```css
/* All state layers hidden by default */
.lana-svg-wrap svg use[class^="lana-state-"] { display: none; }

/* Show the layer matching the wrapper's state attribute */
.lana-svg-wrap[data-lana-state="idle"]     svg .lana-state-idle      { display: block; }
.lana-svg-wrap[data-lana-state="talking"]  svg .lana-state-talking   { display: block; }
.lana-svg-wrap[data-lana-state="listening"] svg .lana-state-listening { display: block; }
.lana-svg-wrap[data-lana-state="thinking"] svg .lana-state-thinking  { display: block; }

/* Per-state CSS animations */
@keyframes lana-breathe {
  0%, 100% { transform: scale(1); }
  50%      { transform: scale(1.01); }
}
.lana-svg-wrap[data-lana-state="idle"] svg {
  animation: lana-breathe 2.4s ease-in-out infinite;
}

@keyframes lana-listen-pulse {
  0%, 100% { transform: scale(1); }
  50%      { transform: scale(1.04); }
}
.lana-svg-wrap[data-lana-state="listening"] svg {
  animation: lana-listen-pulse 0.8s ease-in-out infinite;
}

@keyframes lana-think-sparkle {
  0%, 100% { opacity: 0.7; }
  50%      { opacity: 1.0; }
}
.lana-svg-wrap[data-lana-state="thinking"] svg {
  animation: lana-think-sparkle 0.6s ease-in-out infinite;
}

/* prefers-reduced-motion respects the user's OS setting */
@media (prefers-reduced-motion: reduce) {
  .lana-svg-wrap svg { animation: none !important; }
}
```

### §2.4 · Asset deliverables checklist (designer)

For the 4 baseline states, the brand/illustration team needs to deliver:

- [ ] `lana-idle.svg` — the default pose (the uniformed-concierge Lana the founder uploaded · cream + teal + gold)
- [ ] `lana-talking.svg` — same character · slight gesture · subtle mouth open
- [ ] `lana-listening.svg` — ear forward · head tilt · attentive
- [ ] `lana-thinking.svg` — hand on chin OR sparkle dots above · contemplative

Each SVG:
- Same 1024×1024 viewBox
- Same character (uniformed concierge) — only pose changes
- Transparent background
- Single artboard · no overlapping artboards or hidden layers
- Same color palette (cream #fefef8 · teal #0E5D5E · gold #fed052 · pink #f4cfc1)
- ≤ 50KB each (optimize via SVGO)

---

## §3 · Backend → Lana state mapping

The frontend derives the Lana state from the backend's `routing_phase` + turn lifecycle. **The backend does NOT need to send a separate field** — this logic lives in the FE.

| Backend signal | Lana state |
|---|---|
| Session opened · no recent activity · `ui_intent: chat` | `idle` |
| User typing in input OR mic actively recording | `listening` |
| HTTP request to `/lana/sessions/.../messages` in flight (waiting for response) | `thinking` |
| Response received · `assistant_message` rendering (typewriter or instant) | `talking` |
| `routing_phase: listening` · waiting for next user input | `idle` |
| Any `routing_phase: await_*` (collect_zip · collect_phone · collect_otp · etc.) · user is mid-input | `listening` |
| `routing_phase: synthesizing` (if backend uses it) · or any pause before response | `thinking` |
| `assistant_message` populated · text mid-stream | `talking` |
| `ui_intent: upload_profile_photo` (file picker open · waiting for selection) | `listening` |
| Tool call in flight (orchestrator running tools) | `thinking` |

### §3.1 · State machine view

```
                 ┌────────┐
                 │ idle   │ ←──────┐
                 └───┬────┘        │
                     │             │
            user starts            assistant_message done
            typing/talking         response complete
                     │             │
                     ▼             │
                 ┌────────────┐    │
                 │ listening  │    │
                 └─────┬──────┘    │
                       │           │
                user submits       │
                       │           │
                       ▼           │
                 ┌────────────┐    │
                 │ thinking   │    │
                 └─────┬──────┘    │
                       │           │
              response starts      │
                       │           │
                       ▼           │
                 ┌────────────┐    │
                 │ talking    │────┘
                 └────────────┘
```

This is a clean unidirectional loop. Lana is always in exactly one state. State transitions are driven by user input events + backend response events.

### §3.2 · Implementation in Abdullah's FE

Pseudo-code:

```typescript
type LanaState = 'idle' | 'listening' | 'thinking' | 'talking';

function deriveLanaState(args: {
  isUserComposing: boolean,
  isRequestInFlight: boolean,
  isResponseStreaming: boolean,
  uiIntent: string,
  routingPhase: string,
}): LanaState {
  if (args.isResponseStreaming) return 'talking';
  if (args.isRequestInFlight) return 'thinking';
  if (args.isUserComposing) return 'listening';
  if (args.uiIntent.startsWith('collect_') || args.uiIntent === 'upload_profile_photo') return 'listening';
  if (args.routingPhase.startsWith('await_')) return 'listening';
  return 'idle';
}

// Apply to wrapper
function applyLanaState(state: LanaState) {
  document.querySelector('.lana-svg-wrap')?.setAttribute('data-lana-state', state);
}
```

---

## §4 · v0.3+ extension paths

The 4-state baseline is the foundation. Founder's three roadmap items map cleanly on top.

### §4.1 · Customize Lana's dress (v0.4+ · monetization-adjacent)

**What it means:** mom picks a Lana outfit · concierge uniform (default) · running track suit · seasonal (Christmas elf · summer beach hat) · regional (Brazilian flag bowtie · Mexican pin) · premium drops.

**Implementation:**
- Each outfit adds a NEW set of 4 symbols (one per state) — `lana-running-idle` · `lana-running-talking` · etc.
- A new attribute `data-lana-outfit` on the wrapper controls which symbol set is active: `data-lana-outfit="running" data-lana-state="idle"` → `#lana-running-idle`
- Settings UI for Lana wardrobe · stored in `users.lana_outfit` column
- Cost per outfit: 4 SVG poses · ~2 hours of designer time · zero engineering changes after the symbol pattern is set

**Why powerful:**
- Identity bonding (Tolan / Replika retention data: +30-40% DAU from companion customization)
- Brand virality · custom Lanas become screenshot moments
- Monetization · premium / seasonal drops
- Cultural localization · regional accents per market

**Backlog priority:** v0.4 after Day-Zero retention is proven.

### §4.2 · Move Lana · walking / running / mini-game (v0.5+)

**What it means:** when Lana is idle, she wanders her block · walks to fellow houses · skips/runs when celebrating a match. Mom can tap her to make her wave. Optional ambient interaction.

**Implementation:**
- Adds 3-4 new states: `walking-left`, `walking-right`, `running`, `waving`, `dancing`
- A "block map canvas" surface (probably a separate sub-screen) where Lana animates
- Pure CSS sprite animation OR Phaser 3 lightweight game loop
- Idle-state expansion · Lana's "home base" can be the mom's block map

**Why powerful:**
- Ambient delight · the app feels ALIVE without forcing interaction (Animal Crossing / Tamagotchi DNA)
- Pull-to-open habit · mom checks the app to see what Lana's doing today
- Discovery surface · when Lana walks to a fellow's house, she's surfacing a match without a banner
- Differentiation moat · zero competitors have this

**Backlog priority:** v0.5+ once core retention works.

### §4.3 · Proactive Lana · she starts the conversation (v0.4)

**What it means:** Lana initiates dialogue · per-name · per-subject specific · without the user prompting anything. Examples:

- *"Maria · it's Saturday · 3 new moms signed up in your block this week. Want to see who?"*
- *"Maria · I noticed you asked about pediatric dentists 3 weeks ago. Sara just shared a recommendation. Want it?"*
- *"Maria · today is Sergio's 4th birthday · want me to draft something cute for the block?"*

**Implementation:**
- Server-side scheduler (Cloud Run cron · per-user evaluator)
- Triggers: time-of-day · new matches · new fellows · milestones · seasonal events · birthdays from identity_claims
- Push notification → opens Lana chat with the proactive turn pre-populated
- Each trigger has a Lana voice template + personalization slots filled from user's identity_claims + signal history
- A/B + frequency-cap controls (max N proactive nudges per mom per week · respect Do-Not-Disturb)

**Why powerful (this is the BIGGEST add-on):**
- Engagement multiplier · 3-5x DAU vs reactive-only (per Replika data)
- Memory + signal closure · the captures from `out_of_scope_inquiry` + `identity_claims` + meets become triggers · the data loop IS the moat
- Trust deepening · Lana remembers what mom said weeks ago and FOLLOWS UP · the difference between a chatbot and a concierge
- WhatsApp / SMS extension · she can text mom outside the app at the right moment

**Visually:** proactive turns use the `talking` state · maybe with a unique visual cue ("Lana checked in" pill) so mom knows the message is unprompted.

**Backlog priority:** **v0.4** (4 weeks after Day Zero · once the signal-capture flywheel has data to drive personalization).

---

## §5 · Open questions for the team

1. **Designer (you or whoever):** can you deliver the 4 baseline poses (idle/talking/listening/thinking) of the uniformed-concierge Lana as 4 separate SVG files, same character, only pose changes? Timeline?
2. **Abdullah:** confirm the FE-side state derivation (§3.2) is reasonable. Any latency considerations on swapping the state attribute every keystroke?
3. **Aki (when back):** for `thinking` state · should we surface tool-call telemetry to the FE so we can show longer "thinking" durations on tool-heavy turns? Or is the simple "request in flight" signal enough?
4. **Founder:** which v0.3+ extension comes first when we cross the retention bar? My recommendation: **proactive Lana** (highest engagement leverage · uses data we're already capturing). Custom dress and walk-Lana are delight features · proactive Lana is a fundamentally different product surface.

---

## §6 · Sources

- Founder verbatim transcript · 2026-06-12 (the message asking for multi-state Lana)
- `LANA_UNIFIED_DISCOVERY_FRONTEND.md` · GitHub (TagAlng-Backend)
- `LANA_TOOL_ROUTING_v1.md` · Drive
- `lana-v01-walkthrough.html v0.2.9` · the state-hook scaffold
- Replika engagement research · published 2024-2025 (custom-companion retention)
- Tolan / OpenAI case study · companion AI persistence patterns

---

*v1 first pass · 2026-06-12 · Tommaso, please review/edit/forward to design + Abdullah + Aki · this becomes the canonical state-model doc for the Lana mascot system.*
