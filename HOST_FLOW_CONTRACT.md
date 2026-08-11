# Host Flow — Backend ⇄ Frontend Contract

The response payloads, `ui_intent` values, and endpoint shapes for the in-chat host
(create-a-meet) flow in the unified Lana concierge (`/chat`). This is the data contract
only — not the internal logic.

- **Backend:** `services/lana-worker` (FastAPI). Response model: `SendMessageResponse`.
- **Frontend:** `tagalng-pwa-main` (`src/features/voice/`). Reads `turn.ui_intent` +
  `turn.event_draft`.

---

## 1. Turn response — `POST /lana/sessions/{session_id}/messages`

Request:

```jsonc
{ "message": "a brazilian coffee saturday morning at foxtail", "intent_hint": "host_event" }
```
`intent_hint` is optional (`"host_event"` from the CTA); a natural-language host phrase also
enters the flow.

Response (`SendMessageResponse`) — host-relevant fields:

```jsonc
{
  "session_id": "…",
  "status": "continue" | "ready_to_complete",
  "assistant_message": "Here's your meet — …",   // Lana's bubble text for the turn
  "ui_intent": "event_review",                    // ← drives which host card the FE renders
  "event_draft": { … },                           // the meet draft so far (see §3)
  "event_id": "uuid" | null,                      // set only once published
  "routing_phase": "preview" | "await_signup_phone" | …,
  "requires_phone_verification": false,
  "phone_verified": true,
  "ui": { "bucket": null, "focus_phrase": null, "highlights": [] },
  "ui_actions": []
}
```

Only `ui_intent`, `event_draft`, and `event_id` matter for host rendering. Everything else
in `SendMessageResponse` (peer_matches, signal_saved, auth_action, …) is unused during
hosting.

---

## 2. `ui_intent` values (host) — what the FE renders

The FE (`thought-chat.tsx`) switches on `turn.ui_intent`:

| `ui_intent`            | FE renders                          | Component                     |
| ---------------------- | ----------------------------------- | ----------------------------- |
| `collect_event_detail` | plain bubble + draft receipt/chips  | `EventDraftCard` / `EventDraftActions` |
| `event_review`         | "Drafted by Lana" review card       | `EventReviewCard` (stage `review`) |
| `event_setup`          | scrollable quick-setup carousel     | `HostSetupCarousel`           |
| `event_confirm`        | "It's all set" final review card    | `EventReviewCard` (stage `confirm`) |
| `event_created`        | "You're live" celebration + share   | `EventCelebrationCard`        |

`event_review` / `event_setup` / `event_confirm` are backed by a server-side `host_stage`
(`review` → `setup` → `confirm`); `event_created` fires on publish. The card **replaces**
Lana's text bubble for that turn.

---

## 3. `event_draft` object (full shape)

Sent on every host turn; serialized from the `EventDraft` model.

```jsonc
{
  "title": "Brazilian moms coffee",
  "description": "Saturday morning coffee — open to first-time Brazilian moms.",
  "venue_name": "Foxtail Coffee",
  "venue_address": "…",              // exact place (if pinned via Google)
  "place_id": "ChIJ…",               // Google Places id (if pinned)
  "venue_lat": 28.36,
  "venue_lng": -81.25,
  "starts_at": "2026-07-11T09:00:00", // ISO (naive local; TZ-anchored at publish)
  "ends_at":   "2026-07-11T10:30:00",
  "duration_minutes": 90,
  "recurrence": "weekly",             // "weekly"|"biweekly"|"monthly"|null (null = one-off)
  "recurrence_until": "2026-08-31",   // null = 180 days from publish
  "max_attendees": 8,                 // null = no limit
  "auto_approve": false,              // false = host approves each join; true = anyone joins
  "allow_attendee_share": true,       // attendees can pass the invite link
  "bring_items": ["Stroller", "Coffee mug"],
  "event_setup": { … },               // AI-tailored setup-card copy (see §4)
  "cohort_tags": ["brazilian", "coffee_meet"],
  "affinity_prompt": null,
  "affinity_options": [],
  "suggestions": [],                  // legacy quick-reply chips (unused in batched flow)
  "missing": []
}
```

FE usage by field:
- `title` / `description` / `cohort_tags` / `starts_at` / `venue_name` → review + celebration cards.
- `recurrence` → the "Every Friday" badge on review/confirm/celebration. `starts_at` is the
  FIRST occurrence; captured from the host's own words ("every Friday at 6"), no extra step.
- `max_attendees` / `auto_approve` / `allow_attendee_share` / `bring_items` → carousel initial state + confirm/celebration pills.
- `event_setup` → carousel card copy/options (§4).
- `place_id` / `venue_lat` / `venue_lng` / `venue_address` → precise "open in maps" pin.

---

## 4. `event_setup` object (AI-tailored carousel config)

Set once when the flow enters the `setup` stage; the FE renders the carousel cards from it.

```jsonc
{
  "capacity_label": "How many moms?",     // audience noun tailored to the event
  "capacity_default": 8,
  "sharing_label": "Can attendees pass the link on?",
  "sharing_hint": "She can hand it to a friend who fits.",
  "approval_label": "Want to approve each joiner?",
  "approval_hint": "I'll text you each request · one tap.",
  "bring_label": "Anything to bring?",
  "bring_hint": "I'll add it to the pinned list in chat.",
  "bring_suggestions": ["Stroller", "Coffee mug"]  // pre-filled bring chips
}
```

All keys have deterministic fallbacks if the LLM is unavailable.

