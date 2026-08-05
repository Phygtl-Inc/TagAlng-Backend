# The recommendation cascade — backend answers (§12 / issues #68)

Answers the four gaps and the two smaller items in the Backend Asks §12, for the
`looking.tip` turn on `POST /lana/sessions/{id}/messages` (and `/stream`, same payload
inside the terminal `result` frame).

Everything here is **additive**. No existing field changed shape, and every new field is
absent — not empty-stringed — when the turn has nothing to say. A frontend that ignores
this document keeps working exactly as it does today.

**One migration ships with it:** `supabase/migrations/20261002120000_tip_rec_cascade.sql`
(`find_neighbor_tips` v2). It is **not pushed yet**. Until it is applied, the worker still
answers recommendation asks in prose, and the new row fields simply do not arrive — the
worker detects the older function and retries against it (see *Degrade behaviour*).

---

## (a) The rec itself, per row

`peer_matches[]` is now populated on a `looking.tip` turn, one row per neighbor who posted
a matching `tip_share`. Three new fields on `PeerMatchRow`:

```jsonc
{
  "peer_user_id": "…",
  "nickname": "Marisol",
  "avatar_url": "https://…",
  "tip_text": "Dr. Reyes at Lake Nona Smiles — so gentle with the toddlers, and she takes our insurance.",
  "tip_signal_id": "…",          // the local_signals row — for opening/attributing the tip
  "distance_text": "a few minutes away",
  "trait_tags": ["Toddler stage", "Same insurance"],
  "actions": [{ "id": "peer_card_nudge", "label": "Nudge", "message": "introduce me to Marisol", … }]
}
```

**What these rows deliberately do NOT carry:** `match_stars`, `match_band`, `match_badge`,
`similarity_score` — all `null`. Nothing here compared two people; the row's claim is
"this neighbor recommended this, and they are this far away", and that is all it will ever
assert. `trait_tags` on a cascade row are the **tip's** tags (what the rec is about), not
shared identity. Use `tip_text != null` to tell a cascade row from a claim-affinity row.

`discovery_surface` on these turns is counted the same honest way:
`strong_count` = rows carrying a real rec, `partial_count` / `weak_count` = 0,
`status_label` = `"3 neighbor recs"`, `weak_peer` = `null`.

Privacy note, since it is a change: `tip_share` authors are now identifiable
(`peer_user_id`, avatar). A shared tip is a deliberate broadcast to the block and the
nickname was already on the wire — what was missing was the ability to attribute or reply
to it. Recommendation **seeks** and claim-based matches stay anonymous exactly as before.
A withdrawn tip (`close_local_signal`) leaves the cascade on the next turn.

## (b) Distance

`distance_text` is `humanize_distance_text()` over the two coarse points (block centroid,
then ZIP centroid) — the same helper the `*_near` peer RPCs use. EN is miles, ES/PT km,
walking phrasing under 1600 m. Rendered from `users.locale`.

`null` whenever either side's point is unknown. Render nothing then — do not fall back to
`matching_peer_label`, which is a different fact (a shared thread, not a distance).
Precision is block-level by design, so same-block neighbors legitimately read as the
closest bucket.

## (c) Server-side re-rank

Two controls, both re-ranking over a **wider fetch than the page you were shown** (12 rows
vs 3) — which is the part a client-side sort cannot do.

**Weights (preferred).** Send the tapped threads with the hint:

```jsonc
POST /lana/sessions/{id}/messages
{ "message": "Same insurance",              // the user-visible utterance, your choice
  "intent_hint": "look_tip_rerank",
  "weights": ["Same insurance", "Toddler stage"] }   // max 8
```

A weight counts when it appears in that row's own words — its `trait_tags` or its
`tip_text`. Match strength stays the tiebreak. Weights only ever **re-order**: an
unmatched weight drops nobody and empties nothing. The turn re-runs the last ask
(remembered server-side), writes nothing, and comes back with `peer_matches` re-ordered,
`routing.tool_called: "tip_seek_reranked"`.

**Widen.** `"Find more people"` (exact payload, see below) searches past the user's own
block via the radius lever (~5 mi), same response shape. It is explicitly *not* read as
posting consent — see the protocol table.

## (d) The extracted ask, before anything is broadcast

