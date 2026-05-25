# TagAlng-backend

The TagAlng server side — auth, database, SMS, content. **Day Zero: June 15, 2026.**

This repo holds **everything that runs server-side**: Supabase migrations + edge functions, Twilio SMS handlers, Sanity CMS schemas, the API endpoints that the website (`TagAlng-Web`) and app (`TagAlng-App`) both call.

---

## Services we glue together

| Service | What it does | Cost at launch |
|---|---|---|
| **Supabase** | Postgres + Auth (SMS OTP) + Realtime + Storage + Edge Functions | Free tier through Day Zero |
| **Twilio** | SMS sender (Day-Zero blast + waitlist confirm + RSVP reminders) | ~$0.0079 per SMS · pay-as-you-go |
| **Sanity** | Headless CMS for Reads (blog posts) — the website + app both pull from here | Free tier (3 users · 10k docs) |
| **Vercel** | Hosts the public API endpoints that `tagalng.com` and the app hit | Already provisioned (Pro plan) |

Note Tommaso's rule: **no paid actions without double approval.** Free tiers across the board through launch.

---

## Repo layout (target)

```
TagAlng-backend/
├── supabase/
│   ├── migrations/        # SQL migrations · 20260525000_init.sql, etc.
│   ├── functions/         # Supabase Edge Functions (Deno)
│   │   ├── waitlist/      # POST /waitlist · stores phone + cohort + via
│   │   ├── rsvp/          # commit-unlocks-info state machine
│   │   ├── mutual-meet/   # both-tapped check → reveal real names
│   │   └── cohort-resolve/  # URL ?cohort=... param → resolved cohort set
│   ├── seed.sql           # the 12 Day-Zero events + 14 seed moms
│   └── config.toml
├── twilio/
│   ├── senders.md         # phone numbers + verification status
│   ├── templates.ts       # the 6 SMS templates (launch, RSVP-confirm, host-message, etc.)
│   └── webhooks/          # opt-out + delivery-failure handlers
├── sanity/
│   ├── schemas/           # post, cohort, author, scene
│   ├── sanity.config.ts
│   └── README.md
├── scripts/
│   ├── seed-cohorts.ts    # populates cohorts.yaml into Supabase
│   ├── seed-events.ts     # the 12 Day-Zero events (mirrored from the website)
│   └── seed-moms.ts       # 14 seed moms (Marina, Beatriz, etc.)
├── docs/
│   ├── api.md             # endpoint reference for app + web devs
│   ├── schema.md          # ER diagram of the DB
│   └── runbook.md         # how to roll a release · how to roll back · who to page
├── .env.example
├── .gitignore
├── package.json
├── tsconfig.json
└── README.md (this file)
```

---

## Setup (for a new contractor)

```bash
git clone https://github.com/Phygtl-Inc/TagAlng-backend.git
cd TagAlng-backend
npm install
cp .env.example .env.local       # ask Tommaso for service keys

# Local Supabase (Postgres on Docker)
npx supabase start
npx supabase db reset            # runs migrations + seed.sql

# Edge functions locally
npx supabase functions serve waitlist --env-file .env.local

# Sanity Studio locally (for editing Reads)
cd sanity && npx sanity dev
```

---

## Branch workflow

- `main` — production. Protected. PR-only.
- `staging` — staging Supabase project. All migrations apply here first.
- Feature branches: `feat/<short-name>` from `staging`, PR back into `staging`.

Migrations are append-only — never rewrite a merged migration; always add a new one.

---

## Schema v0 (mirrors the website + app)

