# Community activities + "what it has" — frontend handoff

Backend is live in the worker (`app/place_activities.py`, migration
`20261010120000_place_activities.sql`). Nothing here needs a new screen: the two
lists ride on endpoints you already call.

Two member-curated lists on a community:

| List | What it is | Who owns a row |
|---|---|---|
| **activities** | what a member DOES here — "Aerobics", "Weightlifting" | per member; everyone's are visible to everyone at that place |
| **features** ("what it has") | what the PLACE has — "Pool", "Childcare", "Sauna" | shared fact, one row per place+key, removable only by whoever added it |

Adding an activity also writes it as an identity claim, so it starts shaping the
user's matches — same as if they'd told Lana in chat. Removing it from a
community does **not** delete the interest: they still do it, just not here.

---

## 1. Reads (fields added to endpoints you already use)

### `POST /lana/circles/mine` — each row gains `activities`

```jsonc
{
  "id": "aff-uuid",
  "place_name": "OrangeTheory Narcoossee",
  "detail": "Tuesday 6am spin",
  "activities": [
    { "concept": "aerobics",      "label": "Aerobics",      "member_count": 12, "mine": true  },
    { "concept": "weightlifting", "label": "Weightlifting", "member_count": 9,  "mine": true  },
    { "concept": "spin",          "label": "Spin",          "member_count": 7,  "mine": false }
  ]
}
```

One list, two uses — no second call:

- `mine: true` → the **YOUR ACTIVITIES · 2** chips.
- `mine: false` → the **+ Add more activities · N** menu. These are what other
  members at the same place actually do, which is the only suggestion list that
  is true of the place. Nothing is invented; if the list is empty, show the
  free-text field alone.

Sorted most-shared first.

### `POST /lana/circles/profile` — gains `activities`, and `features[].emoji`

```jsonc
{
  "place_name": "OrangeTheory Narcoossee",
  "features": [
    { "key": "has_pool",      "label": "Pool",      "sub_group": null, "emoji": "🏊" },
    { "key": "has_childcare", "label": "Childcare", "sub_group": null, "emoji": "🧸" },
    { "key": "has_sauna",     "label": "Sauna",     "sub_group": null, "emoji": "🧖" }
  ],
  "activities": [ /* same shape as above, `mine` = the caller's */ ]
}
```

`emoji` is picked server-side when the feature is written (same as
`circle_affiliations.emoji`). **Null on rows learned before this migration** and
whenever the model declines — render the chip without one, never substitute a
local emoji map.

---

## 2. Writes

All four take **either** `affiliation_id` (from `/mine`) **or** `place_id` — same
rule as `/lana/circles/profile`. Membership is re-checked on every call.

| Endpoint | Body | Returns |
|---|---|---|
| `POST /lana/circles/activities/add` | `{ affiliation_id, label }` | `{ place_id, concept, label, already_there }` |
| `POST /lana/circles/activities/remove` | `{ affiliation_id, concept }` | `{ ok: true }` |
| `POST /lana/circles/features/add` | `{ affiliation_id, label, sub_group? }` | `{ place_id, key, label, written }` |
| `POST /lana/circles/features/remove` | `{ affiliation_id, key }` | `{ ok: true }` |

- `label` is free text as typed ("weight lifting"), or the `label` of a suggested
  row tapped from the menu. The server slugs it into `concept` / `key`, so pass
  the label, never a slug you built.
- **add is idempotent** — a repeat comes back `already_there: true` with no second
  row, so double-taps are safe.
- `features/add` writes the same rows Lana learns in chat, under the existing
  write policy: a place owner's own row is never overwritten by a member's.
- `written: false` from `features/add` means the existing row won (owner-sourced,
  or higher confidence). It is not an error — just refetch.
- **Refetch after each write** (`/mine` or `/profile`); don't patch local state
  from the response. Writes land immediately, not on the drawer's Save button.

### Errors

| HTTP | `detail` | Meaning |
|---|---|---|
| 400 | `place_required` / `label_required` / `concept_required` / `key_required` | nothing usable in the body (e.g. a label that slugs to nothing, like "!!!") |
| 403 | `not_yours` | removing a feature someone else contributed — hide the × on those chips |
| 404 | `not_a_member` / `affiliation_not_found` / `feature_not_found` | |
| 409 | `too_many_activities` | 12 per member per community. Toast and stop. |

---

## 3. Screens

**Community edit drawer** (`community-edit-drawer.tsx`, the "Your gym" sheet) —
add a `YOUR ACTIVITIES · N` block under `WHEN YOU GO`: filled chips for
`mine: true` (tap to remove), then a `+ Add more activities · N` button that opens
a free-text input plus the `mine: false` chips as tap-to-add. `N` is
`activities.filter(a => !a.mine).length`.

**Community profile card** (`WHAT IT HAS`) — this screen isn't built in the PWA
yet; `/lana/circles/profile` has been serving it since the communities work and
now carries `features[].emoji` and `activities`. When it goes in, the "what it
has" chips need an add affordance wired to `features/add`, and an × on chips the
caller contributed (`features/remove`, 403 for anyone else's).

Copy rule unchanged: the UI never says "circle" — it's "your gym", "this
community".

---

## 4. Deploy note

Migration `20261010120000_place_activities.sql` must be pushed before the worker
ships. Until then `/mine` and `/profile` return `activities: []` and the write
endpoints 500 — treat the lists as optional so an unmigrated environment degrades
to today's UI.
