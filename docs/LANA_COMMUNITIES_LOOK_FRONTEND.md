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

**Meet in your communities**, not "your communities": *Find a meet* answers with the
meets actually happening at her places, not a list of places to go dig through. Each
row is the community plus its next couple of meets, and **a meet row opens that meet**
(`event_id` → the meet sheet, same as the profile's `upcoming_events`).

The turn that answers *Find a meet* (`intent_hint: "look_meet"`, the one that replies
"Love it — what kind of thing are you up for?") carries a top-level `communities`
object on `POST /lana/sessions/{id}/messages` (and inside the terminal `result` frame
on `/stream`):

```jsonc
"communities": {
  "items": [
    {
      "affiliation_id": "…",         // the circle_affiliations row (also from /circles/list)
      "place_id": "…",               // what the profile + people endpoints are keyed on
      "place_name": "OrangeTheory Narcoossee",
      "place_address": "9145 Narcoossee Rd",
      "circle_type": "fitness",
      "relation": "gym",             // caller-relative noun — never the word "circle"
      "member_count": 34,
      "meets_this_week": 3,
      "meets": [                     // what's ON there, soonest first — tap = open the meet
        { "event_id": "…", "title": "Saturday run", "starts_at": "2026-08-29T07:00:00",
          "has_time": true, "venue_name": "Laureate Park", "cover_emoji": "🏃" }
      ],
      "upcoming_count": 3,           // everything upcoming; `meets` is the first two
      "active": true,                // 2+ people confirmed there
      "status_line": "34 people · 3 meets this week"
    }
  ],
  "total": 7,
  "more_count": 4                    // → "View 4 more" (Radar › Communities tab)
}
```

Ranked by what's on: places with something upcoming first, then liveliest (people, then
newest). Three items max; `more_count` is what's past them — **places**, not meets, so
*View more* still opens the all-communities list. A place with nothing coming up is
still listed, with `meets: []` — never a meet invented to fill the row. `status_line` is only the two numbers already in the row — render it or
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

No new endpoint: `POST /lana/circles/list` already returns every grounded community.
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
  "create_event_venue": { "name": "OrangeTheory Narcoossee",
                          "address": "9145 Narcoossee Rd",
                          "place_id": "ChIJ…",        // GOOGLE place id, not our place_id
                          "lat": 28.38, "lng": -81.21 },
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

- *Create an event* goes **through chat**, like every other host — hosting stays one
  implementation. Two calls in the tap handler, in this order:

  ```jsonc
  POST /lana/sessions/{session_id}/event-venue     // create_event_venue, verbatim
  { "name": "…", "address": "…", "place_id": "ChIJ…", "lat": 28.38, "lng": -81.21 }
  → { "ok": true }

  POST /lana/sessions/{session_id}/messages        // then the chip's own message
  { "message": "I want to host something at OrangeTheory Narcoossee" }
  ```

  The first call is a plain context stamp (no model call) that pins the venue and marks
  the where-step satisfied; the second is the normal chat turn, so Lana opens the host
  flow already knowing where — she asks only for what's actually missing. Skip the stamp
  and it still works, just worse: the host brain re-geocodes the name and can land on a
  different google place.

  Render this same chip **above "Popular events" too** — one payload serves both spots,
  nothing extra to fetch.

  `create_event_venue.place_id` is the **Google** place id, deliberately not the
  profile's `place_id` (our `places.id`). Publishing re-resolves the pin through Google
  and stamps `events.place_ref` from it; feed it the wrong id and the meet lands on a
  different `place_ref` than the community — and `upcoming_events` filters on
  `place_ref`, so the meet you just created would be missing from the community you
  created it in. Post the block as-is and that can't happen. `create_event_venue` is
  `null` when the place has no Google id on file — just post the message and let the
  host flow ask for the venue.

- *Invite people* is **not** in `actions`, on purpose: minting a labeled link and opening
  the share sheet is native FE work (`POST /lana/invites/mint { circle_key }` → `{token,
  url}`), which a message-posting chip cannot do. Same reason `event_created_actions`
  returns nothing. Render it as your own secondary button.

`actions`, `create_event_venue` and `member_preview` are empty/`null` for an unverified
caller.

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
      "activities": ["Spin", "Pilates"],
      "attributes": ["Runs", "Loves to cook"],
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
**both** hold (exact concept overlap, public claims only); with no overlap they are empty.

