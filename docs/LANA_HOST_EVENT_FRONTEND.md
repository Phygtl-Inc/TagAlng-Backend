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
| what to call it / what kind           | `["Playdate at the park", "Weekend playgroup", …]`        |
| when / which day                      | concrete dates: `["Sat Jun 20", "Sun Jun 21", …]`         |
| what time to start                    | `["9 AM", "12 PM", "3 PM", "6 PM"]`                       |
| what time in the afternoon            | `["12 PM", "1 PM", "2 PM", "3 PM"]`                       |
| where / which place                   | `["The playground", "The park", "My place", …]`           |

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
