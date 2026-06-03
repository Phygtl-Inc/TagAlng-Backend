# Dev seed data (users, events, RTJ)

**File:** [`supabase/seed.sql`](../supabase/seed.sql)  
**Apply on tagalng-dev:** `supabase db query --linked -f supabase/seed.sql`

## Test accounts

| Phone | OTP | Nickname | Role |
|-------|-----|----------|------|
| `+15550100001` | `000000` | Marina | Host |
| `+15550100002` | `000000` | Beatriz | Guest (RTJ) |
| `+15550100003` | `000000` | Carla | Peer |

Add all three in **Supabase Dashboard → Auth → Phone → Test numbers**.

## Seeded data

- 4 open events in `lake-nona`
- 1 pending + 1 approved RTJ
- 7 identity claims (shared `parents_toddlers` for match testing)
- 1 nudge

## Postman

Use `seed_phone_host` in environment, or run **B1/B2** with `+15550100001`, then **C0 → C1b** for ZIP block flow.

See [`FRONTEND_API.md`](./FRONTEND_API.md) for full API order.
