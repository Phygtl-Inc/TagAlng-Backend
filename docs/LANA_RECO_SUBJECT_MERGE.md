# Recommendation subjects — merging recs about the same thing

Status: **Stages 1 and 2 built** (2026-09-22), unpushed. Stages 3-4 are still plan.

## What changes

Today a recommendation ask returns **people**: one row per neighbour whose tip matched,
carrying their words, their distance and a Nudge. Three neighbours who all recommend
Dr. Sarah produce three rows, and a reader has to notice they are the same dentist.

This changes the ask results to return **subjects**: one card per recommended thing, with
`vouch_count` = how many neighbours recommended it. Three recs about Dr. Sarah become one
card reading "3 vouched".

The browse feed (`recent_neighbor_tips`, screen 04) does **not** change — see Decisions.

## Decisions already taken

| Decision | Answer | Consequence |
|---|---|---|
| Which surfaces merge | **Ask results only** | Recent feed stays one card per author with Nudge + Helpful. Browse = who said what; ask = what the neighbourhood thinks. |
| 👍/👎 Helpful on a merged card | **Stays per-contributor** | `tip_helpful` keeps pointing at a signal. No data migration, no reinterpretation. Helpful appears in the evidence list, not on the card head. |
| Explicit "I vouch" tap | **No — count is purely derived** | `vouch_count` is exactly "how many people wrote a rec about this". No write path, no endpoint. `/lana/tips/vouch` stays 410 and `tip_vouches` stays dormant where 20261120120000 left it. |

The third decision is the one that keeps this honest. A derived count cannot be inflated
by someone who has never been to the place, which was the entire objection in
20261107120000 — *"the one number a stranger is meant to trust"*.

## Which types merge at all

Not all of them, and the dividing line is not "can we ground it to a place" — that is only
the easiest *mechanism*. The question is:

> **Are the captured fields observations ABOUT a shared referent, or are they the artifact
> itself?**

Compare two `reco_fields` sets:

```
professional   gentle · walk-in · takes insurance     <- three witnesses to one object
recipe         ingredients · steps · 45 min · easy    <- this IS the recipe
```

Three neighbours recommending Dr. Sarah are three independent observations of one dentist;
their fields accumulate, and "3 vouched" means three people stand behind the same
practitioner. Three neighbours recommending banana bread have **three different recipes**.
Merging them would force us to discard two authors' ingredients and then claim three people
vouched for the one we kept. That is the same lie as merging two dentists, arriving by a
different road.

| reco_type | subject | fields are | merges? |
|---|---|---|---|
| `restaurant`, `location` | a map point — Places picker (`_PLACE_SUBJECT_TYPES`) | observations | **yes**, Stage 1 |
| `professional`, `service` | maybe a map point — the extractor's `place_based` read decides | observations | **yes** — Stage 1 when grounded, Stage 2 when not (a plumber, a nanny, a tutor-who-comes-to-you has no storefront) |
| `product` | a SKU — "Cosori gooseneck" | observations | **yes**, Stage 2 — merges well, but a product is never a place, so it needs its own identity space |
| `recipe`, `diy` | the thing itself | **the artifact** | **no** |
| `other` | anything — a bus route, a Facebook group, a broker | mixed | **no** |

`recipe` and `diy` keep exactly today's behaviour: one card per author. That is the
**correct** answer, not a fallback. A reader asking for a banana bread recipe wants three
recipes to choose between, not one blended one that belongs to nobody.

Two consequences:

- **Ask results carry two card languages** — merged subject cards for some types,
  per-author rows for others. In practice a results page is type-coherent, since the
  `reco_type` filter chips (20261128120000) already scope it, but the surface has to
  handle both.
- **A count is not a merge.** "3 neighbours have a banana bread recipe" as a section
  header is a grouping affordance and stays honest, because it claims no corroboration.
  Worth having eventually; it is not this work.

