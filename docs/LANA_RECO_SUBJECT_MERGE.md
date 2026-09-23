# Recommendation subjects — merging recs about the same thing

Status: **Stages 1-3 and the 80/20 clustering built** (2026-09-22/23), behind `LANA_RECO_CARDS` (off). Dev has 20261219+20261220; prod has 20261219. 20261221 and 20261222 are unpushed. Stage 4 (the evidence panel) is still plan.

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

## Which types merge, and how

**Everything merges.** What differs is how a subject's contributions may be COMBINED —
settled at standup 2026-09-22, where Tommaso answered the "Dr. Sara is a great doctor and
her parking space is very big" problem with the Pareto shape:

> *"80% have more or less the same profile… then 20% is the minority… aggregate those that
> have 80% similarity in what was expressed, and for those who are uniquely positioned,
> provide standalone."*

That is the App Store reviews shape, and it is what stops a card blending *"great doctor"*
with *"huge parking lot"* into a sentence nobody wrote. The majority cluster is
summarised; the outliers keep their own voice.

So `reco_subjects.merge_mode` records which kind of subject it is:

| mode | contributions are | the card may | types |
|---|---|---|---|
| `aggregate` | observations ABOUT one shared thing | summarise the majority, surface outliers separately | `professional` `restaurant` `location` `service` `product` |
| `collection` | the artifact ITSELF, one per author | list them side by side — **never blend** | `recipe` `diy` `other` |

A recipe is simply always on the minority side. "Banana bread" is a perfectly good subject
that three neighbours can point at; what must never happen is merging their `ingredients`
and `steps` into one hybrid, which would discard two authors' work and credit the result
to all three. **The subject merges; the artifacts do not.**

The mode lives in the database rather than in a comment because a renderer that blends a
collection reproduces exactly the bug this design exists to avoid — and it is settled by
the creating author from the TYPE, never revised, since a subject cannot be observations
for one neighbour and artifacts for the next.

`reco_subject_candidates` filters on it too, so a recipe is never a candidate for a
business that happens to share a word. (The blocker is recall-biased by design, so
"Bread Street Plumbing" does surface against "Banana bread" on the shared word — the
SCORER refuses it at 0.303, far below the 0.80 floor. Pinned in `TestIdentitySpace`.)

**Earlier position, now superseded:** Stages 1 and 2 excluded `recipe`/`diy`/`other` from
merging entirely, reasoning that their fields are the artifact. That was right about the
FIELDS and wrong about the SUBJECT — 20261221120000 corrects it.

## The 80/20 clustering  ✅ BUILT

`app/reco_cluster.py`, cached in `reco_subject_digests` (20261222120000).

It does not produce a summary — that is what makes "great doctor · huge parking lot"
impossible. It produces **themes**, each carrying how many contributors expressed it and
one real quote. An outlier is a theme with `n=1`, so nothing is discarded to avoid the
blend: the card renders big themes big and small themes small. `n` of `total` is exactly
screen 08's "Gentle with anxious kids **8**/10".

Three rails, each enforced in code rather than asked for in the prompt:

- **Quotes are verified verbatim.** The prompt says to copy a contributor's words; nothing
  about a prompt makes that true. Every quote is checked against the contributions the
  theme claims to come from, and dropped when it is not there — a theme can keep a true
  count while losing an invented quote. A quote under a neighbour's name is the strongest
  claim the card makes.
- **Invented ids are dropped, and a theme left with none goes with it** — a count is the
  whole claim a theme makes.
- **Collections are never clustered**, and neither is a single contribution.

**Cost shape:** a results list renders from cache only and never blocks on a model call.
A miss schedules a bounded (`_MAX_WARM = 3`) daemon-thread warm, so the next read has its
themes — the same fire-and-forget pattern `signal_match_notify` uses. Temperature 0 and a
`basis_sig` over contribution ids *and* text, so a card cannot regroup between two reads,
and an author editing their own recommendation authors a new digest.

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

## Stage 3 — the subject-grouped read path  ✅ BUILT

`find_neighbor_tips` v8 (20261222120000) + `app/reco_cards.py`, wired in
`tip_rec_cascade.stamp_tip_peer_surface` behind **`LANA_RECO_CARDS` (off by default)**.

