# Lana Layer 1 — Manual test guide

What to say in chat to verify every **Layer 1 linear intent** (v0.2). Use the PWA at **`/chat`** or Postman unified Lana collection.

---

## Before you start

### Open chat (not map)

- **URL:** `http://localhost:3000/chat` (or your deployed PWA + `/chat`)
- Root `/` → `/lana`. If you are **phone-verified**, `/lana` redirects signed-in users to `/map`. Go to `/chat` directly.
- Anonymous guests can use `/lana` → **Meet Lana** → `/chat`.

### Environment

| Item | Value / action |
|------|----------------|
| PWA | `NEXT_PUBLIC_LANA_WORKER_URL` points at lana-worker (local or tagalng-dev) |
| Supabase | Anonymous sign-in ON · Phone provider ON |
| Migrations applied | `20260630120000_local_signals_block_log.sql` · `20260701120000_layer1_complete.sql` |
| Dev test phone | `+15550999012` |
| Dev OTP | `000000` |
| Fresh phone | Use a new test number if verify fails (number already linked) |

### How to see what fired

**Browser:** DevTools → Network → `POST .../messages` → Response JSON:

| Field | Meaning |
|-------|---------|
| `active_intent` | Layer 1 intent id (e.g. `looking.swap`) |
| `routing_phase` | Funnel step (`need_zip`, `signal_confirm_missing`, …) |
| `ui_intent` | What UI to show (`show_identity_profile`, `signal_saved`, …) |
| `identity_profile` | Claims card payload |
| `signal_saved` | Posted local signal |
| `block_log_entries` | Block log matches |
| `peer_matches` | Neighbor preview cards |

**Pass:** Lana’s reply matches the “Expect” column and the JSON fields match (when listed).

---

## 0 — Baseline funnel (do once per session)

Run these in order so discovery and signals have ZIP + block.

| Step | You say | Expect |
|------|---------|--------|
| 0.1 | *(open `/chat`)* | Greeting; anonymous session created |
| 0.2 | `32827` *(or your block ZIP)* | Asks for identity or confirms block · `routing_phase: need_zip` clears |
| 0.3 | `I'm Brazilian, two toddlers, runner` | Identity extracted · `need_identity` → progress |
| 0.4 | `Maria` *(when asked for name)* | Display name saved |
| 0.5 | `find people like me` | Redacted peer preview cards |
| 0.6 | Give phone `+15550999012` when gated | Phone collector · `auth_action: link_phone_signup` |
| 0.7 | OTP `000000` | `phone_verified: true` · full peer names/scores |

---

## 1 — Discovery

| Intent | You say | Expect |
|--------|---------|--------|
| `discovery.find_peers` | `find people like me on the block` | Peer cards · `active_intent: discovery.find_peers` |
| | `show me nearby moms` | Same |
| | `who's around me` | Same |
| `discovery.find_by_attrs` | `find a Brazilian mom with toddlers` | Filtered peers · `discovery.find_by_attrs` |
| | `moms near me who speak Portuguese` | Same |
| `discovery.find_in_block` | `what's happening in my block` | Block summary (neighbor count, quiet/active) |
| | `who's new on my block` | Same |
| `discovery.block_log` | `show my block log` | Block log entries card |
| | `who matched with me` | Same · `block_log_entries` in response |
| `discovery.find_activities` | `what activities are nearby` | Activity previews (if any on block) |
| | `anything happening this weekend` | Same |

---

## 2 — Identity

| Intent | You say | Expect |
|--------|---------|--------|
| `identity.show_my_profile` | `what do you know about me` | Profile reply + **identity claims card** |
| | `show me what you have` | Same · `ui_intent: show_identity_profile` |
| `identity.add_claim` | `I'm also a teacher` | “added N identity thread(s)” |
| | `we're Christian, love hiking` | Claims upserted |
| `identity.edit_claim` | `I'm not Brazilian, I'm Mexican` | “updated …” |
| | `edit my identity` | Re-prompt to change something |
| `identity.complete_profile` | `that's me` *(after mapped profile)* | `ready_to_complete` / confirm profile CTA |

---

## 3 — Looking lane (swap · meet · tip)

All use a **4-phase cascade**: extract → confirm missing slot → save → `signal_saved` card.

### 3.1 `looking.swap`

| Turn | You say | Expect |
|------|---------|--------|
| 1 | `I'm looking for rain boots` | Asks for detail/size |
| 2 | `3T rain boots` | May ask size if missing: “3T, size 5…” |
| 3 | `3T` *(if prompted)* | Saved · `signal_saved` · `active_intent: looking.swap` |

**One-shot (if detail includes size):**  
`I'm looking for 3T rain boots` → save directly.

### 3.2 `looking.meet`

