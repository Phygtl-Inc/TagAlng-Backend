# Lana Layer 1 — Team testing guide

**For:** QA, product, and engineers manually checking Lana in the PWA.  
**Goal:** Sign in once, then type short phrases and confirm the right **intent** fired and the right **UI** appeared.

**Where to chat:** `https://<your-pwa>/chat` (use `/chat` directly — not `/map`).

---

## Part 1 — Sign in first (every tester, every session)

Do this before testing any intent below.

| Step | What you do | What should happen |
|------|-------------|-------------------|
| 1 | Open **`/chat`** | Lana greets you (anonymous session starts) |
| 2 | Say your **ZIP** — e.g. `32827` | Lana knows your block |
| 3 | Say who you are — e.g. `I'm Brazilian, mom of two toddlers` | Identity saved |
| 4 | Say your **name** when asked — e.g. `Sofia` | Display name saved |
| 5 | Say `find people like me` | Neighbor preview cards (names may be hidden until verified) |
| 6 | When asked to verify, enter phone **`+15550999012`** | SMS / verify UI appears |
| 7 | Enter OTP **`000000`** (dev only) | **`phone_verified: true`** — full names, signals, block log, intros work |

**Dev setup**

| Item | Value |
|------|--------|
| Test phone | `+15550999012` |
| Test OTP | `000000` |
| PWA env | `NEXT_PUBLIC_LANA_WORKER_URL` → lana-worker |
| DB | Run `supabase db push` from **TagAlng-backend** (signals + block log migrations) |

**Already have an account?** Say `log in` → same phone → OTP `000000`.

**Pass:** You’re verified and Lana shows real neighbor names on peer cards.

---

## Part 2 — How to know a test passed

Open **DevTools → Network →** `POST .../messages` → **Response JSON**.

| Field | Meaning |
|-------|---------|
| `active_intent` | Which Layer 1 intent ran (main check) |
| `ui_intent` | Which UI card to show (`signal_saved`, `show_block_log`, …) |
| `assistant_message` | Lana’s reply text |
| `peer_matches` | Neighbor preview cards |
| `block_log_entries` | Match log cards |
| `signal_saved` | “Posted to your block” card |
| `identity_profile` | Claims / profile card |

**Pass:** Reply makes sense **and** `active_intent` matches the table below.

---

## Part 3 — Test every Layer 1 intent

After Part 1, send **one phrase at a time**. Use a **fresh line** in chat for each test.

