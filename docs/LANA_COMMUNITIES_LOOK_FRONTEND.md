# Communities — backend answers (C-CIRCLE-LOOK-COMMS → …-COMM-PEOPLE, + discover & join)

Backend for the four walkthrough panels — the **Your communities** card on the look
screen, the **all communities** list behind *View more*, a **community profile**, its
**people** list with Nudge — plus the two the product added after: **discovering
communities you're not in yet**, and **joining** one.

Everything here is **additive**. A frontend that ignores this document keeps working
exactly as it does today: the new turn field is absent (not empty) on every turn that has
nothing to say, and every endpoint below is a new path.

**One migration**, for discover + join only:
`supabase/migrations/20261004120000_community_discovery_join.sql` — **not pushed yet**.
The four original panels need no DB change and work without it; `/lana/circles/discover`
returns an empty list and `/lana/circles/join` fails until it is applied.

---

## Panel 1 — the look screen card (`C-CIRCLE-LOOK-COMMS`)

The turn that answers *Find a meet* (`intent_hint: "look_meet"`, the one that replies
"Love it — what kind of thing are you up for?") now carries a new top-level
`communities` object on `POST /lana/sessions/{id}/messages` (and inside the terminal
`result` frame on `/stream`):

```jsonc
"communities": {
  "items": [
    {
      "affiliation_id": "…",         // the circle_affiliations row (also from /circles/mine)
      "place_id": "…",               // what the profile + people endpoints are keyed on
      "place_name": "OrangeTheory Narcoossee",
      "place_address": "9145 Narcoossee Rd",
      "circle_type": "fitness",
      "relation": "gym",             // caller-relative noun — never the word "circle"
      "member_count": 34,
      "meets_this_week": 3,
      "active": true,                // 2+ people confirmed there
      "status_line": "34 people · 3 meets this week"
    }
  ],
  "total": 7,
  "more_count": 4                    // → "View 4 more" (Radar › Communities tab)
}
```

Ranked liveliest-first (people, then newest). Three items max; `more_count` is what's
past them. `status_line` is only the two numbers already in the row — render it or
compose your own, but a place with nobody else there reads **"just you so far"**, never
a dressed-up count.

**When it is absent** (the field is `null`, so render nothing):

- the user has no community yet — grounded, confirmed rows only, per the place-mandatory
  rule in `LANA_CIRCLES_BACKEND.md`;
- any turn that isn't the looking-open ask. It is a per-turn card
  (`app/turn_surfaces.py`), so it never re-renders under a later reply.

`CreateSessionResponse` carries the same field for symmetry; in practice the opening
turn is not a looking-open turn, so it is `null` there.

## Panel 2 — all communities (*View more*)

No new endpoint: `POST /lana/circles/mine` already returns every grounded community.
Two fields were **added** to each row for these panels:

| Field | Why |
|---|---|
| `place_id` | Open the profile / people panels without a second lookup |
| `circle_key` | The invite label for `POST /lana/invites/mint {circle_key}` |
| `source`, `confirmed_via`, `joined_via_label` | How this community became theirs — see *Provenance* below |

## Panel 2b — communities you're **not** in yet (`C-CIRCLE-COMM-DISCOVER`)

```jsonc
POST /lana/circles/discover
{ "query": "orange", "limit": 20 }     // query optional — everything nearby without it
```

```jsonc
{
  "communities": [
    { "place_id": "…", "place_name": "OrangeTheory Narcoossee",
      "place_address": "9145 Narcoossee Rd", "place_type": "fitness",
      "relation": "gym", "zip": "32827",
      "member_count": 34,
      "distance_text": "11 min walk",
      "is_member": false,
      "status_line": "34 people · 11 min walk" }
  ],
  "radius_meters": 8000
}
```

Places with **at least one confirmed member**, within ~5 miles of the caller's coarse
point (block centroid, else ZIP centroid; ZIP equality when they have neither), ordered
liveliest-then-nearest. `radius_meters` is returned so an empty list can be explained
rather than looking broken.

**No member identities, on purpose.** A row is a place, a count and a distance — never who
is there. The people panel stays members-only, so *joining is what earns the names*. A
place whose only members have blocked the caller (either direction) is not returned at
all: the count is computed over visible members and zero-count places are dropped.

