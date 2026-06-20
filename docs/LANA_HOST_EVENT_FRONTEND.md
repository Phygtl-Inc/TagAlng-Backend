# Lana — Host-a-Meet (in-chat) · Frontend Integration

_Last updated: 2026-06-19_

This covers the **host-a-meet** flow that now runs **inside the unified Lana chat**

Everything below comes back from the normal turn endpoint:

```
POST /lana/sessions/{session_id}/messages   →  body: { message, intent_hint? }
```

The response is the same shape for every Lana flow — discovery, intros, auth,
hosting. On a host turn most non-host fields are just empty.

---

## 1. What changed

- **Hosting happens in the main chat.** When the user taps **"A meet to host"**, send
  the message with an intent hint so the backend enters host mode:
  ```ts
  sendMessage(sessionId, "I want to host a meet", "host_event")
  // → body: { message: "I want to host a meet", intent_hint: "host_event" }
  ```
- The backend then drives a short capture conversation (title → when → place →
  "who's it for?") and **auto-publishes** the event when it has enough.
- **All tappable options come from the backend** in `event_draft`. The FE never
  invents chips — it renders what's in the response.
- **Two new `ui_intent` values** were added for this flow: `collect_event_detail`
  and `event_created` (see table below).
- **Response was slimmed.** These fields are **no longer sent** (they were unused by
  the client): `debug`, `timing_ms`, `message_count`, `onboarding_step`. If you were
  reading any of these, stop — they're gone from the payload.

---

## 2. `ui_intent` — the render switch

`response.ui_intent` (top-level string) tells you what to render this turn. Full list:

| `ui_intent`              | Render                                                       | Key payload field        |
|--------------------------|--------------------------------------------------------------|--------------------------|
| `chat`                   | Plain message bubble                                         | `assistant_message`      |
| `collect_zip`            | ZIP input                                                    | —                        |
| `collect_identity`       | Identity prompt                                              | —                        |
| `collect_display_name`   | Display-name input                                           | —                        |
| `collect_phone`          | Phone input (auth gate)                                      | `auth_action`            |
| `collect_otp`            | OTP input                                                    | `auth_action`            |
| `collect_signal_detail`  | Signal capture follow-up                                     | —                        |
| **`collect_event_detail`** | **Event card (in progress) + tappable option chips**       | **`event_draft`**        |
| **`event_created`**      | **Published "it's live" card + CTA buttons**                 | **`event_draft`, `ui_actions`** |
| **`collect_item_detail`** | **Pass-along item card + entity chips + suggestions/photo** | **`item_draft`**         |
| **`item_listed`**        | **"Listed on your block" card**                             | **`item_draft`**         |
| `show_peer_preview`      | Peer match list                                              | `peer_matches`           |
| `show_activity_preview`  | Activity cards                                               | `activity_previews`      |
| `show_identity_profile`  | Identity profile card                                        | `identity_profile`       |
| `show_block_log`         | Block log list                                               | `block_log_entries`      |
| `signal_saved`           | "Saved / listening" receipt                                  | `signal_saved`           |
| `offer_neighbor_intro`   | Intro offer (single peer)                                    | `peer_matches`           |
| `propose_neighbor_intro` | Intro proposal                                               | `intro_proposal`         |
| `show_pending_intros`    | Pending intro list                                           | `pending_intros`         |
| `respond_pending_intro`  | Respond to an intro                                          | `pending_intros`         |
| `confirm_profile`        | Profile confirm                                              | `identity_profile`       |
| `upload_profile_photo`   | Photo upload                                                 | —                        |
| `sign_out`               | Sign-out confirm                                             | —                        |

> **Bold rows** are the host-a-meet flow.

---

## 3. `event_draft` — card contents **and** the options

On `collect_event_detail` and `event_created`, read `response.event_draft`:

```jsonc
"event_draft": {
  "title": "Mom gettogether",            // card header (null until captured)
  "venue_name": null,                    // place
  "starts_at": null,                     // ISO 8601 once resolved
  "cohort_tags": ["lifestyle_social"],   // shown as meta chips
  "max_attendees": null,
  "description": null,

  // ── TAPPABLE OPTIONS (this is what the FE renders as chips) ──
  "suggestions": ["Sat Jun 20", "Sun Jun 21", "Sat Jun 27", "Sun Jun 28"],
  "affinity_prompt": null,               // usually null
  "affinity_options": [],                // usually empty

  "missing": ["venue_name", "starts_at"] // what's still needed
}
```

### The two kinds of options

| Field | When present | How to render | On tap |
|-------|--------------|---------------|--------|
| `suggestions` (string[]) | most collecting turns | row of tappable pills | send the pill **text** as the next message |
| `affinity_prompt` (string) + `affinity_options` (string[]) | exactly one turn, right before publish | the prompt as a question + tappable pills | send the chosen option **text** as the next message |