**Additive, never a swap.** `peer_matches` keeps its exact shape and the live client keeps
working; `reco_cards` rides alongside for a client that knows about it. A flag that
replaces one store with another has no safe half-way state — that is how PR #96 broke
rapport ([[identity-concepts-pr96]]).

**Grouping happens in Python, not a second RPC.** The visibility predicate in
`find_neighbor_tips` is long and load-bearing (blocks, community-vs-area scope, expiry,
the type chips, the strength gate). A grouping RPC would restate it, and a second copy is
how the rules in this repo rot. So rows stay row-shaped and one definition of who may see
what survives.

**The count is not the window.** `subject_vouch_count` is computed in SQL across every
visible live contribution, so a `LIMIT 1` page still reports 3. Grouping a paged result in
Python would have made "3 vouched" silently become "2 vouched" on page two — a count a
stranger is meant to trust cannot be an artefact of pagination.

### The three open questions, answered

| question | answer |
|---|---|
| Distance | the **subject's** (`subject_distance_text`), computed from its grounded coordinates; falls back to the lead contributor's when ungrounded, flagged by `distance_is_subject` so copy never implies otherwise |
| Circle header | **best provenance across contributors**, not the first row's — a subject recommended from a shared circle *and* from nearby files under the circle |
| Your own rec | **counted, never listed**. The row list still excludes you; `i_contributed` lets the copy read "you and 2 neighbours" instead of crediting your voice to strangers |

### Ranking

Lexicographic on facts that are never multiplied together: provenance, then **max**
match strength, then vouch count, then title. Max and not mean, so extra voices can never
dilute a subject's rank — averaging would put a dentist with one strong and four weak
recommendations below one with a single strong one. Title last makes the order total, so a
float tie cannot let two cards swap places between reads.

**Recommender affinity is not wired** — it lives in Pouya's module, which is not on main.
The seam is ready: aggregate it as `max` across contributors, for the same reason, and
attribute it to the contributor rather than to the subject. Dr. Sarah is not Turkish
because a Turkish neighbour recommended her.

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

## Measured, on a real stack (2026-09-23)

`scripts/eval_reco_resolution.py --trials 5` · 11 cases / 55 pairs · real Google, real
model calls, local Supabase. Ground truth by construction.

```
5 trials · precision 100% every trial · 0 false merges across all trials
recall     60% / 100% / 100% / 60% / 60%
UNSTABLE   i1+i4 merged 2/5   ("Mike Plumber" vs "Mike the Plumber", both at 0.86)
```

**The error that matters never happened.** No false merge in 5 trials — nothing invented
corroboration. Google grounding, auto-merge at ≥0.93 and every separation behaved
identically every time, including three different dentists kept apart (two sharing the word
"Pediatric") and an identical name with a different trade refused via the category cap.

### The adjudicated band is not deterministic

One borderline pair merged 2 times in 5 on identical input at an identical score.
**`temperature=0` is not a determinism guarantee** — an earlier note in this doc claimed a
card cannot regroup between reads, and that was wrong. The consequence is real: whether two
neighbours' recommendations merge is decided once at capture and persisted, so the outcome
is sticky per tip but arbitrary across tips.

The fix is the pattern already used for digests: cache the verdict by the compared PAIR
(`subject_key` + category on both sides), so one comparison resolves one way forever. Not
built.

### Three bugs the real runs found that mocks could not

**`ambiguous` was overwritten by `new`.** The refusal was recorded, then `set_signal_subject`
stamped `method='new'` and nulled the candidate microseconds later. Every unit test passed
because none ran both calls in sequence against a database. Fixed by recording after the
create; pinned by a test asserting call ORDER, not call presence.

**Grounding ignored `place_based`.** `subject_is_place()` already decides whether a subject
has a storefront — "a barber shop is, a plumber is not" — and this module searched Google
anyway. A search always finds *something*, so "Mike Plumber" was attached to a real plumbing
company the neighbour never named, and could then never merge with "Mike the Plumber", who
had no listing. Fixed: a *pick* still grounds regardless (user action outranks a classifier),
but a *search* defers to the flow's verdict.

**The harness leaked subjects between runs**, so a later run scored against an earlier run's
rows and turned a correct separation into a merge at 1.00 against a ghost. Fixed with a
scoped pre-flight reset.

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
- Canonical-source detection for published recipes.
- Touching `_signal_match_strength`, which the swap matcher shares and which
  20261126120000 deliberately left alone.
