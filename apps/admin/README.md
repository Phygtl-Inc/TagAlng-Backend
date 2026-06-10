# TagAlng Admin — Lana inbox

WhatsApp-style viewer for **all Lana signup chats**, with **bucket / focus_phrase / highlights** on each assistant message.

## Prerequisites

1. `supabase db push` — migration `20260605120000_admin_lana_inbox.sql`
2. Supabase Auth user (email/password) on `admin_allowlist` — see [`docs/ADMIN_LANA_INBOX.md`](../docs/ADMIN_LANA_INBOX.md)

## Run locally

```bash
cd apps/admin
cp .env.example .env.local
# NEXT_PUBLIC_SUPABASE_URL + NEXT_PUBLIC_SUPABASE_ANON_KEY from Dashboard
npm install
npm run dev
```

Open http://localhost:3001 → `/login` → `/lana`

## Deploy on Vercel

1. Import `Phygtl-Inc/TagAlng-Backend`, branch `main`.
2. **Root Directory:** `apps/admin` (use **Edit** and pick the folder — do not leave repo root).
3. **Framework Preset:** choose **Next.js** manually if the dropdown stays empty, then confirm build command is `npm run build`.
4. Env: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` (anon key only).
5. Deploy. Enable **Deployment Protection** for internal use.

`apps/admin/vercel.json` pins the framework for monorepo imports.

## Pages

| Route | Description |
|-------|-------------|
| `/login` | Admin email/password |
| `/lana` | Inbox list + chat thread + claim cards |
| `/lana/meet` | **PWA guest onboarding demo** — anonymous → Lana chat → joint moment → phone → intro (see [`docs/GUEST_PWA_HANDOFF.md`](../docs/GUEST_PWA_HANDOFF.md)) |

## Data

- `admin_list_lana_sessions` — left pane
- `admin_get_lana_conversation` — messages include `metadata.ui` (rendered by `LanaBubble`)

No service role in the browser.