**Tap contract is always the same:** tapping any chip = sending its text as the
user's next turn (identical to typing it).

```ts
// suggestion / affinity chip tap
onClick={() => sendMessage(sessionId, chipText)}
```

The `suggestions` are **context-aware** — they match the question Lana just asked:

| Lana asks…                            | `suggestions` you'll get                                  |
|---------------------------------------|-----------------------------------------------------------|
| what to call it / what kind           | **AI-tailored titles** for this event, e.g. `["Brazilian Moms Meetup", "Brazil Heritage Mixer"]` |
| when / which day                      | concrete dates: `["Sat Jun 20", "Sun Jun 21", …]`         |
| what time to start                    | `["9 AM", "12 PM", "3 PM", "6 PM"]`                       |
| what time in the afternoon            | `["12 PM", "1 PM", "2 PM", "3 PM"]`                       |
| where / which place                   | `["The playground", "The park", "My place", …]`           |

The **affinity question** (`affinity_prompt` + `affinity_options`) is also AI-tailored to
the event now — e.g. a Brazilian-moms event gets `"Who's it for?"` →
`["Brazilian moms only", "All Latina moms", "Open to everyone"]`, and it always shows
once before publishing (it no longer gets skipped when a cohort tag was auto-detected).

Dates are computed live, so they're always real upcoming days.

---

## 4. `ui_actions` — post-publish CTA buttons

On `ui_intent: "event_created"`, render `response.ui_actions` as buttons:

```jsonc
"ui_actions": [
  { "id": "invite_mom", "label": "Send to a mom", "message": "who can I invite to this event?", "style": "primary" },
  { "id": "later",      "label": "Maybe later",   "message": "not now",                          "style": "secondary" }
]
```

Shape (`UiActionRow`):

| Field          | Type                                   | Notes                              |
|----------------|----------------------------------------|------------------------------------|
| `id`           | string                                 | stable key                         |
| `label`        | string                                 | button text                        |
| `message`      | string                                 | **send this** as the next turn on tap |
| `style`        | `"primary" \| "secondary" \| "ghost"`  | button variant                     |
| `intro_id`     | string \| null                         | present on intro actions only      |
| `peer_user_id` | string \| null                         | present on peer actions only       |

Same tap contract: `onClick={() => sendMessage(sessionId, action.message)}`.

---

## 5. Integration recipe

```ts
const turn = await sendMessage(sessionId, text, intentHint);

switch (turn.ui_intent) {
  case "collect_event_detail":
    renderEventCard(turn.event_draft);          // title + meta chips
    if (turn.event_draft?.affinity_prompt) {
      renderAffinity(turn.event_draft.affinity_prompt,
                     turn.event_draft.affinity_options); // tap → sendMessage(text)
    } else {
      renderChips(turn.event_draft?.suggestions ?? []);  // tap → sendMessage(text)
    }
    break;

  case "event_created":
    renderPublishedCard(turn.event_draft);       // "it's live"
    renderActions(turn.ui_actions);              // tap → sendMessage(action.message)
    break;

  // …other intents unchanged
}
```

**Entry point:** the "A meet to host" CTA must send with `intent_hint: "host_event"`.
After that, no hints are needed — the backend stays in host mode and releases on
publish, on an explicit cancel/topic-change, or after a turn cap (never loops).

---

## 6. TL;DR for the FE team

- All chips/buttons come from the **backend** — render `event_draft.suggestions`,
  `event_draft.affinity_prompt` + `affinity_options`, and `ui_actions[]`.
- **Tap = send the chip/action text as the next message.** Always.
- Switch your UI on `response.ui_intent`. Two new values: `collect_event_detail`
  (capturing) and `event_created` (published).
- `debug`, `timing_ms`, `message_count`, `onboarding_step` are **removed** from the
  response — don't depend on them.

---

# Lana — Pass Along an Item (in-chat) · Frontend Integration

_Added: 2026-06-19_

The **"Something to pass along"** flow (give away / swap an item) runs in the same
unified chat, the same way hosting does. Same turn endpoint, same tap contract.

## 1. Entry

The "Something to pass along" CTA sends an intent hint so the backend enters the
flow deterministically:

```ts
sendMessage(sessionId, "I have something to pass along", "pass_along")
// → body: { message: "...", intent_hint: "pass_along" }
```

Lana then: asks what the item is → extracts entities → asks the one missing detail
(free/swap, condition) with options → offers a photo → **lists it** to the block as
a `swap_offer` (so neighbors who are *looking for* that item get matched + pinged).

## 2. `ui_intent`

- `collect_item_detail` → render the item card (`item_draft`).
- `item_listed` → render the "listed" confirmation (`item_draft`).

## 3. `item_draft` — card contents, chips, suggestions