### Discovery — find neighbors & block

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| `find people like me` | `discovery.find_peers` | Peer match cards |
| `show me nearby moms` | `discovery.find_peers` | Same |
| `find a Brazilian mom with toddlers` | `discovery.find_by_attrs` | Filtered peer cards |
| `what's happening on my block` | `discovery.find_in_block` | Block summary (neighbor count, quiet/active) |
| `show my block log` | `discovery.block_log` | Block log / match cards |
| `who matched with me` | `discovery.block_log` | Same |
| `what activities are nearby` | `discovery.find_activities` | Activity previews (if any on block`) |

---

### Identity — profile & claims

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| `what do you know about me` | `identity.show_my_profile` | Reply + **identity claims** card |
| `I'm also a teacher` | `identity.add_claim` | “Added identity thread(s)…” |
| `I'm not Brazilian, I'm Mexican` | `identity.edit_claim` | “Updated …” |
| `that's me` *(after profile shown)* | `identity.complete_profile` | Ready to complete / confirm profile |

---

### Looking — you want something on the block

Lana may ask **one follow-up** (size, when, category) before saving.

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| `I'm looking for 3T rain boots` | `looking.swap` | Saved → **signal_saved** card (“looking for…”) |
| `looking for rain boots` → then `3T` | `looking.swap` | Size question, then save |
| `looking for a walking buddy on weekends` | `looking.meet` | May ask “when?” → then save |
| `know a good pediatrician?` → then `health` | `looking.tip` | Category question, then save |

**Matcher note:** `looking.meet` only matches someone who **`sharing.host`** (hosting a meetup), not another person who is also only “looking for a buddy.” Two buddy-seekers won’t match yet.

---

### Sharing — you offer something on the block

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| `I have 3T rain boots to give away` | `sharing.swap` | Saved → offer card |
| `I want to host a weekend walking meetup` | `sharing.host` | Saved → hosting card; may find **meet** matches |
| `Dr. Smith is a great pediatric dentist` → `health` | `sharing.tip` | Saved → tip card |

**Matcher note:** To match a walking buddy seeker, the **host** says “host a weekend walk”; the **seeker** says “looking for walking buddy.” Swap matches (e.g. “kid bicycle”) are separate — old swap signals can still show on block log until closed.

---

### Intros & nudges

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| `introduce me to Maria` *(after peers shown)* | `social.propose_intro` / `tier.send_nudge` | Intro proposed or “already have an intro…” |
| `show my intros` | `social.list_intros` | List of pending intros (sent + received) |
| `yes introduce us` *(on pending intro)* | `tier.respond_nudge` | Accept flow |
| `not now` | `tier.respond_nudge` | Decline |

---

### Auth

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| Phone when gated (Part 1) | `auth.signup_phone` | Phone input UI |
| `000000` after signup SMS | `auth.signup_otp` | Verified |
| `log in` / `I already have an account` | `auth.login_phone` | Login phone flow |
| OTP after login | `auth.login_otp` | Signed into existing account |
| `log out` | `auth.logout` | Logout confirm |
| `add my picture` | `auth.upload_photo` | Photo upload prompt |

---

### Settings & help

| You say (example) | Intent that fires | What you should see |
|-------------------|-------------------|---------------------|
| `change my name to Ana` | `settings.change_name` | “I'll call you Ana…” |
| `I moved` / `update my ZIP` | `settings.change_zip` | Asks for new ZIP |
| `stop texting me` | `settings.notification_prefs` | SMS quiet/off |
| `what can you do` | `help.what_can_you_do` | Short capability list |
| `who are you` | `help.who_are_you` | “I'm Lana…” |

---

## Part 4 — Two-account tests (swaps & meets)

Use **two verified phones** on the **same ZIP/block**.

### Swap E2E

| Account A | Account B | Then either says |
|-----------|-----------|------------------|
| `I have kid rain boots size 3T` (`sharing.swap`) | `I'm looking for 3T rain boots` (`looking.swap`) | `show my block log` |

**Expect:** Block log shows swap match (not meet wording).

### Meet E2E

| Account A | Account B | Then either says |
|-----------|-----------|------------------|
| `looking for a walking buddy on weekends` (`looking.meet`) | `I want to host a weekend walking meetup` (`sharing.host`) | `show my block log` |

**Expect:** “A neighbor wants to meet — you offered to host” (or similar). **Not** “kid bicycle…” unless someone still has an old **swap** signal listening.

---

## Part 5 — Quick checklist (all Layer 1)

Copy for each release:

- [ ] **Sign in** — ZIP, identity, name, phone, OTP  
- [ ] **Discovery** — peers, attrs, block summary, block log, activities  
- [ ] **Identity** — show profile, add claim, edit claim, complete profile  
- [ ] **Looking** — swap, meet, tip  
- [ ] **Sharing** — swap, host, tip  
- [ ] **Intros** — propose, list, accept/decline  
- [ ] **Auth** — signup, login, logout, photo  
- [ ] **Settings** — name, ZIP, notifications  
- [ ] **Help** — what can you do, who are you  
- [ ] **Two-account** — swap match + meet match on same block  

---

## Edge cases (one pass)

| You say | Expect |
|---------|--------|
| Swap/tip with no ZIP | “What ZIP are you in?” |
| Swap while not verified | “Verify your phone first…” |
| `boots` only | “Can you be more specific — size, brand?” |
| Gibberish | Generic chat, no `active_intent` |

---

## Appendix — Automated smoke (backend)

```bash
cd services/lana-worker
PYTHONPATH=. .venv/bin/python -m unittest \
  tests.test_layer1_intents \
  tests.test_local_signals \
  tests.test_discovery_route -q
```

## Appendix — Postman

`docs/postman/TagAlng-Lana-Unified-Full-E2E.postman_collection.json` — folders **A → E** for API E2E without the PWA.

## Appendix — Code references

- Intent list: `services/lana-worker/app/layer1_intents.py`  
- Architecture: `docs/Lana Agentic Architecture/LANA_INTENTS_AND_ROUTING_v1.md`