---

## 5. Messages the FE sends back (CTA → next turn)

The card buttons post plain messages via the same `/messages` endpoint:

| Card / button           | Message sent          | Advances to        |
| ----------------------- | --------------------- | ------------------ |
| Review · "Looks good"   | `"Looks good"`        | `event_setup`      |
| Review · "Let me tweak" | `"Let me tweak"`      | stays `event_review` |
| Carousel · submit       | `"Looks good"` (after `POST /event-setup`) | `event_confirm` |
| Confirm · "Drop the meet up" | `"Drop the meet up"` | `event_created` |

Matched loosely on the backend (substring), so button-label variants still land.

---

## 6. Side endpoints (stamp state, then a message advances)

### `POST /lana/sessions/{session_id}/event-setup`
Carousel submit — stamps the whole setup (+ any blockers the opening message lacked) onto
the draft in one shot. Then the FE sends `"Looks good"`.

Request:
```jsonc
{
  "title": "Coffee Club" | null,          // blockers, only when collected in the carousel
  "starts_at": "2026-07-11T09:00:00" | null,
  "venue_name": "Foxtail" | null,
  "venue_address": "…" | null,
  "venue_lat": 28.36 | null,
  "venue_lng": -81.25 | null,
  "place_id": "ChIJ…" | null,
  "max_attendees": 8 | null,               // null = no limit
  "auto_approve": false | null,
  "allow_attendee_share": true | null,
  "bring_items": ["Stroller", "Coffee mug"]
}
```
Response: `{ "ok": true }`

### `POST /lana/sessions/{session_id}/event-venue`
"Let me tweak" / where-step place pick — stamps the exact picked place.

Request:
```jsonc
{ "name": "Foxtail Coffee", "address": "…", "lat": 28.36, "lng": -81.25, "place_id": "ChIJ…" }
```
Response: `{ "ok": true }`

### `POST /lana/places/search`
Powers the in-carousel / where-step place search (Google Places, biased to the block).

Request: `{ "q": "foxtail" }`
Response:
```jsonc
{ "results": [ { "name": "Foxtail Coffee", "address": "…", "place_id": "ChIJ…", "lat": 28.36, "lng": -81.25 } ] }
```

---

## 7. Per-case: what the backend sends

| Case (turn)                                   | `ui_intent`            | Key payload                                                            |
| --------------------------------------------- | ---------------------- | --------------------------------------------------------------------- |
| Sparse open (`"host a meet"`)                 | `event_setup`          | `event_draft.event_setup` set; carousel also shows Title/When/Where cards (blockers empty) |
| Rich open (`"…coffee saturday at foxtail"`)   | `event_review`         | `event_draft` with `title`, `description`, `cohort_tags`, `starts_at`, `venue_name` |
| Review → `"Looks good"`                       | `event_setup`          | `event_draft.event_setup` + seeded `max_attendees` / `bring_items`    |
| Review → `"Let me tweak"` / free-text edit    | `event_review`         | updated `event_draft`                                                 |
| Carousel submit → `"Looks good"`              | `event_confirm`        | `event_draft` fully assembled                                         |
| Confirm → `"Drop the meet up"` (published)    | `event_created`        | `event_id` set; `event_draft` final                                   |
| Confirm → drop, guest not verified            | `collect_email`        | `routing_phase: "await_signup_phone"`, `requires_phone_verification: true` |

---

## 7b. Recurring meets — one row that rolls forward

A recurring meet is **one** `events` row whose `starts_at` is the NEXT occurrence, not N
rows. So: one group chat, one standing RSVP per person (approved once = in every week),
one card in browse. After an occurrence passes, `roll_recurring_events()` advances the row
— read-repair on every `/meet` open and every chat browse, no cron.

Two host actions, and they are different:

| Host wants | Call | Effect |
| ---------- | ---- | ------ |
| End the whole thing | `cancel_event` RPC + `POST /hooks/event-cancel` (unchanged) | series over, roster notified |
| Skip just this one | `POST /hooks/event-skip` `{ "event_id": "…" }` | that date is called off, `starts_at` moves to the next one, roster notified with the new date |

`/hooks/event-skip` does the RPC **and** the fan-out, so it's one call (unlike cancel):
```jsonc
{ "ok": true, "next_starts_at": "2026-08-21T22:00:00+00:00", "notified": 6 }
```
`next_starts_at: null` means that was the last occurrence, so the series is now closed.
Errors come back as 400s the FE can render: `not_event_host`, `not_recurring`,
`event_not_open`, `event_not_found`, `occurrence_conflict`.

Show "Skip this one" only when `recurrence` is non-null. Attendees have no skip — a
standing RSVP means "can't make it this week" is just a message in the group chat.

## 8. Published meet — `/meet/{id}` (`get_event_preview[_authed]` RPC)

After publish, the shared meet page reads the event via the preview RPCs (not the chat
turn). Host-set fields surfaced there:

```jsonc
{
  "title": "…", "description": "…", "starts_at": "…", "ends_at": "…",
  "venue_name": "…", "venue_address": "…", "place_id": "…",
  "cohort_tags": ["…"], "max_attendees": 8,
  "recurrence": "weekly" | null,               // "Every Friday" badge; starts_at is the next one
  "recurrence_until": "2026-08-31" | null,
  "bring_items": ["Stroller", "Coffee mug"],   // rendered as the pinned "bring" list
  "participant_count": 0, "participants": [ … ], "distance_text": "…"
}
```