`is_member: true` marks the caller's own places instead of hiding them — and note
`member_count` **includes** the caller there, which is why `status_line` reads "You + 11
others" rather than "12 people".

`distance_text` is `null` whenever either point is unknown. Render nothing then.

## Joining

```jsonc
POST /lana/circles/join
{ "place_id": "…", "circle_type": "fitness" }   // circle_type optional
```

```jsonc
{ "affiliation_id": "…", "place_id": "…", "place_name": "OrangeTheory Narcoossee",
  "status": "confirmed", "already_member": false,
  "source": "chat_extraction", "confirmed_via": "community_join",
  "joined_via_label": "From something you told Lana",
  "promoted_from_candidate": true }
```

**Joining is a self-claim, not a request.** These are real-world places, so "I go here too"
is a statement about yourself: it takes effect immediately, there is no approval step and
nobody is notified. Leaving is the existing `POST /lana/circles/remove`, which drops it
from matching at once. Re-joining a place they're already in is a no-op with
`already_member: true` (safe to call optimistically).

## Provenance — "did they join it in Lana, or did we add it after they said something?"

Two columns, because one cannot answer both halves:

| Field | Question it answers | Values |
|---|---|---|
| `source` | Where the community **first came from**. Never rewritten. | `chat_extraction` · `profile_add` · `invite_confirmed` · `community_join` |
| `confirmed_via` | Which action **made it real** (confirmed + grounded) | `grounding_ask` · `profile_add` · `invite_self_confirm` · `community_join` |

The pair is what distinguishes the case you asked about:

| What happened | `source` | `confirmed_via` | `joined_via_label` |
|---|---|---|---|
| Found it in Lana's discovery panel and joined | `community_join` | `community_join` | "Joined in Lana" |
| Mentioned a gym in chat → Lana asked which spot → they answered | `chat_extraction` | `grounding_ask` | "From something you told Lana" |
| Mentioned it in chat, then later tapped **Join** on the discovery panel | `chat_extraction` | `community_join` | "From something you told Lana" (origin wins; `promoted_from_candidate: true` on the join response) |
| Added it themselves in the Communities panel | `profile_add` | `profile_add` | "You added it" |
| Came in through an invite link | `invite_confirmed` | `invite_self_confirm` | "From an invite" |

`joined_via_label` is a ready-made phrase keyed on `confirmed_via`, falling back to
`source` for rows written before the column existed. Both raw fields are on the wire too,
so you can render your own copy. Analytics: `circle_joined` carries `source`,
`confirmed_via` and `promoted_from_candidate`.

## Panel 3 — a community profile (`C-CIRCLE-COMM-PROFILE`)

```jsonc
POST /lana/circles/profile
{ "place_id": "…" }            // or { "affiliation_id": "…" } — either works
```

```jsonc
{
  "place_id": "…",
  "affiliation_id": "…",
  "place_name": "OrangeTheory Narcoossee",
  "place_address": "9145 Narcoossee Rd",
  "circle_type": "fitness",
  "circle_key": "orangetheory_narcoossee",   // → /lana/invites/mint
  "relation": "gym",
  "detail": "morning classes",               // the caller's own note, if any
  "member_count": 34,
  "active": true,
  "status_line": "34 people · 3 meets this week",
  "description": "A friendly gym in Lake Nona — pool, childcare.",
  "features": [{ "key": "has_pool", "label": "Pool", "sub_group": null }],
  "member_preview": [{ "peer_user_id": "…", "nickname": "rosegold22", "avatar_url": "…" }],
  "upcoming_events": [
    { "event_id": "…", "title": "Saturday run", "starts_at": "…",
      "has_time": true, "venue_name": null, "going_count": 2 }
  ],
  "actions": [{ "id": "community_create_event", "label": "Create an event",
                "message": "I want to host something at OrangeTheory Narcoossee",
                "style": "primary" }]
}
```

**Members-only, by construction.** The caller must hold a confirmed, non-dismissed
affiliation at that place or the call is `404 not_a_member`. A place is named to the
people who go there — this is not a directory of the neighborhood (§F).

**`features` is what members said, never what we assume.** Rows come from
`place_features` (volunteered in conversation, ≥0.5 confidence). `label` is humanized
from the key (`has_pool` → "Pool"); the emoji in the mock is yours to add. An empty list
means nobody has mentioned anything yet — not that the place has no pool.

