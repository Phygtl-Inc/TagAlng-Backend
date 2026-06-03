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

## Pages

| Route | Description |
|-------|-------------|
| `/login` | Admin email/password |
| `/lana` | Inbox list + chat thread + claim cards |

## Data

- `admin_list_lana_sessions` — left pane
- `admin_get_lana_conversation` — messages include `metadata.ui` (rendered by `LanaBubble`)

No service role in the browser.
