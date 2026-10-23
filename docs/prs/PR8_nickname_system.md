# PR8 · Display-name + handle ("nickname") system

**Status:** spec + migration written, verified against PROD in a rolled-back transaction. Not pushed, no GitHub PR.
**Migration:** `prs/PR8_nickname_system.sql` → `supabase/migrations/20260917130000_nickname_system.sql`
**Owner to review/push:** Asjid (schema gatekeeper). Worker changes: Asjid + Yunchao. FE: Abdullah.
**Tickets it closes:** Ankit's *AI nickname generation* + *AI "about you" section*.
**Verified against:** Supabase PROD `kmetmatfxdkrialwrnzj`, 2026-07-30.

---

## 1 · The problem, precisely

From the 2026-07-30 standup: Lana asks *"tell me your name"*, gets a first name, writes it to `users.nickname`, and that single value is simultaneously asked to be a warm human name **and** a unique network identifier. It cannot be both.

What is actually in PROD today (verified, not remembered):

| Fact | Evidence |
|---|---|
| `users` has **26 columns**, including both `nickname text NULL` and `full_name text NULL` | `information_schema.columns` |
| **31 users total. 7 have a `nickname`. 0 have a `full_name`.** | `select count(*), count(nickname), count(full_name) from users` |
| All 7 nicknames are Capitalised first names, 4–7 chars, **no digits, no spaces**, all distinct | masked sample below |
| **No unique index on `nickname`** — the only unique indexes on `users` are `users_pkey(id)` and `users_email_lower_idx (UNIQUE lower(email)) WHERE email IS NOT NULL` | `pg_indexes` |
| **No RPC writes `nickname`.** `handle_new_user()` inserts only `(id, phone, email, referred_by)` | `pg_get_functiondef` |
| The lana-worker writes `users.nickname` **directly with the service role** | absence of any write RPC + Tier-2 architecture in `_CODE_TRUTH_2026-07-30.md` |
| A client can already `PATCH /users?id=eq.<self>` and set any nickname with **zero validation** | RLS policy `users_update_own` — `UPDATE ... using (id = auth.uid())` |
| `get_my_profile()` **already returns a `handle` key — aliased to `u.nickname`** | `'handle', u.nickname` in the function body |
| `get_peer_profile()` returns `nickname` but never `full_name` | function body |
| `get_profile_summary()` (the anon/Stranger card) returns `'nickname', null, 'is_blurred', true` and **nothing renderable in its place** — this is Abdullah's "non-signed-in users show initials", and the initials are being computed client-side from data the RPC doesn't send | function body |
| **No `citext`, no `unaccent`, no `pg_trgm`** installed | `pg_extension` |
| `blocks` has 7 rows, 1 cluster (`lake-nona`), `display_name` like `Foster City (94404)` | `select * from blocks` |

Masked nickname distribution (7 rows, first letter only):

```
M****   len 5   2026-06-29   phone
T******  len 7  2026-07-27   email
A****   len 5   2026-07-27   email
R***    len 4   2026-07-27   email
N******  len 7  2026-07-28   email
S****   len 5   2026-07-28   email
D*****  len 6   2026-07-30   email
```

Seven for seven: a capitalised bare first name. Exactly the "tell me your name" failure mode, and exactly the thing that will collide the moment two Marias join one block.

---

## 2 · Research — how everyone else solves this

The consistent finding across every platform I looked at is that **nobody makes one field carry both jobs**. The platforms that tried it later split it, at enormous cost.

### 2.1 Discord — the cautionary tale

Discord ran `Name#1234` for a decade specifically so that *"you could always be 'Alex' without worrying about availability"* — the discriminator absorbed all collisions so the visible name stayed human. In May 2023 they forced everyone onto a single globally-unique lowercase username. The backlash was severe enough to make general news, and the migration had to be run in waves (Nitro subscribers and older accounts first), which produced its own land-grab controversy.

