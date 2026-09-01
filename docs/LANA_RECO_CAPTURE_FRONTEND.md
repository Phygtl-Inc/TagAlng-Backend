# Recommendation capture — dynamic question sets (C-4-RECO / C-4-EVENT-P2B)

Backend + frontend for the **share a recommendation** flow: the swipe carousel, the chat
fork, the ready card, and the typed storage behind them.

**The one thing to internalise:** the questions are **written per recommendation by the
LLM**, not chosen from a fixed set per type. A recipe gets asked about taste and
difficulty; a pediatrician gets asked what ages she sees. So the frontend renders
*whatever steps arrive* and branches only on `kind`. **Adding, removing or rewording a
question is a backend-only change with zero frontend work.** The moment the FE switches on
a field name, that stops being true.

Everything here is **additive**: `tip_draft` gains two fields, and the carousel is a new
component behind a condition that is false on every turn that has no steps.

---

## 1 · The wire contract

`tip_draft` (on `POST /lana/sessions/{id}/messages`, and inside the terminal `result`
frame on `/stream`) gains:

```jsonc
"tip_draft": {
  "name": "Dr. Sarah",
  "category": "pediatrician",
  "trait": "gentle with my toddler",
  "locality": "Lake Nona",
  "reco_type": "professional",     // NEW · fixed taxonomy, see §2
  "steps": [                       // NEW · the carousel, in ask order
    { "field": "profession",       // storage key. NOT an enum — never switch on it
      "label": "Profession",       // the eyebrow, already in the user's language
      "question": "What kind of doctor is Dr. Sarah?",
      "kind": "text",              // the ONLY thing to branch on, see the table below
      "placeholder": "Pediatrician",   // example answer for THIS subject; may be absent
      "options": [],               // present for choice / toggle / agree
      "required": true,            // ready card is blocked while a required step is empty
      "answer": null },            // filled in as answers land, on every later turn
    …
  ],
  "missing": ["helped_with"],      // required fields still unanswered
  "chips": […], "suggestions": […], "ready": false, "listed": false
}
```

Types live in `src/lib/lana.ts` (`RecoStep`, `TipDraft`) and `src/lib/lana.schema.ts`
(`recoStepSchema`). Both are `.loose()`, so a new key added by the backend survives
without a FE release.

### `kind` — the whole rendering contract

| `kind` | control | what to post back |
|---|---|---|
| `text` | free-type input, `placeholder` is the hint under it | the text |
| `choice` | `options` as tappable chips **over a text box** — a real answer is often none of the chips | the text, whether typed or tapped |
| `place` | Google Places picker (`PlaceSearchCombobox` + `searchPlacesForCircle`) | `"Name · Address"` |
| `toggle` | two buttons from `options` — the consent step | the option string **verbatim** |
| `agree` | multi-select over `options`, which arrive as `"easy parking ×2"` | the picked options **comma-joined** |

Rules that matter:

- **`options` for a toggle are the backend's own wording.** Post one of them back
  unchanged; don't send `true` / `"yes"`.
- **`agree` must be comma-joined.** The tally query splits stored answers on `,` `;` `·`
  to count them, so `"easy parking, books online"` becomes two tallies and
  `"easy parking and books online"` becomes zero.
- **Unknown `kind` → fall back to `text`.** New kinds will appear; an unrecognised one
  must render a text box, not nothing.
- **`label` / `question` / `placeholder` are already localised** — the backend generates
  them in the session's language. Never run them through `next-intl`. Only the carousel's
  own chrome (CTA, banner, "Can't find it? Type it") is in `messages/*.json` under
  `recoSetup`.

---

## 2 · `reco_type` — fixed taxonomy, dynamic questions

Seven values, `CHECK`-constrained in the DB and indexed:
`professional · service · restaurant · recipe · product · location · diy`.

Split by **what a neighbour would DO with it**, never by topic — go eat there =
restaurant, cook it = recipe, buy it = product, spend time there = location. That's what
keeps *"great biryani at Zaiqa"*, *"here's my biryani recipe"* and *"their frozen biryani
packs"* out of one bucket.

Use it for grouping, filtering and iconography. **Do not** use it to pick which questions
to render — that's exactly what the generated set replaces.

---

## 3 · Two forks, one step list

`C-4-EVENT-P1B-FORK` — same steps, two ways to answer them.

### Cards fork (default) — `RecoSetupCarousel`

`src/features/voice/components/reco-setup-carousel.tsx`. Renders when:

```ts
uiIntent === 'collect_tip_detail' && (tipDraft?.steps?.length ?? 0) > 0 && !tipDraft.ready
```

Takes the bubble's place, exactly like `HostSetupCarousel` does for hosting, and is built
on the same `input-carousel` primitives (`QuestionShell`, `Carousel`, `Dots`) — so the
numbered badge, the morphing `Looks good · next` CTA and the dot rail behave identically
in both flows.

On submit it posts **every answer at once**:

```ts
POST /lana/sessions/{id}/tip-setup      // { answers: { <field>: "<text>", … } }
     → { ok: true, missing: ["where_to_buy"] }
```
then sends the message `"Looks good"`, which is what advances the turn to the ready card.
`setTipSetup()` in `src/lib/lana.ts`; the two-step (stamp, then message) mirrors
`setEventSetup` — the POST is what the turn advances *with*, the message is what advances
it.

The endpoint keeps **only fields present in that session's own step set**. Since the set
is generated there is no enum to validate against, so this intersection is also what stops
a client writing arbitrary keys into a draft. Blank values never erase an existing answer.