New top-level `ask_draft` on the response, on every `looking.tip` turn — including the
verify-gate and ZIP-gate turns, so a gated ask arrives with the user's actual words
attached rather than a bare "verify first":

```jsonc
"ask_draft": {
  "title": "Gentle pediatric dentist",
  "detail": "Gentle with toddlers · Lake Nona · open to any insurance.",
  "category": "pediatric dentist",
  "locality": "Lake Nona",
  "chips": [{ "label": "pediatric dentist", "tone": "sky", "field": "category" }],
  "ready": true
}
```

`chips[].field` is `category` | `locality` | `qualifier`; `tone` is from the existing
palette (`coral | sky | green | amber | violet`). `title` is AI-rewritten into a card
headline; with no LLM available it falls back to the user's ask verbatim, so the card is
never empty and never invents a qualifier they did not say. Absent (`null`) on every
non-tip turn.

`ask_draft` is a **receipt of understanding, not a posting**. It rides along with the
answer — the frame's P2 and P3 are one turn on the wire, because the ask is answered from
neighbor tips + Places before anything is written. The write still happens only on an
explicit yes to the ask-neighbors offer.

---

## Smaller item 1 — a documented `intent_hint` for the seek

`intent_hint: "look_tip"` — this message *is* a recommendation ask; the worker answers it
and posts nothing. Deterministic, so the fork no longer depends on the classifier (which
did route correctly on every probe — this makes it a guarantee). Mirrors
`look_meet` / `host_event` / `pass_along`. Consumed on that turn: the next turn routes
normally.

## Smaller item 2 — the protocol utterances, confirmed

Your guesses were close; two are adjusted. These are the **canonical payloads** the router
matches when the LLM is unavailable — post them verbatim as `message`, and localize only
the label you render (same contract as `Yes, ask my neighbors`).

| Frame | Label you render | `message` payload | What the worker does |
|---|---|---|---|
| P2 | Looks good | `Looks good` | Acknowledges; **does not** post, does not re-ask |
| P2 | Let me tweak that | `Let me tweak that` | Asks what to change; the next message is folded into the ask and re-answered |
| P4 | Keep listening for me | `Keep listening for me` | Accepts the ask-neighbors offer → posts (was: `keep listening for me`, now an explicit accept) |
| P4 | Find more people | `Find more people` | Widens past the block. **Never** read as posting consent |

Free-text equivalents in any language still work — each of these is read by an AI
interpreter first, and the exact payload is only the no-LLM floor.

**One ordering rule worth knowing.** If both the ask-neighbors offer and the ask-draft card
are on screen and the user says something affirmative, the **spoken offer wins**: they are
answering the question Lana asked out loud, so it counts as posting consent. Draft
confirmation is only read for turns the offer reader passed on. This is why "Looks good" is
safe to send as its own payload but a bare "yes" is not.

## Guest behaviour

Unchanged and deliberate: the first turn still hits the verify gate
(`routing_phase: "await_signup_phone"`, `requires_phone_verification: true`), because the
block read and the Places lookup both need a verified account. What is new is that the gate
now arrives with `ask_draft` populated, so the user sees their ask was heard. A
preview-then-unlock shape here remains a product call, not a wire limitation.

## Degrade behaviour

| State | What arrives |
|---|---|
| Migration not applied | Worker retries the v1 function: prose answer as today, `peer_matches` empty, no `tip_text` / `distance_text`. `ask_draft` still arrives (it needs no DB). |
| No neighbor has posted a matching tip | No `peer_matches` — the Places fallback and the ask-neighbors offer, exactly as today. |
| Distance unresolvable | `distance_text: null` on that row only. |
| No LLM configured | `ask_draft.title` is the user's ask verbatim; protocol payloads still work via the exact-match floor. |

## Where this lives

- `app/tip_rec_cascade.py` — rows, re-rank, counts strip
- `app/tip_ask_draft.py` — the seek-side draft + correction merge
- `app/discovery_route.py` — `_tip_seek_answer_turn`, `_try_tip_cascade_control_turn`,
  `_try_ask_draft_reply_turn`, `_try_tip_tweak_answer_turn`
- `tests/test_tip_rec_cascade.py` — including the two consent guards ("Find more people"
  and a draft confirmation must never write)
