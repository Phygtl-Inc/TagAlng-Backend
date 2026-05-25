# TagAlng Admin (Next.js)

Phygtl internal admin at `/admin`. Calls the same Supabase project as TagAlng-app.

**Phase 1 pages to build:**

- Atlas dashboard — `get_atlas_snapshot` RPC
- Waitlist tooling (service-role server routes only)

Scaffold with:

```bash
npx create-next-app@14 . --typescript --app --eslint
```

Use `@supabase/ssr` for server-side Supabase client.
