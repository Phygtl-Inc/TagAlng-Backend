# TagAlng — Supabase setup (backend + admin)

You have the **TagAlng** org on Supabase (Pro). Follow these steps once, then migrations live in `supabase/migrations/`.

## Step 1 — Create the cloud project (dashboard)

1. Open [Supabase Dashboard](https://supabase.com/dashboard) → org **TagAlng**.
2. Click **+ New project**.
3. Suggested settings:

   | Field | Value |
   |-------|--------|
   | Name | `tagalng` (or `tagalng-prod`) |
   | Database password | Strong password → store in 1Password |
   | Region | **East US (North Virginia)** — matches CTO spec `us-east` |

4. Wait until the project is **Active** (~2 minutes).

5. Copy from **Project Settings → API**:

   - Project URL  
   - `anon` **public** key (website + admin client)  
   - `service_role` **secret** key (server-only — never in browser)

6. Copy **Project ref** from URL: `https://supabase.com/dashboard/project/<PROJECT_REF>`

## Step 2 — Install Supabase CLI (your machine)

```bash
brew install supabase/tap/supabase
```

Or: https://supabase.com/docs/guides/cli

Login:

```bash
supabase login
```

## Step 3 — Link this repo to the cloud project

From `/Users/dev/Downloads/TagAlng`:

```bash
cd /Users/dev/Downloads/TagAlng
supabase link --project-ref YOUR_PROJECT_REF
```

## Step 4 — Push migrations (creates all Phase 1 tables)

```bash
supabase db push
```

This applies:

- `blocks`, `waitlist_signups`, `cohorts`, `block_waitlist_counts`, `audit_log`, `analytics_events`
- RLS policies
- RPCs: `join_waitlist`, `get_atlas_snapshot`
- Atlas trigger + `pg_notify` for Realtime

### If `vector` or `postgis` fails on hosted Supabase

Dashboard → **Database → Extensions** → enable **postgis** and **vector**, then run `supabase db push` again.

## Step 5 — Enable Realtime (atlas ticker)

Dashboard → **Database → Replication** (or Realtime settings):

- Add table `block_waitlist_counts` to publication if required by your project version.

For `pg_notify` channel `atlas_block`, admin can subscribe via:

```ts
supabase.channel('atlas').on('broadcast', { event: 'atlas_block' }, ...) 
// or use postgres_changes on block_waitlist_counts
```

Simplest Phase 1: subscribe to **postgres_changes** on `block_waitlist_counts`.

## Step 6 — Env for admin (Next.js, later)

```bash
cp .env.example .env.local
# fill NEXT_PUBLIC_SUPABASE_URL and keys
```

## Step 7 — Smoke test (SQL Editor)

```sql
select * from public.get_atlas_snapshot('lake-nona');

select public.join_waitlist(
  p_city := 'Orlando',
  p_declared_cohorts := array['parents'],
  p_candidate_block_id := '8a2a1072b59ffff',
  p_inbound_ref := 'test-post',
  p_recaptcha_verified := true
);

select * from public.get_atlas_snapshot('lake-nona');
-- signup_count on Block A should be 1
```

## What you have after Phase 1 DB

| Piece | Purpose |
|-------|---------|
| `join_waitlist` RPC | Website/admin waitlist form |
| `get_atlas_snapshot` RPC | Admin dashboard + public ticker |
| `cohorts` table | Validates cohort ids vs `cohorts.yaml` |
| RLS | Public cannot read phone numbers |

## Next (admin portal)

1. Next.js `/admin` with `@supabase/ssr` — server client only for sensitive ops.  
2. Page: atlas map table from `get_atlas_snapshot`.  
3. Waitlist form → server action → `join_waitlist` with reCAPTCHA.  

## Replace placeholder blocks

Update `blocks.id` with real H3 cells from Lake Nona dossier when available.
