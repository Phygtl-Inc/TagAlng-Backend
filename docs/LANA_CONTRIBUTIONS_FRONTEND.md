# Lana — Radar "Your contributions" · Frontend Integration

The **Radar → Your contributions** list (mock `C-CONTRIB-LIST`): one feed of everything
the signed-in user has posted — their `local_signals` (offers / seeks / tips / casual host
asks) **plus** the meets they host (`events`). One RPC, one list, each row tappable.

> **Backend status: done in code; needs the migration applied.**
> Migration `20260719120000_my_contributions.sql` adds `get_my_contributions`. Apply via
> Dashboard SQL Editor or the pooler URL (`...pooler.supabase.com:6543`).
>
> **Frontend status: built** — `src/app/(public)/map/contributions-drawer.tsx`, opened from
> a "Your contributions" button on the Radar (`/map`) screen. This doc is the contract.

---

## RPC: `get_my_contributions(p_since timestamptz default null)`

`security definer`, granted to `authenticated`. Returns a **jsonb array**, newest first.
`p_since` filters by `created_at` (for the today / this week / this month chips); `null` =
everything. Each row:

```ts
interface Contribution {
  kind: 'signal' | 'event';
  id: string;
  intent: string;          // swap_seek | swap_offer | meet_seek | host_meet | tip_seek | tip_share
  title: string;
  category: string | null;
  status: string;          // signal: listening|matched|published|closed|expired · event: open|cancelled|completed
  created_at: string;
  photo_url: string | null;        // signals only
  event_id: string | null;         // event rows only — route a tap to /meet/{event_id}
  starts_at: string | null;        // event rows only
  yes_count: number | null;        // event rows: approved/attended count
  capacity: number | null;         // event rows: max_attendees
  peer_label: string | null;       // matched signals: matched neighbour's nickname ("matched · Sara")
}
```

Helper: `fetchMyContributions(since?: string)` in `src/lib/events.ts`.

## Rendering (matches the mock)

| Source | `intent` | Row label |
|---|---|---|
| `events` I host | `host_meet` | **HOSTING** + `{yes_count} of {capacity} going` |
| `local_signals` | `swap_offer` | **OFFERING** |
| | `swap_seek` | **SEEKING** |
| | `meet_seek` | **LOOKING FOR** |
| | `tip_share` | **SHARING A TIP** |
| | `tip_seek` | **SEEKING A TIP** |

Status pill: `listening · 4d` / `matched · Sara` / `closed · 6w` (signals), `N of M going`
(events).

## Tap → open

- **`kind === 'event'`** → `router.push('/meet/{event_id}')` (the meet page — full open).
- **`kind === 'signal'`** → there is **no signal detail page yet**, so the FE hands off to
  `/chat` (Lana) where the user can relist / cancel / follow up. *(Follow-up: a dedicated
  signal-detail route would make this a true open.)*