Deliberately skipped: recipes naming a published source ("the NYT no-knead", "Ottolenghi's
shakshuka") genuinely *are* shared referents. But each author still transcribes their own
version into `ingredients`/`steps`, and canonical-recipe detection is a lot of machinery
for a rare case. Revisit only if the data shows it.

## The trap this design avoids

`local_signals.embedding` (768-dim, HNSW, 20261126120000) is the obvious clustering tool
and it is the wrong one. `tip_embedding_text()` covers name + category + place +
description + fields, so the vector measures **what the rec is about**, not **who it is
about**:

```
"Dr. Sarah · pediatric dentist · Lake Nona · gentle with toddlers"
"Dr. Ahmed · pediatric dentist · Lake Nona · so gentle with little ones"
```

Near-identical vectors, two different dentists. Merging on cosine would fold them together
and the vouch count would be a lie. **Topic similarity is not entity identity.** Every
matching signal below is drawn from the subject's identity (name, category, locality) and
never from the recommendation's content.

`reco_subject_key()` already anticipated this work
([local_signals.py:113](../services/lana-worker/app/local_signals.py#L113)):

> *"Case and whitespace only — no stemming, no fuzzy match, so 'Dr Sarah' and 'Dr. Sarah'
> stay separate subjects. ponytail: exact key. The pgvector path is the upgrade if that
> turns out to be too strict."*

This is that upgrade, with entity resolution in place of raw pgvector.

---

## Stage 1 — `reco_subjects` and grounding at capture  ✅ BUILT

Migration `20261219120000_reco_subjects.sql`. The piece everything else sits on.

### Schema

```sql
create table public.reco_subjects (
  id              uuid primary key default gen_random_uuid(),
  subject_key     text not null,              -- normalized identity string
  google_place_id text unique,                -- set where the subject grounded
  display_name    text not null,              -- author casing of the first rec
  category        text,
  locality        text,
  lat             double precision,
  lng             double precision,
  created_at      timestamptz not null default now()
);

alter table public.local_signals
  add column subject_ref uuid references public.reco_subjects (id);

create table public.reco_subject_merges (
  from_subject uuid not null references public.reco_subjects (id),
  into_subject uuid not null references public.reco_subjects (id),
  method       text not null,                 -- 'google' | 'blocked' | 'adjudicated' | 'confirmed'
  confidence   real,
  decided_at   timestamptz not null default now(),
  primary key (from_subject, into_subject)
);
```

`reco_subject_merges` records subject→subject consolidation. Stage 2 turned out not to need it — everything it does is signal→subject attachment, audited on the signal itself (`subject_method`, `subject_confidence`, `subject_candidate_ref`). The table is left standing for the consolidation/unmerge tool, and is unwritten today.

`local_signals.circle_place_ref` already exists and is **not** this — it is the community
the tip was shared *into*, not the subject it is *about*.

### Resolution

Runs at the tip_share call site **after** the insert, beside `tag_local_signal`, not
threaded through `save_local_signal` — that function's own comment says why
([tip_share.py:850](../services/lana-worker/app/tip_share.py#L850)): *"150 lines of
dedupe/match/notify: threading one column through it is how a behaviour goes missing in a
copy-paste."* It is also the only place the draft — and so the tapped place — is in scope.

Best-effort throughout, under the rule `set_signal_embedding` already follows: **a tip that
posted must never fail on its subject.** Unresolved rows keep `subject_ref` null, render
exactly as they do today, and the backfill picks them up.

Two paths, and the first one is nearly free.

**A. The user tapped a real place — exact, no inference.**

A recommendation's subject step is already a map search, and a pick already carries a real
`googlePlaceId`. It was being discarded on the way to the worker at **three** separate
layers, while the community lane next door kept it:

| layer | was |
|---|---|
| `PlaceAnswer.onPick` | no `onPin` → flattened the pick to `"Name · Address"` text |
| `onTipSetup(answers, _googlePlaceId, …)` | parameter named `_googlePlaceId` — received, never forwarded |
| `setTipSetup` / `TipSetupRequest` / `/tip-setup` | no field for it at all |

A fourth drop sat in the chat fork, where `nearby_place_suggestions` asked Google for
`places.displayName` only and returned `list[str]`, throwing away ids it had just fetched.

All four now keep it, and grounding takes the strongest available, genuinely ranked:

1. **the carousel pick** — an id the neighbour chose off a map. Certain.
2. **the chat-fork chip** — an id we offered and she answered with. Certain.
3. **the search** — our guess at what she typed, floored at 0.82. A judgement.

The carousel matters most: it is the primary capture surface, and before this a neighbour
could tap the exact clinic and still have the tip re-derived by fuzzy search.

The community lane's rule deliberately does **not** transfer. There, a typed name is
rejected and the step re-asked, because an ungrounded community is invisible everywhere.
A recommendation must still accept a typed name — a plumber, a nanny and a
tutor-who-comes-to-you have no listing to pick — so the id is opportunistic, never
required, and its absence is an ordinary outcome rather than an error.

A pick is also dropped again if the neighbour then types over it: the client keeps the text
the pick wrote and only sends the id while the answer still matches it, so an edited answer
cannot ship the id of a place she just rejected.

**B. The user typed a name instead — search, with a floor.**

[`search_places`](../services/lana-worker/app/places.py#L148) on `reco_name` + `reco_place`,
biased to the author's origin. Accept the top hit only above a confidence floor; below it,
leave `subject_ref` null and let Stage 2 or the backfill try again. A wrong merge is worse
than no merge.

Both paths then upsert `reco_subjects` on `google_place_id` (unique — a lookup, not a
search) and stamp `subject_ref`. Grounding also hands over `lat`/`lng`, which Stage 3 needs
(see Open question 1).

### Backfill

`scripts/backfill_reco_subjects.py`, modelled on `scripts/backfill_tip_embeddings.py`.
Idempotent, resumable, safe to run repeatedly.

---

## Stage 2 — the second identity space  ✅ BUILT

Migration `20261220120000_reco_subject_resolve.sql`, `app/reco_subject.py`.

Not a fallback for Stage 1 failures: a different kind of identity. Covers `product`
(a SKU is never a map point, so it skips the Places search entirely) and the
`professional` / `service` / place subjects Google could not settle — a plumber, a nanny,
"Chef Ana meal prep" have no storefront to find.

`recipe`, `diy` and `other` are **out of scope entirely** — see Which types merge.

A place id is a fact. Everything here is a judgement, so every attachment records how it
was reached, on `local_signals.subject_method` + `subject_confidence`:

| method | meaning |
|---|---|
| `google` | exact `google_place_id` match (Stage 1) |
| `new` | created its own subject, merged with nothing |
| `blocked` | name + locality scored past the auto floor — no model call |
| `adjudicated` | a model call on a shortlist said yes |
| `confirmed` | the sharer was asked and said yes — **not built, see below** |
| `ambiguous` | a candidate was found and deliberately NOT merged |

### The three bands

```
score >= 0.93   AUTO_MERGE_FLOOR    merge, method 'blocked', no model call
score >= 0.80   ADJUDICATE_FLOOR    one model call on the top candidate
below           not a candidate     its own subject, method 'new'
```

A disagreeing category cannot by itself refuse a merge — "dentist" and "pediatric dentist"
are one person described twice — but it caps the score at 0.92, just under the auto floor,
so a name collision between `Mike the Plumber` and `Mike the Barber` has to go past the
model rather than merging on the name alone.

Measured band membership (`TestBands` pins these, and they are measured rather than
assumed — the first draft of the tests asserted a pair that actually reads 0.69):

| against "Mike the Plumber" | score | band |
|---|---|---|
| `Mike The Plumber`, `… LLC`, `… Inc` | 1.00 | auto |
| `Mike the Plumbr` (typo) | 0.97 | auto |
| `Mike the Plumbers` | 0.90 | adjudicate |
| `Mike Plumber` | 0.86 | adjudicate |
| `Mike Plumbing` | 0.69 | **below — known recall gap** |
| `Chef Ana meal prep` | 0.29 | below |

`Mike Plumbing` is the honest miss: a human would likely call it the same plumber, and it
gets its own subject. That is the conservative failure — two rows where there might be
one — and it is the cheaper of the two mistakes. A real corpus should move the floor, not
intuition.

### Refusal is a first-class outcome

The model answers true / false / **null**, and null is a real answer the prompt asks for
explicitly. False and null do the same thing — neither merges — and both record the
near-miss on `subject_candidate_ref`, so a later pass can settle it without re-running the
search and the model call that found it. Temperature 0: the same pair must not merge on
Tuesday and split on Wednesday.

### Not built: step 4, the confirm-ask

`confirmed` is a valid `subject_method` and nothing writes it yet. Asking the sharer
("Same Dr. Sarah maple recommended?") means adding a question to the capture flow, which
is a UX decision about whether sharing starts to feel like an interrogation — not an
engineering one. The ambiguous rows accumulate with their candidate attached, so whenever
that question does get designed, it has a ready queue and needs no re-computation.

`reco_subject_merges` (from 20261219120000) is also still unwritten: it records
subject→subject consolidation, a repair operation, while everything Stage 2 does is
signal→subject attachment. Left standing rather than dropped, for the consolidation tool.

---

## Stage 3 — the subject-grouped read path

`find_neighbor_tips` gains a subject-grouped sibling (or a v5 — OUT columns change, so a
`drop` + `create`, not `create or replace`, per 42P13).

One row per `subject_ref`:

- `vouch_count` — `count(distinct user_id)`, not `count(*)`: one neighbour posting twice
  about the same place is one voice.
- `contributor_avatars` — the avatar stack on the card.
- `match_strength` — the strongest across members, so the merged card ranks on its best
  evidence.
- `member_signal_ids` — what the evidence list and per-contributor Helpful read from.
- `distance_meters` — **the subject's**, not the recommender's (Open question 1).

[`peer_rows_from_neighbor_tips`](../services/lana-worker/app/tip_rec_cascade.py#L96)
becomes subject rows. The truthfulness rule in that module's header still binds: these
rows are not claim-affinity matches and must never be dressed as them.

---

## Stage 4 — the evidence panel

Screen 08: per-attribute strength, the cohort line ("4 of these 8 have toddlers, like
you"), and the quote carousel.

`reco_attr_tallies` (20261118120000) is the seed — it already groups attributes by
`(block_id, reco_subject)` with counts. It needs to move to `subject_ref`, and to carry
attributed quotes, which today it deliberately refuses to do: it is fenced to aggregates
of ≤24 chars / ≤3 words precisely so *"never a row, an author, a signal id, or a sentence
somebody wrote about their own kid"* escapes. Attaching names to quotes reopens that fence
and needs its own consent story before anything is built.

### The n=1 problem — design this before building it

Screen 08 assumes eight people recommended one dentist. At pilot density the merged card
is n=1 or n=2 nearly every time, and at n=1 it is today's card with the author's name
moved. The merge infrastructure is right regardless; the evidence panel needs deliberate
n=1 and n=2 states rather than letting them fall out of a layout built for n=8.

---

## Open questions

1. **Distance semantics.** Today `distance_text` is how far the *recommender* lives. The
   designs read "Pediatric clinic · 0.2 mi", which is the *place*. Stage 1 grounding
   supplies subject coordinates — but this changes the meaning of a field already on the
   wire, and the browse feed keeps the old meaning.

2. **Circle headers.** Grouping is `circle → block → nearby` based on the *recommender's*
   overlap with the reader. A merged subject with three recommenders from three circles
   has no single header. Screen 07 files Dr. Sarah under ST MARY'S CHURCH at 2 vouched, so
   the rule is probably strongest-provenance-wins (your block > shared circle > nearby) —
   but that is a decision, not a derivation.

3. **The reader's own rec.** `find_neighbor_tips` filters `s.user_id <> v_me`. If the
   reader also recommended Dr. Sarah, the merged card either reads "2 vouched" while
   silently omitting her own voice, or counts her and contradicts the filter.

## Not in scope

- Reviving `set_tip_vouch` or `/lana/tips/vouch` (410). The count is derived.
- Merging the Recent feed.
- Merging `recipe`, `diy` or `other` — those stay one card per author, permanently.
- Canonical-source detection for published recipes.
- Touching `_signal_match_strength`, which the swap matcher shares and which
  20261126120000 deliberately left alone.