```jsonc
"item_draft": {
  "title": "3T rain boots",          // card header (null until known → P1 "what is it?")
  "category": "kids clothing",
  "condition": "slightly worn",
  "stage": "3T",
  "intent_type": "free",             // "free" | "swap"
  "photo_url": null,                 // set after upload (see §4)

  // "Heard you" colored entity chips — tap to CORRECT that field
  "chips": [
    { "label": "Pass along",  "tone": "coral",  "field": "intent_type" },
    { "label": "3T",          "tone": "sky",    "field": "stage" },
    { "label": "Free",        "tone": "green",  "field": "intent_type" },
    { "label": "slightly worn","tone": "amber", "field": "condition" }
  ],

  // tappable answers for the current question (condition / free-or-swap)
  "suggestions": ["Brand new", "Lightly used", "Well-loved"],

  "listed": false,
  "signal_id": null
}
```

| Field | Render | On tap |
|-------|--------|--------|
| `chips[]` | colored pills (`tone` → coral/sky/green/amber/violet) | send **`fix:<field>`** (re-asks that entity) |
| `suggestions[]` | neutral pills | send the pill **text** |

**Chip correction contract:** tapping a chip sends `fix:<field>` (e.g. `fix:condition`)
as the next message — the backend clears that field and re-asks it. That's the only
special string; everything else is plain text.

## 4. Photo

When the item is shaped and no question is pending, the card shows **Add photo** /
**List it now**. Photo upload is a separate multipart endpoint:

```
POST /lana/sessions/{session_id}/signal-photo   (multipart, field: file)
→ { "photo_url": "https://…" }
```

The backend stores the photo AND attaches the URL to the session's item draft. After
a successful upload, send `list it` to finalize — the next turn lists the item with
its photo and flips `ui_intent` to `item_listed`.

```ts
await uploadSignalPhoto(sessionId, file);   // returns photo_url, attaches to draft
sendMessage(sessionId, "list it");          // → item_listed
```

To list **without** a photo, just send `list it` (or tap "List it now").

## 5. Matching (why this is a `swap_offer`, not a marketplace listing)

The item is saved to `local_signals` as a `swap_offer`, so the existing matcher pairs
it with anyone who has a `swap_seek` ("I'm looking for a bicycle") on the block and
notifies them. The photo rides along on the match.

## 6. TL;DR

- CTA sends `intent_hint: "pass_along"`.
- Switch on `ui_intent`: `collect_item_detail` (capturing) / `item_listed` (done).
- Render `item_draft.chips[]` (colored, tap → `fix:<field>`) + `item_draft.suggestions[]`
  (tap → text). All backend-driven.
- Photo: `POST /lana/sessions/{id}/signal-photo` (multipart) → then send `list it`.

---

# Lana — Share a Tip / Recommendation (in-chat) · Frontend Integration

_Added: 2026-06-19_

The **"A tip to share"** CTA opens an in-chat recommendation capture, same patterns as
pass-along. Saved as a `tip_share` so neighbors asking for that category get matched.

## 1. Entry
```ts
sendMessage(sessionId, "I have a tip to share", "tip_share")
```
Lana asks what to recommend → extracts name/category/trait → asks the missing piece
(who/where, with **real nearby places from Google** when it's place-based) → shows an
assembled card with a **dual CTA**.

## 2. `ui_intent`
- `collect_tip_detail` → render the tip card (`tip_draft`).
- `tip_listed` → render the "on your block" confirmation.

## 3. `tip_draft`
```jsonc
"tip_draft": {
  "name": "Dr. Sarah",
  "category": "pediatric dentist",
  "trait": "twin-friendly",
  "locality": "Lake Nona",
  "chips": [ { "label": "★ Recommendation", "tone": "amber", "field": "category" }, … ],
  "suggestions": ["Lake Nona Family Park", "Crescent Park"],  // Places results OR option chips
  "ready": false,   // true → render the dual CTA
  "listed": false
}
```
- `chips[]` — tap → send `fix:<field>` (re-asks that entity). Same contract as items.
- `suggestions[]` — tap → send the text. For place-based tips these are **real nearby
  places** (Google Places, searched around the block); otherwise AI-tailored options.

## 4. Dual CTA (when `tip_draft.ready === true`)
- **Pass the tip along →** : send `"pass the tip along"` → saves to the block (`tip_listed`).
- **Send to a mom you know** : open the native share sheet with the tip text, then also
  send `"pass the tip along"` so it's posted too.

## 5. Matching
Saved as `tip_share` in `local_signals` → the matcher pairs it with any `tip_seek`
("anyone know a good pediatric dentist?") on the block and pings them.

## 6. TL;DR
- CTA sends `intent_hint: "tip_share"`.
- Switch on `ui_intent`: `collect_tip_detail` / `tip_listed`.
- Render `tip_draft.chips[]` (tap → `fix:<field>`) + `tip_draft.suggestions[]` (tap → text).
- When `tip_draft.ready`, show the two CTAs (Pass along / Send to a mom).
- Nearby place options come from Google Places — no FE work, they arrive in `suggestions`.