`attributes` is what goes under the name, and it is about **them**: their own public
threads (max 3), the ones the caller holds too first. Render them joined or as chips,
your call. Self-subject only — a thread about their child is dropped rather than listed
as theirs. **Empty when nothing true is on file**; render nothing in that case. There is
no filler line any more: the old `shared_line` ("You both go to this gym") was true of
every row and so said nothing.

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
| A member is past the shared-concept fetch cap (200) | Their own threads are listed, the shared ones just don't sort first — never a wrong shared thread |
| A member has no public claims | `attributes: []` — nothing listed, never a filler line |
| Migration 20261004 not applied | `/discover` returns `communities: []` (RPC missing → logged, empty) and `/join` returns `400 join_failed`. Everything else is unaffected |
| Caller has no block **and** no ZIP | `/discover` returns `[]` — there is no honest scope to search |
| Nobody nearby has a community yet | `[]` with `radius_meters` set, so the empty state can say "nothing within ~5 miles yet" |

## Language

Cards are English on the wire, like every other card surface (`final-mile-localization`:
the AI-rendered reply text is localized, cards and progress are not yet). `status_line` is
the only composed string left here — if you'd rather localize it, build it from the
numeric fields instead. `attributes` are stored concept labels (English-canonical in the
DB), not composed copy.

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

---

# Second drop — the join question, the caller's own row, feature ownership

Answers backend asks §15, §17, §18, §19, §20 (issues #77, #81, #83, #84, #86). All
additive; nothing already shipped changes shape.

## "I'm a member" vs "just curious" (§19)

`POST /lana/circles/join` takes `membership: 'member' | 'curious'` (default `'member'`, so
today's callers are unchanged), and the sheet can also answer after the tap:

```
POST /lana/circles/membership { affiliation_id, membership }
  → { affiliation_id, place_id, membership }
  400 place_required (an ungrounded candidate is not a community) · 404 affiliation_not_found
```

`curious` is deliberately **not** membership: the row is stored as
`circle_affiliations.status = 'curious'`, which every member count, roster, peer profile and
matcher already excludes by filtering `status = 'confirmed'`. So she does not move
`member_count`, does not appear in `member_preview` / `members`, and never becomes a match
candidate — while the place stays in her own `/lana/circles/list` list.

`POST /lana/circles/profile` opens for a curious joiner and carries `membership: 'curious'`:
the head, the count, the features and the activities, with `member_preview: []`,
`actions: []` and `create_event_venue: null` — the names belong to the people who go there.
`/lana/circles/members` still 404s `not_a_member` for her. Either answer can be changed
later through the same endpoint; tapping Join again as a member promotes the same row.

## The caller is in her own community's roster (§17)

`members[]` and `member_preview[]` now include the caller, flagged `me: true` (no
`attributes`, no trait tags, no Nudge — she is not described back to herself). Rows rendered
now equal `member_count`, so the client-side splice can go. Everyone else's rows are
unchanged, and a failed block read still hides the whole list rather than leaving one row.

## Features say who added them (§15)

Each `features[]` row carries `mine: boolean` — true exactly when
`/lana/circles/features/remove` will succeed for the caller. Render the × on those and only
those; the session-state workaround can go.

## Supabase RPCs

- `get_peer_profile` claim rows gain **`bucket`** and **`created_at`** (§14a), so the
  neighbour's timeline groups by category and restores the recency rail.
- `get_peer_profile.communities[]` is **GONE** (migration `20261101120000`, 2026-08-18).
  Two surfaces were answering "what communities is this user in?" with different rules —
  the RPC hid a place's name/id unless matched-or-shared, while `POST /lana/circles/list
  {user_id}` names every place to anyone. The worker endpoint owns it alone now. Its rows
  carry `shared` (the viewer is at that place too), `activities[].theirs` (what THIS person
  does there — the old `sub_groups`), and shared-places-first ordering. `detail` is not
  sent at all: a member's own words for her place stay hers. Every named row opens, since
  `/lana/circles/profile` serves visitors.
- **`set_my_nickname(p_nickname) → { nickname }`** (§20), the twin of `set_my_handle`.
  The rule is **1–30 characters after trimming** — confirmed, not guessed: the extraction
  path has always truncated at 30. Over or empty raises a matchable `nickname_invalid`
  instead of truncating. Renames are **not** rate limited (neither is the handle); if a
  cooldown ever lands it will raise `nickname_rename_too_soon:<seconds>` so the UI can say
  when. No uniqueness — `nickname` is the real first name Lana speaks, `handle` is the
  unique public one.

Migrations: `20261017120000_membership_intent.sql`, `20261018120000_peer_profile_claim_fields.sql`,
`20261019120000_set_my_nickname.sql`, `20261101120000_peer_profile_drop_communities.sql`. The three RPC/DB items above need the push; the
worker fields need the worker deploy.