```sql
-- 20260525000_init.sql

create table users (
  id uuid primary key default gen_random_uuid(),
  nickname text not null unique check (char_length(nickname) between 2 and 30),
  real_name text,                          -- never returned in API responses by default
  phone text not null unique,              -- E.164 format
  home_zip text not null,                  -- 5-digit US zip
  cohort_tags text[] default '{}',         -- ['brazilian','christian','mom-4-11'] etc.
  language text default 'en' check (language in ('en','pt','es')),
  phone_verified_at timestamptz,
  created_at timestamptz default now()
);

create table events (
  id uuid primary key default gen_random_uuid(),
  host_id uuid references users(id),
  title text not null,
  description text,
  when_at timestamptz not null,
  venue_name text not null,                -- 'Holy Family Church'
  venue_address text,                      -- revealed only after RSVP commit
  identity_cohort text not null,           -- 'christian'
  vicinity_zip text not null,
  activity_tag text not null,              -- 'sunday-service'
  capacity int default 12,
  scene_id text                            -- 'scene-faith' · matches website + app
);

create table rsvps (
  user_id uuid references users(id),
  event_id uuid references events(id),
  state text not null check (state in ('idle','committing','committed','cancelled')),
  committed_at timestamptz,
  cancelled_at timestamptz,
  primary key (user_id, event_id)
);

create table mutual_meet (
  user_a uuid references users(id),
  user_b uuid references users(id),
  event_id uuid references events(id),
  a_tapped_at timestamptz,
  b_tapped_at timestamptz,
  unlocked_at timestamptz generated always as (
    case when a_tapped_at is not null and b_tapped_at is not null
         then greatest(a_tapped_at, b_tapped_at)
    end
  ) stored,
  primary key (user_a, user_b, event_id),
  check (user_a < user_b)                  -- canonical ordering
);

-- RLS policies are non-trivial: see docs/schema.md
```

The full schema, every RLS policy, every column-level constraint and the anti-discrimination "silent-no" architecture are documented in `docs/schema.md` once the repo is populated.

---

## Day-Zero scope (June 15, 2026)

Minimum viable backend. Anything outside this list ships after Day Zero.

1. **Supabase project provisioned** under Phygtl org · free tier
2. **Schema v0 migrated** (users, events, rsvps, mutual_meet)
3. **Twilio account set up** · 1 verified sender phone · `/api/waitlist` endpoint that captures `{phone, cohort, via, language}` and fires a confirmation SMS
4. **The 12 Day-Zero events seeded** (mirror from `TagAlng-Web` HTML)
5. **The 14 seed moms** (Marina, Beatriz, Carla, etc. — for visual social proof; not real accounts)
6. **Day-Zero SMS blast** — scheduled for **June 15 · 9:00am ET**, sent to every verified waitlist phone, language-aware
7. **Sanity dataset** with the 3 LIVE Reads posts ported from the website
8. **Edge function: `cohort-resolve`** — takes URL `?cohort=brazilian,christian` → returns matching events + moms (matches what the website + app both need)

Out of scope until post-Day-Zero: payments, push notifications (in-app SMS-only at launch), creator dashboards, analytics dashboards, multi-tenant CMS.

---

## Faith taxonomy reminder

We use **"Christian"** (broader umbrella), not "Catholic" (a subset). Cohort tag id in DB: `christian`. "Evangelical" remains a distinct cohort id: `evangelical`. Don't conflate.

---

## Anti-discrimination architecture (critical)

The platform **never tells a user who opted out of matching with them**. Implementation: when a user's identity filter excludes another user, that other user simply does not appear in the result set — no notification, no flag, no "she filtered you out" surface anywhere. The Privacy Policy commits to this in writing (§4). The schema enforces it via RLS: a user's filter preferences are visible only to that user; the *effect* of a filter is silent omission, not a signal to the excluded party. Full RLS spec lives in `docs/schema.md`.

---

## Who to talk to

- **Product / design / brand:** Tommaso · t@phygtl.com
- **Mobile app integration:** see `Phygtl-Inc/TagAlng-App`
- **Website integration:** see `Phygtl-Inc/TagAlng-Web`
- **DevOps / infra:** Hermes (TBD on contact handle)

---

## Status

🟡 **Scaffold only.** Empty repo. Contractor TBD. Target: waitlist endpoint live + seeded DB by **May 28, 2026.**