### Chat fork — `"Just chat with me"`

Sets local state that hides the carousel; Lana then asks the same steps one at a time and
the ordinary composer answers them. State is local to `thought-chat.tsx` and resets when
the subject changes, so the next recommendation opens on cards again. While the carousel
is up, `TipDraftActions`' quick-reply pills are suppressed so the two don't stack.

---

## 4 · The ready card and corrections

`TipDraftCard` renders the answered steps as label/answer rows (`C-4-RECO-P3`). Tapping a
row sends `fix:<field>`, which re-opens **that** step with its own question and lands the
next reply back on it.

`fix:` also works for the draft-level fields (`name`, `category`, `trait`, `locality`,
`details`) — those are the chip taps that already existed.

---

## 5 · What the backend owns (so you don't re-implement it)

`services/lana-worker/app/reco_question_sets.py`:

- **Generation** happens inside the existing extraction call (`tip_share.py`), on the turn
  the type + name land. **Written once per recommendation** and then cached on the draft —
  the questions cannot change under a user who is halfway through answering them.
- **`validate_steps()`** is what makes a model-written set askable. It slugifies field
  keys, drops duplicates, drops "questions" that aren't questions, drops private asks
  (home address, DOB, earnings), caps the length, and drops a model-generated copy of the
  tail steps.
- **`_FLOOR`** is the per-type set of fields a reader cannot act without — a plumber must
  be reachable, a recipe must be shoppable. Missing floor fields are inserted; the model's
  own ordering is otherwise preserved. **Required = the type's first two floor fields**,
  wherever they sit in the set.
- **Tail steps** are appended server-side and are never model-written: the consent toggle
  (its wording gates whether neighbours may message the author, so it must be identical
  for everyone) and the agree row (real tallies of what other neighbours logged about the
  same subject). **The agree step only exists when there are tallies** — an empty one is a
  dead card, which is why the step count varies.
- **Fallback:** if generation fails or the model is unconfigured, the type's static set is
  used. The flow never blocks on generation, so `steps` is never empty once a type is known.

Storage (`local_signals`): `reco_type`, `reco_subject` (normalised name, what tallies group
on), and `reco_fields` — an **array** of the answered steps, each carrying the question it
answered. It has to be self-describing: with generated questions, `{field: answer}` loses
the wording and a reader card ends up with values and no labels.

---

## 6 · How to make a change

| You want to… | Where |
|---|---|
| Add / remove / reword a question | Backend only — the prompt in `tip_share._STEPS_SPEC`, or `_SETS` for the fallback. **No FE change.** |
| Change which fields are mandatory | `_FLOOR` in `reco_question_sets.py` (first two entries per type are the required ones) |
| Add a recommendation type | `_SETS` + `_FLOOR` + `TYPE_RULES`, and the `CHECK` in a new migration |
| Change carousel chrome copy | `messages/{en,es,pt-BR}.json` → `recoSetup` |
| Make the location step a map picker somewhere else | add the field name to `_PLACE_FIELDS` (backend) — the FE already handles `kind: "place"` |
| Restyle a step control | `StepControl` in `reco-setup-carousel.tsx` — one switch on `kind` |
| **Add a new `kind`** (photo, price, rating…) | three places: `_kind_for()` in `reco_question_sets.py`, `RecoStep` in `models.py` + `lana.ts` + `lana.schema.ts`, then a branch in `StepControl` |
| Style the agree row's counts separately | needs the backend to send `{attr, n}` objects instead of `"attr ×n"` strings — `tail_steps()` |
| Show a map link / real distance for a place answer | needs `googlePlaceId` carried on the step; the stored array can take one more key (`ponytail:` note in `PlaceAnswer`) |

---

## 7 · Testing

```bash
# UI with no backend — three sets through one component + the ready card
cd tagalng-pwa-main && npm run dev      # then /dev/recommendation   (needs Node ≥ 20.9)

# Backend, no LLM key and no DB — both forks printed end to end
cd services/lana-worker && .venv/bin/python scripts/try_reco_carousel.py
.venv/bin/python -m pytest tests/test_reco_question_sets.py tests/test_tip_setup_endpoint.py -q

# Real model writing real sets (needs a working LLM key)
./scripts/try_reco_capture.py "Dr. Sarah in Lake Nona is so gentle with my toddler"
```

Phrases that exercise each type: `my feijoada — the whole block asks for the recipe`
(recipe) · `the Hatch Rest got our toddler sleeping through` (product) · `Boxi Park, kids
run wild while you actually sit and eat` (location) · `Mike the plumber fixed our water
heater same day` (service) · `I recommend Zaiqa for biryani` (restaurant, and the only one
that also matches the legacy entry regex).

---

## 8 · Known gaps

- **Reader side is blind to this.** `find_neighbor_tips` doesn't return `reco_fields`, so a
  recommendation read back *from the DB* (tip feed, rec cascade) still shows the
  `detail_text` sentence, not structured rows. The ready card renders from the live draft.
- **No browse-by-type.** The index exists; the query doesn't.
- **No verify gate on the capture.** Every other signal save is gated behind
  `phone_verified`; this one isn't, so an unverified guest can post. Attribution is intact
  (anonymous users are real auth users) but the author has no name.
- **Place answers are text, not pins** — see §6.
- **Migration state:** `20261117120000` + `20261118120000` are applied on **prod**; dev has
  only the first, so on dev `reco_fields` stays `[]` and the write failure is swallowed by
  design (the tip still posts, as prose in `detail_text`).
