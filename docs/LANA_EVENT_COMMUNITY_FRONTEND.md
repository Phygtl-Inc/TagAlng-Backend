# Host setup · community card

C-4-EVENT-P2B v0.2.35 adds a **COMMUNITY** card to the quick-setup carousel:
*"For one of your communities?"* — optional, defaults to **None**.

It sits **before WHERE**, not where the mockup numbers it. Community and venue are
different things — a school's picnic can be at a park — but most community meets happen
at the community's own spot, so picking one pre-fills the venue (see below) and the
where-step is already answered. Asking it after meant searching Google for a place the
host was about to name anyway.

Backend and PWA are both done. This is the contract, for anyone touching either side.

## The card

`event_draft.event_setup` (already the carousel's config source) carries:

```jsonc
{
  "community_label": "For one of your communities?",   // AI-tailored, like the other labels
  "community_hint": "Optional — skip if it's just your own.",
  "communities": [                                      // the host's OWN communities
    {
      "place_id": "uuid",                               // OURS — submitted as circle_place_id
      "name": "Lake Nona YMCA",
      "emoji": "🏋️",
      "relation": "gym",
      "address": "9055 Northlake Pkwy…",                // the same place as a VENUE
      "google_place_id": "ChIJ…",                       // GOOGLE's — the venue field's place_id
      "lat": 28.3772,
      "lng": -81.2519
    }
  ]
}
```

Picking a community fills an **empty** venue from those last four fields. It never
overwrites a venue the host already chose, and the WHERE card stays editable — that is
what keeps "for my kid's school, at the park" sayable.

- `communities` is **data, not suggestions** — real confirmed, grounded rows.
- `communities: []` → the card is skipped entirely (a picker whose only option is "None"
  is a dead card), so the carousel is 4 cards, not 5.
- Rendered with the existing single-select (chips), not a dropdown: a host has a handful of
  communities at most, so one tap beats two. `relation` is the chip's subtitle.
- `event_draft.circle_place_id` pre-selects a chip — see the community screen below.

## Submitting

`POST /lana/sessions/{id}/event-setup` takes one extra field:

```jsonc
{ "circle_place_id": "uuid" }   // null = "None"
```

The carousel is a full submission, so omitting it clears any earlier pick — same contract
as `max_attendees: null` meaning "no limit".

## What happens on publish

The meet is tagged with that community (`events.circle_place_ref`) and **every other
confirmed member of it is emailed** ("New meet at Lake Nona YMCA"), in their own language,
capped at 200 recipients. Nothing is sent for "None". Membership is re-verified
server-side, so a tampered `circle_place_id` tags and mails nothing.

Not built (say the word if product wants it): push alongside the email, and visibility
scoping — a community meet is still discoverable by everyone.

## Hosting from a community's screen

The community profile's `create_event_venue` block now carries `circle_place_id` too. POST
it verbatim to `/lana/sessions/{id}/event-venue` (that's `setEventVenue(sessionId, place,
circlePlaceId)`) and the meet is recorded as being **for** that community, not merely held
at its address — the setup card arrives pre-selected and members get the publish email.
Before this, that button only pre-filled the venue.

`community_profile.upcoming_events` now lists meets **held at** the place OR **created
for** it, so a school's picnic in the park shows on the school's screen.

## Reading it back

`events.circle_place_ref` is the community's canonical place id — the same id
`/lana/circles/profile` is keyed on, so a chip on the event card can link straight there.
It is **not** `events.place_ref`, which is where the meet is held.

Migration: `20261012120000_event_community.sql`.