**`description`** is AI-authored from exactly those facts plus the area and the real
member count, and is `null` when there is nothing true to say. It never rates the place
and never says people are waiting for anyone.

**"Popular events" is ordered on a real going roster** (`going_count`, the same
approved+going predicate the cancel fan-out uses), upcoming only, max 5. `has_time:
false` = render the date without a clock time (#56).

**`member_preview`** is the avatar row (up to 5 faces, mutual blocks filtered). The full
list is panel 4.

**The two CTAs, deliberately split:**

- *Create an event* is in `actions` — post its `message` like any chat CTA, and the host
  flow opens with the venue already known.
- *Invite people* is **not** in `actions`, on purpose: minting a labeled link and opening
  the share sheet is native FE work (`POST /lana/invites/mint { circle_key }` → `{token,
  url}`), which a message-posting chip cannot do. Same reason `event_created_actions`
  returns nothing. Render it as your own secondary button.

`actions` and `member_preview` are empty for an unverified caller.

## Panel 4 — the people (`C-CIRCLE-COMM-PEOPLE`)

```jsonc
POST /lana/circles/members
{ "place_id": "…", "limit": 20, "offset": 0 }     // limit ≤ 50
```

```jsonc
{
  "place_id": "…",
  "place_name": "OrangeTheory Narcoossee",
  "member_count": 34,
  "members": [
    { "peer_user_id": "…", "nickname": "mapleluz", "avatar_url": "…",
      "trait_tags": ["Gardens", "Runs"],
      "shared_line": "You both: Gardens · Runs",
      "actions": [{ "id": "peer_card_nudge", "label": "Nudge",
                    "message": "introduce me to mapleluz", "style": "primary",
                    "peer_user_id": "…" }] }
  ],
  "has_more": true,
  "requires_phone_verification": false
}
```

**What a row claims, and what it never claims.** No `match_stars`, `match_band`,
`match_badge` or `similarity_score` — nothing here compared two people, so there is
nothing to score. `trait_tags` are the identity concepts the caller and that neighbour
**both** hold (exact concept overlap, public claims only). With no overlap the tags are
empty and `shared_line` states the one thing that IS true of every row on this panel:
**"You both go to this gym"**. Render `shared_line` as the line under the name; the tags
are the same facts as chips.

Self and mutual blocks are excluded from `members` (`member_count` is the raw roster, so
it can exceed what you can page through). Paginate with `offset` while `has_more`.

`Nudge` posts `message` back to Lana, exactly like the peer cards — the intro flow is
unchanged.

**Unverified caller:** `members: []`, `requires_phone_verification: true`, and
`member_count` still real. The count is honest; the names are for verified accounts, same
rule as the peer cards.

## Errors

| Status / detail | Meaning |
|---|---|
| `404 not_a_member` | The caller has no confirmed affiliation at that place |
| `404 affiliation_not_found` | `affiliation_id` isn't the caller's (or isn't grounded) |
| `404 place_not_found` | The `places` row is gone (or `join` was given an unknown `place_id`) |
| `400 place_required` | Neither `place_id` nor `affiliation_id` was sent |
| `400 join_failed` | The write itself failed — safe to retry |

## Degrade behaviour

| State | What arrives |
|---|---|
| No LLM configured | `description` falls back to a factual template ("A gym in Lake Nona — pool, childcare.") or `null` |
| No features recorded | `features: []`, and the description never invents one |
| No meets at the place | `upcoming_events: []`, `meets_this_week: 0` |
| Nobody else there yet | `active: false`, `status_line: "just you so far"`, `members: []` |
| Blocks table unreadable | The people panel returns no names (fails closed, on purpose) |
| A member is past the shared-concept fetch cap (200) | Their row reads "You both go to this …" — never a wrong shared thread |
| Migration 20261004 not applied | `/discover` returns `communities: []` (RPC missing → logged, empty) and `/join` returns `400 join_failed`. Everything else is unaffected |
| Caller has no block **and** no ZIP | `/discover` returns `[]` — there is no honest scope to search |
| Nobody nearby has a community yet | `[]` with `radius_meters` set, so the empty state can say "nothing within ~5 miles yet" |

## Language

Cards are English on the wire, like every other card surface (`final-mile-localization`:
the AI-rendered reply text is localized, cards and progress are not yet). `status_line`
and `shared_line` are the two composed strings here — if you'd rather localize, build
them from the numeric fields instead.

## Where this lives

- `app/community_surface.py` — the card, the profile, the people, the blurb
- `app/community_discovery.py` — discover, join, and the provenance model
- `supabase/migrations/20261004120000_community_discovery_join.sql` — `discover_communities_near`, `confirmed_via`, `source` += `community_join`
- `app/circles_flow.py` — `list_my_circles` (now with `place_id`, `circle_key`, provenance); `ground_affiliation` stamps `confirmed_via`
- `app/activity_browse.py` — stamps the card on the P1 interest ask
- `app/turn_surfaces.py` — `communities_card` is per-turn
- `app/main.py` — `/lana/circles/profile`, `/members`, `/discover`, `/join`, and `communities` on the turn
- `tests/test_community_surface.py`, `tests/test_community_discovery.py`, `tests/test_activity_browse.py`

## In chat — "show me communities around me"

That ask now has its own lane (`discovery.communities`). Before, with no handler, the
classifier sent it to whichever arm looked closest: one probe got the area-not-open host
bridge ("there aren't any local communities to show yet" — asserted without counting
anything), another got an **attribute peer search** for neighbours "interested in
community". The asking account had two communities in its own ZIP both times.

The turn answers from real rows and carries two payloads:

- `communities` — the ones they're already in (same shape as the look-screen card)
- `community_discovery` — nearby ones they could join, already filtered to
  `is_member: false`, max 5. Same `CommunityDiscoveryRow` shape as `/discover`, and
  `radius_meters` is `0` here (the endpoint is the place to read it from)

Both are **turn-scoped** — they appear on the turn that answered the ask and nowhere else.
Unverified callers get a one-line "verify and I can show you" instead: member counts are
neighbours' data, same gate as find-peers.

### Joining from chat

The turn also carries `ui_actions` with one **Join** chip per listed community (max 3):

```jsonc
{ "id": "community_join_0", "label": "🏋️ Join Lp Fit",
  "message": "Join Lp Fit", "style": "primary" }
```

Two ways to join, pick either:

- **Native** — call `POST /lana/circles/join {place_id}` from your own button on the card.
  No chat round-trip, instant.
- **Chat chip** — post the `message` verbatim like any other CTA. The next turn reads it,
  joins, and confirms with the real member count. Localize the **label** only; `message`
  is the canonical payload the reader matches when the LLM is unavailable.

The offer lasts exactly one turn. Typed answers work too ("join lp fit", or just "Lp Fit").
A bare "yes" joins only when **one** community was listed — with three on screen it doesn't
say which, and guessing would write the wrong membership, so Lana falls through instead.
Double-joining is a no-op (`already_member: true`), so taps are safe to repeat.

### Emoji

Every community row on every surface (look card, discovery, profile, join chip) carries an
`emoji` for its **type**: 🏋️ fitness · ⛪ faith · 🎒 school · 🧸 kids · 🏡 neighborhood ·
🎨 hobby · 🤝 support · 🌍 heritage · ☕ friends · 📍 other.

Deterministic per type — the same community never shows a different icon on a different
screen, and there's no LLM call. It's a category icon, not a claim about the place, and an
unknown type gets the neutral pin rather than a guess. Advisory: swap in your own icon set
if you'd rather, the field is safe to ignore.

## Address-shaped places are never offered

The chat extractor parks whatever the user said and grounding pins whatever Google
returned, so dev already holds `places` rows like `10057 Selten Way #328`, `373 Tampa Ct`
and one literally named `32827`. Discovery filters those out by name shape — a bare street
address or a ZIP is not a community anyone should be invited to join. Their owner's own
row keeps working everywhere else, and real names with numbers ("FIT 407 Lake Nona")
survive the filter.

## Still open (not in this drop)

- **Typed "join X"** — the chat lane lists and offers; joining is the card tap /
  endpoint.
- **Radius is a fixed ~5 mi** (`LANA_COMMUNITY_DISCOVERY_RADIUS_METERS` to override). No
  widen offer on an empty result yet.
- **Junk `places` cleanup** — the filter hides address-shaped rows from discovery but
  does not remove them; the ones already grounded stay in the table.
