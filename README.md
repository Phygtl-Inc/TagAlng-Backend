# TagAlng-backend

Platform repo for TagAlng: **Supabase** (schema, RLS, RPCs), **admin portal** (Next.js), and later Cloud Run workers.

Consumer app UI lives in **[TagAlng-app](https://github.com/Phygtl-Inc/TagAlng-app)** (frontend team).

## Structure

```
supabase/          # migrations — source of truth for Postgres
cohorts.yaml       # cohort taxonomy (sync with DB seed)
apps/admin/        # Next.js admin (/admin) — Phase 1+
docs/              # setup guides
```

## Quick start

1. Create Supabase project (org **TagAlng**, region **East US**).
2. Follow [docs/SUPABASE_SETUP.md](docs/SUPABASE_SETUP.md).
3. `supabase link` → `supabase db push`.

## Docs (product)

Product PDFs and architecture diagram stay in the docs repo or `/TagAlng` planning folder — not required to run migrations.

## Env

Copy `.env.example` → `.env.local` when adding the admin app (never commit secrets).