The part people forget: Discord **did not** end up with one field. They ended up with **two** — a unique `username` and a free-form, non-unique `display_name`. The unique thing is the address; the human thing is what you see. ([Discord blog](https://discord.com/blog/usernames), [Discord support](https://support.discord.com/hc/en-us/articles/12620128861463-New-Usernames-Display-Names), [Fortune on the backlash](https://fortune.com/2023/05/09/discord-forcing-millions-change-username-upset-gamers))

**Lesson for us:** if you retrofit global uniqueness onto a name people already own, you pay in churn and in ugly names. Do the split *now*, at 31 users, when it's free.

### 2.2 Signal — uniqueness that is deliberately invisible

Signal added usernames in Feb 2024. Their design is the strongest match for a trust product: *"A username is not the profile name that's displayed in chats, it's not a permanent handle, and not visible to the people you are chatting with. If someone reaches out to you by username and you accept their message request, your username will be replaced in the chat by your profile name."*

So the unique identifier exists purely as a **connection mechanism**, and the instant a relationship is established it is replaced by the human name. ([Signal support](https://support.signal.org/hc/en-us/articles/6712070553754-Phone-Number-Privacy-and-Usernames), [Signal blog](https://signal.org/blog/phone-number-privacy-usernames/))

**Lesson for us:** the handle earns its keep at Stranger→Nudge (finding, inviting, deep-linking). From Acquaintance up it should recede behind the display name. This maps 1:1 onto the Lana relationship-tier ladder.

### 2.3 Bluesky — the handle is a credential, not a name

Bluesky separates `handle` from `display name` and notes plainly that *"most people will see your display name before your username anyway."* Their innovation is that the handle carries **verification meaning** (a domain you control) rather than identity meaning. ([Bluesky handle tutorial](https://bsky.social/about/blog/4-28-2023-domain-handle-tutorial), [analysis](https://blog.giovanh.com/blog/2024/12/03/verification-on-bluesky-is-already-perfect/), [TechRadar](https://www.techradar.com/computing/social-media/bluesky-just-made-it-harder-for-someone-to-steal-your-name-but-verification-is-still-a-challenge))

**Lesson for us:** a handle that encodes something *true and local* (`maria.fostercity`) is worth more in a neighbourhood product than one that encodes nothing (`maria47`). The suffix ladder in this PR is built on that.

### 2.4 Nextdoor — real names, and why we should not copy it wholesale

Nextdoor requires *"the first name that you use when introducing yourself to neighbors... and your legal last name"*, bans aliases/initials/abbreviated surnames, requires address verification, and suspends accounts for fake names. Their stated theory is that it *"lowers the incentive for behaving badly."* ([Nextdoor: About using real names](https://help.nextdoor.com/s/article/About-using-real-names), [Moderator guidelines](https://www.nextdoorneighborhoodteams.com/public/resources/moderator-academy-the-guidelines))

Two things to take and one to leave:

- **Take:** real-name *capture* is what creates accountability, and Nextdoor themselves note *"while Nextdoor requires you to sign up with your real name, you don't have to display it on your profile"* — capture ≠ display.
- **Take:** a real surname is the best possible disambiguator between two Marias.
- **Leave:** compulsory public legal surnames. Our users are parents coordinating around children; a public legal surname next to a home block is a safety liability. And an enforcement regime (report → suspend) is not something a 31-user pilot with **no safety endpoint at any tier** (`_CODE_TRUTH` §"What survives unchanged as real gaps" #3) can operate.

### 2.5 The pseudonymity research — the counter-intuitive result

The intuition "real names = trust" does not hold up. A 1,000-user study found people **trusted pseudonyms and real names equally**, and the Disqus/Coral-style work found comment quality improved markedly when moving from disposable anonymity to **durable pseudonyms** — comments under durable pseudonymity were rated *highest* quality, above real names. In pseudonymous systems trust is built from *"soft identity metrics — the quality of one's content, consistency of past actions, and community participation."* ([Disqus](https://blog.disqus.com/whats-in-a-name-understanding-pseudonyms), [The Conversation on stable pseudonyms](https://theconversation.com/online-anonymity-study-found-stable-pseudonyms-created-a-more-civil-environment-than-real-user-names-171374), [Higher Logic](https://www.higherlogic.com/blog/the-power-of-pseudonyms-in-an-online-community/))

**Lesson for us:** what generates trust is **durability + accumulated history**, not legal identity. Which is excellent news, because we already have the history: `weeks_here`, `event_count`, `shared_claim_count`, `about_tags` are all already returned by `get_profile_summary`. A stable name attached to a visible record beats a legal name attached to nothing. This is the strongest argument that we do **not** need a Nextdoor-grade real-name mandate.

### 2.6 Dating apps — first-name-only is the norm and it works

Hinge shows first name on the card with surname optional and user-controlled; Bumble seeds the name from a connected account and restricts editing specifically to deter fake profiles, while explicitly permitting *"initials, abbreviations, contracted/shortened versions of their name or middle name as long as these names are a variation of their authentic name."* ([Bumble guidelines](https://bumble.com/guidelines/inauthentic-profiles), [Hinge naming](https://dude-hack.com/should-i-use-my-real-name-on-hinge/))

**Lesson for us:** "Maria" is a *complete* and socially normal answer for a person-to-person product. The current Lana ask isn't wrong — it's just being asked to do a second job it can't do.

### 2.7 Collision UX and disambiguation

- Validate availability **inline, as you type** — the classic failure is making the user discover "username already exists" only on submit ([Authgear](https://www.authgear.com/post/login-signup-ux-guide/), [LearnUI](https://www.learnui.design/blog/tips-signup-login-ux.html)).
- Question whether you need a user-facing username at all when you already have a stronger key — we do: `phone` / `email` (with a UNIQUE index) are already the login identity ([LearnUI](https://www.learnui.design/blog/tips-signup-login-ux.html)).
- On identical display names, the field-tested fix is a **contextual extra distinguisher** (last initial, a secondary attribute) rather than mutating the stored name; and colliding identities *"break the platform trust model and supercharge impersonation"* when left unresolved ([enterprise disambiguation threads](https://experienceleaguecommunities-beta.adobe.com/workfront-23/provide-additional-info-email-in-at-tag-options-to-help-distinguish-people-with-the-same-name-133917), [impersonation pattern](https://auditbuffet.com/patterns/ab-000427)).
- Adjective-noun-number generators (`SwiftBadger42`) are ubiquitous ([examples](https://github.com/liverfail/username-generator)) and are exactly what Tommaso means by "cryptic". Reddit-style random handles work on Reddit because Reddit is topic-first and pseudonymous by design. **A neighbourhood product is person-first.** `SwiftBadger42` next to "3 gatherings, 11 weeks here, Foster City" actively destroys the credibility the rest of the card is building.

### What actually works, condensed

1. **Two fields, always.** Unique machine identifier ≠ human display name.
2. **Never auto-suffix the visible name.** Suffix the invisible one.
3. **The unique one should be boring and recede.** Signal's "replaced by profile name" is the ideal.
4. **Uniqueness has to be enforced in the database**, not in the app. A `UNIQUE` index or it doesn't exist.
5. **Disambiguate at render time, in context**, using truthful extra signal (last initial, neighbourhood).
6. **Trust comes from durability + history**, not legal names. We already have the history.
7. **Digits are a last resort, not a strategy.**

### Sources

- [Evolving Usernames on Discord](https://discord.com/blog/usernames)
- [New Usernames & Display Names – Discord Support](https://support.discord.com/hc/en-us/articles/12620128861463-New-Usernames-Display-Names)
- [Discord forcing millions to change username upsets gamers — Fortune](https://fortune.com/2023/05/09/discord-forcing-millions-change-username-upset-gamers)
- [Discord is scrapping its numbers — TechRadar](https://www.techradar.com/news/discord-is-scrapping-its-numbers-in-favor-of-something-more-unique)
- [Phone Number Privacy and Usernames – Signal Support](https://support.signal.org/hc/en-us/articles/6712070553754-Phone-Number-Privacy-and-Usernames)
- [Keep your phone number private with Signal usernames](https://signal.org/blog/phone-number-privacy-usernames/)
- [How to verify your Bluesky account (domain handles)](https://bsky.social/about/blog/4-28-2023-domain-handle-tutorial)
- [Verification on Bluesky is already perfect — GioCities](https://blog.giovanh.com/blog/2024/12/03/verification-on-bluesky-is-already-perfect/)
- [Bluesky strengthens impersonation policy — TechRadar](https://www.techradar.com/computing/social-media/bluesky-just-made-it-harder-for-someone-to-steal-your-name-but-verification-is-still-a-challenge)
- [About using real names — Nextdoor Help](https://help.nextdoor.com/s/article/About-using-real-names)
- [Moderator Academy: The Guidelines — Nextdoor](https://www.nextdoorneighborhoodteams.com/public/resources/moderator-academy-the-guidelines)
- [What's In A Name? Understanding Pseudonyms — Disqus](https://blog.disqus.com/whats-in-a-name-understanding-pseudonyms)
- [Online anonymity: stable pseudonyms created a more civil environment — The Conversation](https://theconversation.com/online-anonymity-study-found-stable-pseudonyms-created-a-more-civil-environment-than-real-user-names-171374)
- [The Power of Pseudonyms in an Online Community — Higher Logic](https://www.higherlogic.com/blog/the-power-of-pseudonyms-in-an-online-community/)
- [Bumble Community Guidelines — inauthentic profiles](https://bumble.com/guidelines/inauthentic-profiles)
- [Should You Use Your Real Name on Hinge?](https://dude-hack.com/should-i-use-my-real-name-on-hinge/)
- [Login & Signup UX guide — Authgear](https://www.authgear.com/post/login-signup-ux-guide/)
- [15 Tips for Better Signup / Login UX — LearnUI](https://www.learnui.design/blog/tips-signup-login-ux.html)
- [No impersonation — name and icon don't mimic existing apps](https://auditbuffet.com/patterns/ab-000427)
- [Distinguishing users with the same name — Adobe Workfront community](https://experienceleaguecommunities-beta.adobe.com/workfront-23/provide-additional-info-email-in-at-tag-options-to-help-distinguish-people-with-the-same-name-133917)

---

## 3 · The design

### 3.1 Three fields, three jobs

| Column | Job | Unique? | Visible to | Who writes it |
|---|---|---|---|---|
| `users.nickname` *(existing)* | **Display name.** The name this person goes by. Warm, any script, spaces and apostrophes fine. | **No — never** | Nudge tier and up | Lana (conversation) or the user (profile) |
| `users.handle` **(new)** | **Address.** Globally unique, lowercase, `@maria.fostercity`. Deep links, invites, @-mentions, search. | **Yes, globally, case-insensitively** | Profile detail + invite surfaces. Recedes once a relationship exists (Signal model). | Auto-derived, user-overridable |
| `users.full_name` *(existing, 0 rows populated)* | **Accountability.** Real name. Private. Source of a last initial. | No | **Nobody** — never returned by `get_peer_profile` or `get_profile_summary` | The user, optional, asked only when it earns its keep |

`users.nickname` keeps its name and its ~36 existing read-sites. Nothing is renamed. `get_my_profile` and `get_peer_profile` gain a `display_name` key that is an alias of `nickname`, so the FE can migrate at leisure.

### 3.2 Uniqueness scope — **globally unique handle, no uniqueness at all on the display name**

The brief asked whether to scope uniqueness globally or per block/neighbourhood. **Per-block is the wrong answer, for four concrete reasons grounded in this schema:**

1. **Our feeds are not block-scoped.** `get_nearby_activities(_authed)` filters on `st_distance(e.location, point)` within a radius — not on `home_block_id`. Two users in *different* blocks routinely appear in the same participant list. Block-scoped uniqueness would permit exactly the collision it claims to prevent.
2. **`home_block_id` is nullable and mutable.** People move, and 24 of 31 rows have no nickname yet, so block assignment is not a stable key. Block-scoped uniqueness means a re-collision every time someone relocates — you'd have to force-rename an existing user, which is the one thing every platform in §2 says never to do.
3. **The cluster granularity is currently degenerate.** All 7 blocks sit in one cluster (`lake-nona`), including `zip-10001` (New York) and `zip-94404` (Foster City). Scoping to `cluster_id` today is *de facto* global anyway, but with a bug waiting for the day the cluster data is fixed.
4. **Global handle uniqueness costs nothing, because the handle is not the pretty name.** The "Maria47 ugliness" objection is real — but it's an objection to *global uniqueness on the visible name*, not to global uniqueness per se. Once the visible name is exempt from uniqueness, a globally-unique handle is free.

So:

> **`handle` is globally unique, case-insensitively, enforced by `CREATE UNIQUE INDEX users_handle_lower_uidx ON users (lower(handle)) WHERE handle IS NOT NULL`.**
> **`nickname` has no uniqueness constraint of any kind and never will.**

`handle` is **nullable**, and NULL is a legitimate steady state (see §3.5). A partial unique index permits unlimited NULLs.

### 3.3 Collision UX — two different collisions, two different answers

**Collision A — two people want the same handle.** Invisible to the user, resolved by the generator before anyone sees a conflict. The ladder, most-human first:

| # | Strategy | Example |
|---|---|---|
| 1 | bare slug of the display name | `maria` |
| 2 | slug + real last initial (from `full_name`) | `maria.k` |
| 3 | slug of the full name | `mariakowalski` |
| 4 | slug + **their actual neighbourhood** | `maria.fostercity` |
| 5 | slug + **their actual ZIP** | `maria.94404` |
| 6 | slug + digit — **last resort, interactive only** | `maria2` |

Every rung except the last is *true information about that specific person*. That is the anti-cryptic guarantee: a suffix is never noise, it's always a fact. Verified output on prod:

```
suggest_handles('Aurelia','Aurelia Costa','zip-94404','94404',5)
  → {aurelia, aurelia.c, aureliacosta, aurelia.fostercity, aurelia.94404}
suggest_handles('Maria','Maria Kowalski','zip-94404','94404',5)   -- 'maria' already taken
  → {maria.k, mariakowalski, maria.fostercity, maria.94404, maria2}
```

Rung 6 is gated behind `p_allow_numeric`. The **automatic** path (Lana capturing a name, and the backfill) passes `false`: if the ladder yields nothing human, we set `handle = NULL` and let the profile ask, rather than silently branding someone `lana2`. The **interactive** path (`check_handle_available`, i.e. the user is looking at a "that one's taken" message) passes `true`, because there a numbered option is a helpful suggestion, not an imposition.

**Who gets asked to change: the second person, always, and structurally.** The backfill iterates `ORDER BY created_at ASC` and the unique index makes the earlier claim win. `set_my_display_name` never rewrites another user's handle, and *changing your display name does not re-derive your handle* (`if v_me.handle is not null then v_handle := v_me.handle`) — so an existing member's address can never be yanked out from under them by someone else's rename. Verified: with `maria` claimed by user A, user B gets `maria.k` and A's handle is untouched.

**Collision B — two people are genuinely both called Maria.** This is the one Tommaso actually cares about ("it wouldn't look good"), and it is *not* a uniqueness problem. Both of them **are** Maria; forcing one to be "Maria2" would be a lie. Solved at **render time**, in context, by `disambiguate_display_names(uuid[])`:

- Feed it the user_ids about to appear together on one surface (event participant list, peer carousel, chat member list).
- Only rows whose lowercase display name actually clashes get a suffix. Everyone else renders bare.
- Suffix precedence: **real last initial** → **neighbourhood name** (only if `home_location_visibility = 'block'`, so it respects the existing privacy setting) → `@handle`.

Verified on prod with three users forced to `nickname = 'Maria'`:

```
Maria K.                      [MK]  dup=true    (has full_name)
Maria B.                      [MB]  dup=true    (has full_name)
Maria · Lake Nona — Area A    [M]   dup=true    (no full_name → block name)
Natasha                       [N]   dup=false   (untouched)
```

This is the Nextdoor surname benefit without the Nextdoor surname mandate: we only ever surface an *initial*, only when there's an actual clash, and only to people who are already seeing each other.

### 3.4 The ask — how Lana captures this

Still **one warm question**. We are not adding a second onboarding field. Per §2.6, "Maria" is a complete answer.

```
Lana:  What should I call you?
User:  Maria
       → nickname = 'Maria'  (display_name_source = 'lana')
       → handle   = suggest_handles('Maria', …, allow_numeric := false)[1]
```

Lana **never asks for a handle.** She derives it and mentions it once, only if it is not simply the name:

> *"Got it, Maria. Around here you'll show up as **Maria** — and if anyone needs to link straight to you it's **@maria.fostercity**. You can change either one any time."*

If the handle came out exactly equal to the slug of the name (`maria`), she says nothing at all. Silence is the good case.

**When Lana suggests versus asks:**

| Situation | Behaviour |
|---|---|
| Name slugs cleanly and the bare handle is free | **Assign silently.** No mention. |
| Name slugs cleanly but is taken → a *human* rung fires (`maria.k`, `maria.fostercity`) | **Assign + mention once**, as above. |
| Ladder produces only numbered options | **Do not assign.** `handle = NULL`. Profile shows a "pick your @" prompt. |
| Name is in a non-Latin script (`京子`) | **Do not assign.** `lana_slugify` returns NULL by design. Display name is preserved perfectly as `京子`; the handle is asked for separately, later, in the profile. |
| Name is 1–2 characters and there's no full name or block | **Do not assign.** Verified: `suggest_handles('A', null, null, null, 3) → {}` — never `ax2`. |
| Handle is reserved (`lana`, `admin`, `support`, `report`, …) | **Do not assign** on the auto path. Verified: `suggest_handles('Lana', …, allow_numeric := false) → {}`. |

**What makes a good suggestion** — the rules the generator enforces:

- It must be **derived from something true about this person**: their name, their surname initial, their neighbourhood, their ZIP. Never a random word.
- It must be **sayable out loud**. A neighbour should be able to tell another neighbour their handle in a school pickup line.
- It must **never introduce a number the user didn't choose**.
- It must **never introduce a letter the user didn't supply** — this is why the 1–2 character branch prefers the real full name (`Jo` + `Jo Ann` → `joann`, not `joj`).
- It must be **lowercase, 3–24 chars, `^[a-z][a-z0-9]*([._][a-z0-9]+)*$`** — enforced by a CHECK constraint, not by convention.

`full_name` is **never** asked for up front. It is asked only when it buys the user something concrete — first time they host a gathering ("hosts show a last initial so people know who they're meeting") — which is also when accountability genuinely matters. Until then it stays NULL and the disambiguator falls through to the neighbourhood.

### 3.5 NULL handle is a feature, not a gap

Any user may sit at `handle IS NULL` indefinitely. It means "not @-addressable yet". They still have a display name, a profile, initials, events, everything. This is what makes the design honest for non-Latin scripts and for people whose name is genuinely 2 letters: we never invent an identity for someone we can't derive one for. The FE shows a single dismissible nudge in Settings; nothing is blocked.

### 3.6 Relationship tiers — what renders at each rung

Grounded in the existing 5-tier ladder (`project_lana_relationship_tiers`) and in what each RPC actually returns.

| Tier | RPC | Renders |
|---|---|---|
| **Stranger** (anon / not matched) | `get_profile_summary` | **`initials` only** (`M`, `MK`) + `about_you` + `weeks_here` + `event_count` + `about_tags`. `nickname`, `display_name`, `handle`, `avatar_url` all stay `null`, `is_blurred: true`. **This PR adds `initials` and `about_you` to that payload** — closing Abdullah's gap where the FE was inventing initials client-side from data it wasn't given. |
| **Nudge** | `get_peer_profile` (authed, not matched) | `display_name` + `handle` + public claims. Handle is visible here because this is the tier where you might need to *find* or *link to* someone. |
| **Acquaintance / Direct** | `get_peer_profile` (matched) | `display_name` foregrounded, `handle` de-emphasised to profile-detail only (Signal model), + mutual claims + shared events. |
| **IRL** | same + `disambiguate_display_names` | `Maria K.` appears only if another Maria is in the same room/list. |
| **Self** | `get_my_profile` | everything, including `full_name`, `handle_set_by`, `about_you_draft`. |

`full_name` is returned at **no** peer tier. Only `lana_initials()` / `lana_last_initial()` derivatives ever escape.

### 3.7 "About you"

Three columns, one rule: **Lana drafts, the user owns.**

| Column | Written by | Meaning |
|---|---|---|
| `about_you_draft` / `about_you_draft_at` | worker, via `set_about_you_draft(uuid, text)` (service_role only — `EXECUTE` revoked from `public`, `authenticated`, `anon`) | Lana's proposal. **Never rendered to peers.** |
| `about_you` / `about_you_source` / `about_you_updated_at` | the user, via `set_my_about_you(text, source)` | The published blurb. |

**How it's generated.** Only from `user_identity_claims` where `dismissed_at IS NULL AND disclosure = 'public'` — i.e. claims the user has *already* consented to publish. No new inference, no new disclosure. The worker prompt (§4) turns the top claims into one first-person sentence; `generate_about_you_draft(uuid)` is the deterministic SQL fallback so the section is never empty and so we can diff LLM output against ground truth.

The fallback is deliberately a **tag list**, not prose:

```sql
select public.generate_about_you_draft('<uid>');
-- 'Around here: Paulista · 14-month-old · faith'
```

Raw claim labels (`14-month-old`, `Paulista`) do not survive being glued into a sentence. A fallback that reads plainly is better than one that reads *wrong*. Returns `NULL` when there are no public claims — never a hallucinated blurb.

**How it's kept honest:**
- It can only ever restate claims the user already made public. If they dismiss a claim, the next draft drops it.
- `about_you_source` records whether the live text is `'lana'` (accepted as drafted) or `'user'` (typed/edited).
- 280-char ceiling, CHECK-enforced.
- Fully editable and clearable (`set_my_about_you(null)` wipes text, source and timestamp together).
- Nothing is published without a user action. `about_you` is never written by the worker.

### 3.8 Writes are locked down

Today `users_update_own` lets any client PATCH `nickname` — and would let it PATCH `handle` — with no validation whatsoever. This PR adds a `BEFORE UPDATE OF handle` trigger, `users_guard_handle()`, that raises `handle_write_requires_rpc` when `auth.role() = 'authenticated'` and the transaction-local flag `lana.handle_write` isn't set. Only `set_my_display_name` / `set_my_handle` set that flag. The service role and migrations are unaffected.

`nickname` stays directly PATCHable (back-compat with the shipped FE and the worker), but is now covered by `users_nickname_shape_chk` (`NOT VALID` — enforced on every new write, never trips the 7 legacy rows): 1–40 chars trimmed, no control characters.

---

## 4 · Worker changes (`services/lana-worker`, Asjid + Yunchao)

### 4.1 The name-capture prompt

Current behaviour asks *"tell me your name"* and writes the answer straight to `users.nickname`. Change:

**Prompt delta** — the name-capture turn should be:

```
What should I call you?
```

not "tell me your name". It licenses "Maria", "Mari", "Maria K" equally, which is what we want. Then:

- Write the answer verbatim to `users.nickname`, trimmed. **Do not lowercase it, do not strip accents, do not truncate below 40 chars.** `María-José` must survive as `María-José`.
- Set `users.display_name_source = 'lana'`.
- **Stop treating `nickname` as an identifier.** Any worker code that assumes nickname uniqueness (dedupe, lookup-by-name, cache key) must move to `id` or `handle`.

### 4.2 Handle derivation — call the DB, do not reimplement

```
handle := rpc suggest_handles(
            p_seed          => <captured name>,
            p_full_name     => <users.full_name, may be null>,
            p_block_id      => <users.home_block_id>,
            p_zip           => <users.home_zip>,
            p_limit         => 1,
            p_exclude_user  => <user_id>,
            p_allow_numeric => false          -- ← non-negotiable on the auto path
          )[1]
```

If the array is empty, leave `handle` NULL and **say nothing about it** in the conversation. Do not improvise a handle in Python — the ladder, the reserved list and the uniqueness check all live in one place on purpose, and the unique index will reject anything the worker invents anyway.

The confirmation line is emitted **only** when `slugify(nickname) <> handle`. Copy in §3.4. Neutral lingo — "around here", "anyone", never "mom".

### 4.3 "About you" drafting

New step, run after the profile-intake session completes (`purpose = 'profile_intake'`, or on `/lana/sessions/{id}/complete`):

1. Read the user's public claims (`disclosure = 'public'`, `dismissed_at IS NULL`), top ~6 by confidence.
2. Prompt the synth model for **one sentence, first person, ≤ 280 chars, ≤ 25 words**, using **only** the supplied claims. Explicit constraints in the system prompt:
   - *Use only the facts provided. Do not add, infer, or embellish.*
   - *No "mom", "mother", "mum". Use "neighbour" or "people" if a collective noun is needed.*
   - *No superlatives, no marketing voice, no emoji.*
   - *If fewer than two claims are supplied, return nothing.*
3. Write it with `set_about_you_draft(user_id, draft)` — **service_role only**.
4. Surface it in-chat as an accept/edit affordance. On accept, the FE calls `set_my_about_you(text, 'lana')`. **The worker must never write `users.about_you` directly.**
5. If the model returns nothing, fall back to `generate_about_you_draft(user_id)`.

Re-draft on a cadence (e.g. when public claim count changes by ≥3), never silently republish — always back into `about_you_draft`.

### 4.4 Guardrail

Add a check to the existing output guardrail: reject any generated `about_you` draft containing a token not traceable to a supplied claim label or a small allowlist of connectives. This is the "kept honest" enforcement and it's cheap.

---

## 5 · Frontend changes (`tagalng-pwa`, Abdullah)

There is currently **no `/profile` route** (`_CODE_TRUTH` Tier 1: the only user-reachable routes are `/chat`, `/meet/[id]`, `/signin-required`). So this is mostly new surface, not a refactor.

### 5.1 Read-contract changes — do these first, they're the breaking bit

| Key | Before | After |
|---|---|---|
| `get_my_profile().handle` | **aliased to `nickname`** | the **real** `users.handle` (may be `null`) |
| `get_my_profile().display_name` | — | **new**, = `nickname` |
| `get_my_profile().initials` / `.about_you` / `.about_you_draft` / `.about_you_source` / `.display_name_source` / `.handle_set_by` | — | **new** |
| `get_peer_profile().display_name` / `.handle` / `.initials` / `.about_you` | — | **new** |
| `get_profile_summary().initials` / `.about_you` / `.display_name` / `.handle` | — | **new** (`display_name` and `handle` are always `null` here by design) |

⚠️ **Anywhere the FE renders `profile.handle` as a person's name today, it will start rendering the new handle (or `null`).** Switch those call sites to `display_name` (falling back to `nickname`) *before* this migration ships. This is the single coordination item between Abdullah and Asjid.

### 5.2 Stop computing initials client-side

`get_profile_summary` now returns `initials`. Render that. The client-side derivation is wrong for anyone with a `full_name` (it can't see one) and wrong for non-Latin scripts.

### 5.3 New: profile name editor

- **Display name** — a real `<input type="text">`, `font-size: 16px` minimum (iOS keyboard rule, `CLAUDE.md`), maxlength 40, any script. Saves via `set_my_display_name(nickname, null, null, 'user')`. No availability check — it can't collide.
- **Handle** — a second real input, prefixed `@`, lowercased on input. Debounced ~300ms call to `check_handle_available(text)`, which returns `{handle, available, reason, suggestions[]}`. Render `reason` as copy (`invalid_format` / `reserved` / `taken`) and `suggestions` as tappable chips. **Validate inline, never on submit** (§2.7). Saves via `set_my_handle(text)`.
- **Empty-handle state** — if `handle` is `null`, show a single non-blocking prompt: *"Pick an @ so people can link straight to you."* Prefill from `check_handle_available(display_name)`'s suggestions.
- **Real name** — optional, collapsed by default, with honest helper copy: *"Only used to show a last initial when someone else nearby has the same name. Never shown in full."* Saves via `set_my_display_name(nickname, null, full_name, 'user')`.

### 5.4 New: "about you" editor

- Show `about_you` if set; otherwise show `about_you_draft` behind a *"Lana suggested this — use it?"* accept/edit affordance.
- Accept → `set_my_about_you(draft, 'lana')`. Edit → `set_my_about_you(edited, 'user')`. Clear → `set_my_about_you(null)`.
- 280-char counter. Never auto-publish.

### 5.5 Disambiguation at render

Any surface rendering **more than one person** — event participant lists, peer carousels, chat member lists, the neighbour drawer — should call `disambiguate_display_names(ids[])` once for the visible set and render `display_label`, falling back to `display_name`. Use `needs_disambiguation` to decide whether to also show the block chip.

Single-person surfaces should keep rendering the bare `display_name`. Do not disambiguate where there is nothing to disambiguate.

### 5.6 Copy rules

Neutral lingo throughout. "neighbour" / "people" / "around here". Never "mom", "mother", "mum".

---

## 6 · Test plan

The `.sql` file carries the full runnable plan in its footer. Summary of what has **already been executed against PROD** inside `begin; … rollback;` on 2026-07-30:

| # | Test | Result |
|---|---|---|
| 1 | Migration applies end to end on the live schema | **PASS** — all DDL, 13 functions, 1 trigger, 1 table, 1 unique index |
| 2 | `lana_slugify('María-José Ñuñez')` | `mariajosenunez` |
| 3 | `lana_slugify('Straße Øystein')` | `strasseoystein` |
| 4 | `lana_slugify('京子')` | `NULL` — by design, we ask instead of inventing |
| 5 | `lana_initials('Maria','Maria Kowalski')` / `('Maria', null)` | `MK` / `M` |
| 6 | `lana_place_token('Foster City (94404)')` | `fostercity` |
| 7 | Backfill of all 31 rows | 7 handles created: `maria, tommaso, asjid, rust, natasha, sofia, daniel` — **0 duplicates, 0 numeric suffixes** |
| 8 | Ladder, handle free | `{aurelia, aurelia.c, aureliacosta, aurelia.fostercity, aurelia.94404}` |
| 9 | Ladder, handle taken | `{maria.k, mariakowalski, maria.fostercity, maria.94404, maria2}` |
| 10 | Short name `Jo` + `Jo Ann` | `{joann, joann.a, joann.fostercity, joann.94404}` — no filler letters |
| 11 | Short name `A`, no other signal | `{}` — asks the user, never `ax2` |
| 12 | Reserved seed `Lana`, auto path | `{}` — never `lana2` |
| 13 | Reserved seed `Lana`, interactive path | `{lana2, lana3, lana4}` + `reason: "reserved"` |
| 14 | Duplicate handle | `23505 unique_violation` — **PASS** |
| 15 | Case-variant duplicate `MARIA` | `23514 check_violation` (uppercase blocked before it can even collide) — **PASS** |
| 16 | `'x'` (1 char) | `23514 check_violation` — **PASS** |
| 17 | `'1maria'` (leading digit) | `23514 check_violation` — **PASS** |
| 18 | `'maria kowalski'` (space) | `23514 check_violation` — **PASS** |
| 19 | Second Maria gets `maria.k` | **PASS** |
| 20 | **First Maria keeps `maria`** | **PASS** — second person pays |
| 21 | 3 users forced to `Maria`, disambiguation | `Maria K.` / `Maria B.` / `Maria · Lake Nona — Area A`, others untouched — **PASS** |
| 22 | `get_profile_summary().initials` | `M`, with `nickname` still `null` — **PASS** |
| 23 | `generate_about_you_draft` on a real user | `Around here: Paulista · 14-month-old · faith` |
| 24 | `check_handle_available('lana')` | `{"available": false, "reason": "reserved", "suggestions": ["lana2","lana3","lana4"]}` |
| 25 | `check_handle_available('a')` | `{"available": false, "reason": "invalid_format", "suggestions": [...]}` |
| 26 | Grants resolve on the 7-arg signature | `authenticated:EXECUTE, service_role:EXECUTE` |
| 27 | Transaction rolled back | **confirmed — nothing committed to PROD** |

**Still to test post-apply** (needs a real user JWT, which this session doesn't have):

- Direct `PATCH /users?id=eq.<self>` with a `handle` body under an `authenticated` JWT → expect `P0001 handle_write_requires_rpc`. (The guard's predicate was verified in isolation; `auth.role()` is `null` in a service-role session.)
- `set_my_display_name` / `set_my_handle` / `set_my_about_you` end-to-end under a user JWT, including the `handle_taken` race path.
- Worker regression: a full `profile_intake` session still completes and writes `nickname` + `display_name_source = 'lana'`.

---

## 7 · Migration path for the existing users

31 rows; 7 with a nickname.

1. **Nothing a user typed is touched.** `nickname` is read, never written, by the backfill.
2. Handles are generated `ORDER BY created_at ASC, id ASC` — earliest member wins the clean handle.
3. `p_allow_numeric := false`, so nobody gets a numbered handle they didn't choose.
4. `display_name_source` is set to `'migrated'` only where it was NULL; `handle_set_by = 'auto'`.
5. The 24 rows without a nickname are skipped entirely (`handle` stays NULL) and will get one the next time Lana asks.
6. Idempotent: `where handle is null`. Re-running the migration is a no-op.

Verified result: `maria, tommaso, asjid, rust, natasha, sofia, daniel` — seven clean, human, bare-name handles. Zero collisions, because there are no duplicate first names in the pilot cohort *yet*. That's the whole point of doing this now rather than at 300 users.

**Post-apply comms:** none needed. No visible name changes. If you want to be generous, a one-line in-app note: *"You've got an @ now — @maria. Change it any time in your profile."*

---

## 8 · Rollback

Full ordered rollback block is in `PR8_nickname_system.sql` (bottom). It drops, in dependency order: the trigger, then the 13 functions, then the unique index, then `handle_reserved`, then the 6 CHECK constraints, then the 9 new columns — and re-creates the three profile RPCs at their 2026-07-30 definitions.

**Rollback is lossless for user-authored data.** `nickname` and `full_name` are never modified by this PR. Only derived data (`handle`, `about_you_draft`) and user-authored `about_you` are lost — and `about_you` didn't exist before this PR, so there is nothing to lose on a same-day revert.

### Verbatim originals for the three replaced RPCs

Captured from PROD 2026-07-30 via `pg_get_functiondef`. Restore these on rollback.

```sql
CREATE OR REPLACE FUNCTION public.get_my_profile()
 RETURNS jsonb
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
  select jsonb_build_object(
    'id', u.id,
    'full_name', u.full_name,
    'nickname', u.nickname,
    'handle', u.nickname,
    'phone', u.phone,
    'phone_verified_at', u.phone_verified_at,
    'profile_photo_url', u.profile_photo_url,
    'home_block_id', u.home_block_id,
    'home_zip', u.home_zip,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'home_location_visibility', u.home_location_visibility::text,
    'locale', u.locale,
    'kids_count', u.kids_count,
    'voice_autoplay', u.voice_autoplay,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$function$;
```

`get_peer_profile(uuid)` — original differs from the new version in exactly three places: the `select … into peer` list is `u.id, u.nickname, u.profile_photo_url, u.home_block_id, u.home_location_visibility, b.display_name, b.cluster_id` (no `handle`, no `about_you`, no `full_name`); the anonymous branch has no `display_name` / `handle` / `initials` / `about_you` keys; the authenticated `result` object has no `display_name` / `handle` / `initials` / `about_you` keys. Everything else — claims, `shared_claim_count`, `location_label`, `upcoming_shared_events` — is byte-identical.

`get_profile_summary(uuid)` — original differs in exactly two places: `select id, created_at into v_user` (not `id, created_at, nickname, full_name, about_you`), and the returned object has no `display_name` / `handle` / `initials` / `about_you` keys.

---

## 9 · Open questions for Tommaso

1. **Is a NULL handle acceptable as a permanent state?** This PR says yes (§3.5) — it's what keeps us honest for non-Latin names. The alternative is forcing a numbered handle on those users, which reintroduces exactly the "cryptic name" problem.
2. **When do we ask for `full_name`?** This PR proposes: at first *host* action only. Alternative: never, and let the disambiguator always fall through to the neighbourhood name.
3. **Should the handle be visible at Nudge tier, or only after a match?** Signal hides it entirely once a relationship exists. §3.6 shows it at Nudge because that's where linking/inviting happens — but hiding it earlier is defensible.
4. **Handle change rate limiting.** Not in this PR. At 31 users it's not a problem; at 3,000 it's an impersonation vector (grab a handle someone just released). Suggest a 30-day cooldown + a `handle_history` table in v0.2.
5. **Handle squatting on `/i/[token]` invite links.** Out of scope here, flagging it.