| Turn | You say | Expect |
|------|---------|--------|
| 1 | `looking for stroller walk buddies` | “When works for you?” |
| 2 | `weekend mornings` | Saved · `looking.meet` |

**One-shot:**  
`moms with toddlers Saturday morning` *(when in same message)* → may skip when prompt.

### 3.3 `looking.tip`

| Turn | You say | Expect |
|------|---------|--------|
| 1 | `know a good pediatrician?` | “What category — health, food…?” |
| 2 | `health` | Saved · `looking.tip` |

---

## 4 — Sharing lane

Same cascade as Looking; intent flips to **offer/host/share**.

| Intent | You say | Expect |
|--------|---------|--------|
| `sharing.swap` | `I have 3T rain boots my kid outgrew` | Confirm size if needed → save |
| `sharing.host` | `I want to host a Brazilian moms coffee` | Confirm when → save |
| | `Saturday playgroup at my house` | Same |
| `sharing.tip` | `Dr. Sarah is the best pediatric dentist` | Confirm category → save |

---

## 5 — Relationship tier & intros

| Intent | You say | Expect |
|--------|---------|--------|
| `tier.send_nudge` | `introduce me to Maria` *(after peers shown)* | Intro proposal draft |
| | `send Maria a note` | Same · maps to `propose_intro` |
| `social.list_intros` | `show my intros` | Pending sent/received list |
| `tier.respond_nudge` | `yes introduce us` | Connected · `accept_intro` |
| | `not now` | Declined |
| | `block` | User blocked |

*Note:* `tier.advance_*` intents run in the **background** on warm exchange / meet attendance — no phrase to test in chat.

---

## 6 — Auth

| Intent | You say | Expect |
|--------|---------|--------|
| `auth.signup_phone` | *(phone after preview gate)* | `auth_action: link_phone_signup` · phone UI |
| `auth.signup_otp` | `000000` | Verified signup |
| `auth.login_phone` | `log in` · `I already have an account` | Login phone flow |
| `auth.login_otp` | OTP after login | `verify_login_otp` |
| `auth.logout` | `log out` · `sign out` | Logout confirm · fresh anon session |
| `auth.upload_photo` | `add my picture` · `upload photo` | Photo upload prompt |

---

## 7 — Settings & help

| Intent | You say | Expect |
|--------|---------|--------|
| `settings.change_name` | `change my name to Sofia` | “I'll call you Sofia” |
| | `my name is actually Ana` | Nickname updated |
| `settings.change_zip` | `I moved` · `update my ZIP` | ZIP re-prompt · `need_zip` |
| `settings.notification_prefs` | `stop texting me` | SMS off |
| | `fewer messages` · `quiet mode` | Quiet prefs |
| | `text me normally again` | Normal prefs |
| `help.what_can_you_do` | `what can you do` · `help` | 2–3 sentence capability summary |
| `help.who_are_you` | `who are you` · `what's your name` | “I'm Lana…” intro |

---

## Edge cases worth one pass

| Scenario | You say | Expect |
|----------|---------|--------|
| Signal without block | Swap/tip with no ZIP set | “What ZIP are you in?” before cascade |
| Signal without verify | Swap as anonymous | “Verify your phone first…” |
| Short swap detail | `boots` | “Can you be more specific — size, brand?” |
| PGRST202 | Any save after migrations missing | PostgREST function not found — **apply migrations** |
| Classifier miss | Gibberish | Generic chat / orchestrator · no `active_intent` |

---

## Automated smoke (backend)

```bash
cd services/lana-worker
PYTHONPATH=. .venv/bin/python -m unittest \
  tests.test_layer1_intents \
  tests.test_local_signals \
  tests.test_discovery_route -q
```

Expect: **58 tests OK**.

---

## Postman alternative

Import `docs/postman/TagAlng-Lana-Unified-Full-E2E.postman_collection.json` + `TagAlng-tagalng-dev.postman_environment.json`. Run folders **A → E** in order for discovery + auth E2E without the PWA.

---

## Quick checklist (all Layer 1 surfaces)

- [ ] Discovery: peers, attrs, block summary, block log, activities  
- [ ] Identity: show profile, add claim, edit claim, complete profile  
- [ ] Looking: swap, meet, tip (multi-turn cascade)  
- [ ] Sharing: swap, host, tip  
- [ ] Tier: send nudge, list intros, respond (accept / decline / block)  
- [ ] Auth: signup, login, logout, photo  
- [ ] Settings: name, ZIP, notification prefs  
- [ ] Help: what can you do, who are you  

---

*Layer 1 catalog: `services/lana-worker/app/layer1_intents.py` · Architecture: `docs/Lana Agentic Architecture/LANA_INTENTS_AND_ROUTING_v1.md`*
