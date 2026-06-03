# Admin Lana inbox

WhatsApp-style internal UI to read all Lana chats with **bucket / focus_phrase / highlights** from `lana_messages.metadata.ui`.

**App:** `apps/admin` — run locally or deploy to Vercel (internal URL).

---

## Setup

### 1. Database

```bash
supabase db push
```

Migration: `20260605120000_admin_lana_inbox.sql`

### 2. Admin user

1. **Apply fix** if Dashboard shows *“Database error creating new user”*:
   ```bash
   supabase db push
   ```
   Migration: `20260606120000_fix_handle_new_user_email.sql`

2. Supabase Dashboard → **Authentication** → **Users** → **Add user** → **Create new user**
   - Email: e.g. `admin@phygtl.com`
   - Password: your choice (12+ chars)
   - **Auto confirm user:** ON
3. If it still fails: **Logs** → Postgres / Auth — look for `handle_new_user` error. Check the user was not already created (refresh Users list).

4. SQL Editor:

```sql
insert into public.admin_allowlist (user_id, label)
values ('<AUTH_USER_UUID>', 'ops');
```

### 3. Admin app

```bash
cd apps/admin
cp .env.example .env.local
# fill NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY
npm install
npm run dev
```

Open http://localhost:3000 → login → `/lana`

---

## RPCs (used by the app)

| RPC | Purpose |
|-----|---------|
| `admin_list_lana_sessions` | Left pane inbox |
| `admin_get_lana_conversation` | Right pane thread + claims |

Both require admin JWT (`is_tagalng_admin()`).

---

## Message `ui` shape

See [`LANA_API.md`](./LANA_API.md). Admin app renders `messages[].ui` on assistant bubbles.

Example assistant message in the thread:

```json
{
  "role": "assistant",
  "content": "\"Italian, living in the USA\" — what region or country are you most connected to?",
  "ui": {
    "bucket": "heritage",
    "focus_phrase": "Italian, living in the USA",
    "highlights": [
      { "text": "Italian", "bucket": "heritage" },
      { "text": "living in the USA", "bucket": "heritage" }
    ]
  }
}
```

The admin UI shows:

- **HERITAGE · LANA ASKS** pill
- Focus phrase in gold italic
- Highlight chips under the bubble
- User messages in green (WhatsApp-style)
