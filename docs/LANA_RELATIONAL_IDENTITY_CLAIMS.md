# Lana · Relational Identity Claims ("my kid goes there", "my dad was in the army")

Status: **design proposal, nothing built.** Needs a product decision (§1) before any schema
lands, because Phase 1 of this deliberately reverses a privacy rule that is currently
hard-coded in the extractor prompt.

Author's short answer to "should this be a graph?": **the graph already exists — don't add a
graph database, add one column.** Details in §3.

---

## 0. What we have today (the ground truth this builds on)

Three separate ledgers, all already live:

| Ledger | Table | What it holds | Implicit subject |
| --- | --- | --- | --- |
| Claims | `user_identity_claims` | one row per identity *thread*: `concept` slug, `label`, `bucket`, `details[]`, `embedding`, `disclosure`, `confidence` | **always the user** |
| Concepts | `identity_concepts` + `claim_concept_links` | shared catalog of concept slugs so two users' wordings collapse to one node ("gymmer" ≡ "Gym enthusiast") | — |
| Circles | `circle_affiliations` → `places` | communities the user attends, `circle_type` ∈ (school, faith, fitness, kids_activity, …), grounded to a `place_ref` | **always the user** |

Matching consumes them in three RPCs:

- `match_peers_by_claim_vectors` — cosine pairs between claim embeddings, truthful pair
  display (`You both: X` / `You: X · Them: Y`), floor 0.70.
- `count_shared_concepts_for_user` — exact `concept_id` overlap.
- `score_onion_candidates_for_user` — composite: `same place_ref +3`, `same circle_type +1`,
  `+1 per shared concept`.

Two facts from the current code that shape everything below:

1. **Kids are deliberately excluded from claims.** `vertex_extract.py:52` — *"NEVER make
   parenting/kids into a claim, and never capture a child's name, age, or school"*, backed by
   `pii.py` which regex-redacts school names and kid names out of every claim label,
   `source_quote`, and synonym before it is written. Only `users.kids_count` (private) and
   `users.role` survive.
2. **…except we already capture the school as a *circle*.** `vertex_extract.py:146` literally
   uses `"my kids are at Lake Nona Middle" → place_name "Lake Nona Middle"` as the worked
   example for a `school` circle. So a kid-linked place already lands in the DB via the circles
   path while the claims path forbids it. **The boundary is already inconsistent** — this doc
   is the chance to draw it on purpose in one direction or the other.
3. **`disclosure='mutual'` is written but never read.** The extractor is instructed to mark
   faith / sobriety / recovery / LGBTQ+ claims `mutual`, and then *every* matcher filters
   `disclosure = 'public'`. Mutual claims are inert today. That dead tier is exactly the
   mechanism relational claims need, so §7 finally implements it — and faith/sobriety claims
   get un-stranded as a side effect.

---

## 1. The product decision that blocks everything (needs a human sign-off)

> Do we store facts whose subject is a **third party** — in particular a **minor** — and use
> them to match adults to each other?

"Both our kids are at the same school" is one of the strongest neighbor signals that exists.
It is also the single most sensitive thing this app could hold: a fact about a child, used to
introduce their parent to a stranger. The whole design hinges on separating two things that
are usually conflated:

- **Scoring** on a minor-linked fact (server-side, never leaves the DB), versus
- **Revealing** it (a string a stranger reads).

Recommended posture, and the rest of the doc assumes it:

**Store the fact. Score on the fact. Never reveal it below a mutual, and never let it be the
sole reason for an introduction.** (§6, §7, §8.)

If product says no to minor-linked facts at all, the design still works for adult relations
(`parent`, `spouse`, `sibling` — "both our dads served") by dropping `child` from the relation
enum. Nothing else changes. So this decision gates *scope*, not *architecture*.

---

## 2. Three orthogonal axes (the conceptual fix)

The mistake to avoid is baking the relation into the concept slug:

```
✗  dads_army_service     ✗  kid_school_lake_nona     ✗  wife_pakistani
```

That fragments the concept catalog — my `dads_army_service` never matches your
`father_military_service`, and the whole point of `identity_concepts` was to collapse wordings
into one node. It also makes every relation a new concept, so the catalog grows O(concepts ×
relations).

Model them as **three independent axes**:

| Axis | Column | Vocabulary | Answers |
| --- | --- | --- | --- |
| **Subject** | `subject_kind` (new) | closed enum: `self`, `child`, `parent`, `spouse`, `sibling`, `grandparent`, `household`, `other` | *who is this about?* |
| **Concept** | `concept` / `concept_id` (exists) | open catalog, subject-neutral: `army_service`, `attends_school`, `plays_soccer` | *what is the fact?* |
| **Bucket** | `bucket` (exists) | heritage, stage, vicinity, faith, activity, interest, general | *what kind of fact?* |

So:

```
"my dad was in the army"      → subject=parent  concept=army_service   bucket=heritage
"I was in the army"           → subject=self    concept=army_service   bucket=heritage
"my kid's at Lake Nona"       → subject=child   concept=attends_school bucket=vicinity  (+ circle w/ place_ref)
"my wife is Pakistani"        → subject=spouse  concept=pakistani_heritage bucket=heritage
```

`army_service` is now **one** catalog node with two users hanging off it via different
relations — which is precisely what makes "both our dads were in the army" a computable
match instead of a string coincidence.

No new bucket for family. Subject is not a kind of fact; it is a different dimension.

---

## 3. Do we need a graph database? No.

The claim store is already a bipartite graph: `(user) -[holds]-> (concept)`. Shared-concept
matching *is* a 2-hop traversal (`user → concept → user`), and `count_shared_concepts_for_user`
is that traversal written as a join.

Adding a subject makes it 3 hops:

```
(user) -[relation: child]-> (person) -[holds]-> (concept) <-[holds]- (person) <-[relation: child]- (user)
```

Bounded depth, known shape, no variable-length path queries, no "friend of a friend of a
friend". Postgres joins are the right tool at this depth; a graph DB (Neo4j, AGE) starts
earning its cost at 4+ hops or unbounded path search, and would cost us the two things we
actually depend on: `pgvector` similarity in the same query as the traversal, and RLS /
security-definer as the disclosure boundary.

What we *should* borrow from graph thinking is discipline, not infrastructure:

- one **closed relation vocabulary** (the `subject_kind` enum), so a future ring layer
  ("parents at your kid's school" as a cohort, friend-of-friend intros) reuses the same edges;
- edges are **first-class and revocable** — deleting a relation cascades its facts (§6.6);
- **never** encode an edge inside a node's name (§2).

If a real multi-hop need shows up later (mutual-friend paths, household clusters), the
migration is `subject_kind` → a `user_relations` table (§9, Phase 4) — still Postgres.

---

## 4. Schema (additive, Phase 1)

### 4.1 Claims get a subject

```sql
-- 202611xx_relational_identity_claims.sql
create type public.claim_subject_kind as enum (
  'self','child','parent','spouse','sibling','grandparent','household','other'
);

alter table public.user_identity_claims
  add column if not exists subject_kind public.claim_subject_kind not null default 'self';

comment on column public.user_identity_claims.subject_kind is
  'Who the claim is about. ''self'' = the user (every pre-existing row). Anything else is a '
  'RELATIONAL claim: the fact belongs to a third party the user is connected to. Never store '
  'that person''s name, age, gender, or photo — the relation IS the identifier.';

-- Threads are per (user, subject, concept): "I served" and "my dad served" are two
-- threads on the same concept and must not merge into one another.
drop index if exists user_identity_claims_user_concept_active_idx;
create unique index user_identity_claims_user_subject_concept_active_idx
  on public.user_identity_claims (user_id, subject_kind, concept)
  where dismissed_at is null;
```

The `default 'self'` is what makes this additive: every existing row, every existing RPC, and
every existing enrichment/merge path keeps working untouched. `details[]` accumulation
([[claim-enrich-not-freeze]]) now enriches the right thread instead of smearing my service
record into my father's.

### 4.2 Circles get the same subject

```sql
alter table public.circle_affiliations
  add column if not exists subject_kind public.claim_subject_kind not null default 'self';
```

This is what makes "our kids are at the same school" fall out of machinery that already
exists: `score_onion_candidates_for_user` scores `same place_ref +3`. A kid's school is a
`circle_type='school'` affiliation with `subject_kind='child'` and a grounded `place_ref` — the
+3 lands for free, and the subject column is what lets the *copy* be honest ("a parent at the
same school" vs "goes to the same school").

### 4.3 Consent, per relation kind

```sql
create table public.user_relation_consent (
  user_id      uuid not null references public.users (id) on delete cascade,
  subject_kind public.claim_subject_kind not null,
  granted_at   timestamptz,
  revoked_at   timestamptz,
  ask_count    int not null default 0,
  primary key (user_id, subject_kind)
);
```

`granted_at is not null and revoked_at is null` is the gate on *matching* (§7). Capture may
proceed without it (the user volunteered the fact in conversation; discarding what they said
is its own kind of rude), but an unconsented relational claim is **scored as zero and never
displayed** — it exists only so Lana can ask about it once and so the user can see it on their
own wall. `child` requires explicit consent, always. Adult relations may default to granted if
product prefers; recommend explicit for `child`, implicit-with-visible-toggle for the rest.

### 4.4 Embedding: strip the subject before embedding

`label` is for humans ("Dad served in the army"), the vector is for clustering. Embed the
**subject-stripped** text ("served in the army", plus `details[]`) so relational and
first-person claims land on the same point in vector space and the pairing rule in §5 —
which reads `subject_kind` explicitly — stays in charge of what counts as a match. Otherwise
the vector quietly does subject matching too, badly.

---

## 5. Matching rules

### 5.1 The pair rule

A shared concept is now a **(subject, concept) pair** on each side. Three cases:

| Case | Example | Weight | Display |
| --- | --- | --- | --- |
| **Same-subject** | both `(parent, army_service)` | full | "You both: dad served in the army" |
| **Cross-subject** | mine `(self, army_service)`, theirs `(parent, army_service)` | half | "You: served · Them: their dad served" |
| **Same place, relational** | both `(child, school@place_ref)` | full + place bonus | "You both have a kid at the same school" (post-mutual only, §7) |

Cross-subject at half weight is a judgment call worth arguing about (§10, Q1). It is a real
affinity — a veteran and someone raised in an army family have something true in common — but
it is weaker, and the copy must never flatten it into "you both". Given the truthful-match
rule already in force ([[truthful-peer-match-model]]), **every relational match label must
name the subject on each side.** "You both: army" over a dad and a veteran is the exact class
of invented affinity that QA already caught once.

### 5.2 Score arithmetic

`score_onion_candidates_for_user.score` is `int` with hardcoded 3/1/1. Scale ×2 to keep
integer math and leave room for the half-weight tier:

| Component | Now | Proposed |
| --- | --- | --- |
| same `place_ref` | +3 | +6 |
| same `circle_type` | +1 | +2 |
| shared concept, same-subject | +1 | +2 each |
| shared concept, cross-subject | — | +1 each |

Components stay in their own returned columns (that was the point of the existing design), so
weights re-tune without a migration.

### 5.3 The co-signal rule (safety, not tuning)

> **A minor-linked fact may never be the only reason a stranger is introduced to you.**

Concretely, in the candidate CTE: if a pair's entire score comes from `subject_kind='child'`
rows, drop it. Require at least one of — a confirmed shared place the *user themselves*
attends, a `self`-subject concept overlap, or an adult-relation overlap. Without this rule the
feature ships a way to find the parents of children at a named school, which is not a product
we are willing to have built.

---

## 6. Privacy rules (non-negotiable set)

1. **No third-party identity, ever.** No name, no DOB or age, no gender, no photo, no grade
   level, no teacher. The relation is the identifier: "your kid", "your dad". `pii.py` stays
   as the deterministic backstop and its `_KIN_NAME` / `_SCHOOL` patterns keep firing on
   labels, `source_quote`, `synonyms`, and `details[]`.
2. **Minor-linked claims are born `mutual`.** Never `public`. The extractor must force
   `disclosure='mutual'` for `subject_kind='child'`, same mechanism already used for
   faith/sobriety.
3. **Coarse pre-mutual, exact post-mutual.** Before a mutual match: "a parent with a kid at a
   school near you." After both sides accept: name the school. This mirrors the §F place
   disclosure tiering that circles already committed to.
4. **Consent is asked once, in Lana's voice, and is revocable** — composed, not canned
   ([[ai-authored-copy-not-canned]]), and modeled on the existing `tip_ask_consent` pattern:
   Lana answers first, then asks. Shape: *"Want me to use that to find other parents at the
   same school? I'd only ever say 'a parent at the same school' — never your kid's name or
   which school, unless you both connect."* One ask, `ask_count` capped, silence = no.
5. **Never in analytics.** Relational claim labels, `details[]`, and school place names must
   not reach Amplitude event properties or session replay. This intersects the still-open
   chat-transcript masking call in [[session-replay-wiring]] — resolve that before Phase 2
   ships, or minor-linked strings will land in a third-party tool.
6. **Retraction is a cascade, and it is a delete.** "Don't use my kid's school" removes the
   relational rows outright rather than setting `dismissed_at` (dismissed rows are retained
   everywhere else; for minor-linked data retention is the liability). One command, all
   subjects of that kind.
7. **Adult third parties are third parties too.** "My dad was in the army" is his fact, not
   the user's. Lower risk, same rules on names; coarse concept + `details[]` only.
8. **The rapport tile may ask relational follow-ups, but never identifying ones.** "Which
   school?" is allowed once consent is granted (it grounds a `place_ref`). "What's their
   name?", "How old?", "What grade?" are permanently forbidden — add them to the extractor's
   FORBIDDEN follow-up list alongside the existing opinion/origin-story ban.

---

## 7. Implementing the `mutual` tier (the real engineering work)

Relational matching is impossible to ship honestly without splitting two things every current
matcher conflates:

```
score_visible  →  may this claim contribute to ranking?      (mutual: YES)
label_visible  →  may this claim's text be shown to a peer?  (mutual: only post-mutual-accept)
```

Today all six matchers hardcode `disclosure = 'public'` for both. Proposed change, applied
uniformly:

- scoring CTEs: `disclosure in ('public','mutual')`
- display/label CTEs: `disclosure = 'public'`, **or** `'mutual'` when a mutual connection
  exists between the two users (there is already a mutual-connection notion in
  `get_peer_profile_mutual` / `get_cluster_peers_mutual` from the phase3b migrations — reuse
  it rather than inventing a second definition).
- when the best-scoring pair is undisplayable, fall back to the best *displayable* pair for
  the card copy, and if there is none, show the honest generic ("You have something in
  common — connect to see") rather than a fabricated reason.

Payoff beyond this feature: faith, sobriety, recovery, and LGBTQ+ claims — currently written
`mutual` and then ignored by every matcher — start counting for the first time.

---

## 8. Extraction changes

`vertex_extract.py` needs a subject field and a rewritten kids rule. Per
[[no-new-regex-use-ai-signals]], the model picks the subject from the closed enum; no kinship
regex in the routing path (`pii.py`'s regex stays, as redaction only).

Prompt deltas:

- Add to the claim JSON shape: `"subject": "self" | "child" | "parent" | "spouse" | "sibling"
  | "grandparent" | "household" | "other"`. Default `self`; emit a non-self subject **only**
  when the user's own words attribute the fact to that person.
- **Replace** `NEVER make parenting/kids into a claim` with: *the user's own parenting stays
  off the wall (`kids_count` and `role` carry it, both private); a fact about a child —
  a school, a sport, an activity — is a `subject: "child"` claim with `disclosure: "mutual"`,
  and never contains a name, age, gender, or grade.*
- Keep `kids_count` and `role` exactly as they are. They are private profile fields and
  emphatically not claims.
- Circle candidates gain the same `subject` field, so the existing "my kids are at Lake Nona
  Middle" example finally carries *whose* school it is.
- Worked examples to add: `"both my parents are Pakistani"` → `(parent, pakistani_heritage)`;
  `"my kid plays soccer at the Y"` → claim `(child, plays_soccer)` + circle `(child, fitness,
  place_name "the Y")`; `"my dad was in the army"` → `(parent, army_service)`; `"my wife
  teaches"` → `(spouse, teacher)`.
- Negative examples: `"looking for a soccer league for my kid"` → a **search**, not a claim
  (the existing search-exclusion rule extends to relational subjects); `"my daughter Sara"` →
  `(child, …)` with the name dropped.

Write path (`claims_persist.py`): thread key becomes `(subject_kind, concept)` everywhere it
is currently `concept` — `fetch_active_claim_threads`, `dedupe_claims`, `_merge_into_existing`,
`resolve_cross_concept_match`, and the heritage reconciliation. Heritage deserves a specific
look: `reconcile_heritage_claims` assumes one heritage per user, and "my wife is Pakistani, I'm
Egyptian" must produce two threads, not a conflict prompt.

---

## 9. Phasing

| Phase | Contents | Gate |
| --- | --- | --- |
| **0** | Product decision (§1) + consent copy + answers to §10 | **blocks everything** |
| **1** | Migration §4 (additive, `default 'self'`), extractor emits `subject`, write path threads on `(subject, concept)`. Nothing reads it. Flag `LANA_RELATIONAL_CLAIMS`, fail-open. | Phase 0 |
| **2** | `mutual` tier end-to-end (§7). Ships value on its own (faith/sobriety claims start counting). | Phase 1 |
| **3** | Matching: pair rule, rescaled onion weights, co-signal rule, subject-bearing truthful copy. | Phase 2 + analytics masking (§6.5) |
| **4** | `user_relations` table (per-person rows, aliases, no names) — **only if** multi-kid disambiguation proves necessary in practice. | evidence |

Phase 4 exists because `subject_kind` alone cannot tell you whether "same school + plays
soccer" is one kid or two. My read is that this does not matter for matching quality (both
facts are true of the household either way) and it is not worth a table until it does. Deploy
ordering follows the house rule: **migration before worker**, both before any FE change.

Rollback: Phase 1–3 are all flag-gated reads over a defaulted column; `LANA_RELATIONAL_CLAIMS=0`
returns exact current behavior with the column inert.

---

## 10. Open questions

1. **Cross-subject matching — in or out?** (my service ↔ your dad's service, half weight.) In,
   at half weight, with subject-explicit copy, is my recommendation; "out" is defensible and
   simpler.
2. **Retraction: hard-delete or `dismissed_at`?** I recommend hard-delete for `child`,
   `dismissed_at` for adult relations — inconsistent on purpose.
3. **Does a kid's school circle count toward ZIP-unlock supply / discovery gating (§D.2)?** It
   is real local supply, but counting it means a minor-linked row influences whether the
   market opens.
4. **Consent granularity:** per `subject_kind` (as drafted) or one blanket "family facts"
   toggle? Per-kind is more honest and more UI.
5. **School granularity:** store the exact `place_ref` and reveal only the cohort pre-mutual
   (drafted), or never store the exact school and match on cohort only ("elementary-age kid in
   your ZIP")? The second is strictly safer and materially weaker.
6. **Do relational claims appear on the user's own identity wall?** They are facts about
   someone else; showing them is transparency (the user can see and remove what we hold),
   hiding them is discretion. I lean show, in a visually distinct "family" group, with the
   consent toggle attached.
7. **Deceased relatives / past-tense facts.** "My dad *was* in the army" — the `transient`
   flag is about temporary states, not past ones, and neither is quite right. Probably fine to
   ignore; noting it so it isn't discovered as a bug later.

---

## 11. Files this touches

- `supabase/migrations/202611xx_relational_identity_claims.sql` (new — §4)
- `services/lana-worker/app/vertex_extract.py` — subject field, kids rule rewrite (§8)
- `services/lana-worker/app/models.py` — `ExtractedClaim.subject_kind`
- `services/lana-worker/app/claims_persist.py` — thread key `(subject, concept)`, heritage
  reconciliation, consent gate on write
- `services/lana-worker/app/pii.py` — unchanged, but now the last line of defense rather than
  the second; worth a test pass against relational labels
- `services/lana-worker/app/circles_capture.py` — subject on circle candidates
- `services/lana-worker/app/{onion,onion_blend,peer_discovery_surface}.py` — pair rule,
  weights, truthful subject-bearing copy
- `services/lana-worker/app/rapport_gap_tree.py` / `identity_ask.py` — relational follow-ups,
  forbidden-ask list
- `services/lana-worker/app/analytics.py` — assert relational labels never enter event props
- migrations rewriting the `disclosure = 'public'` filter: `20260820120000_truthful_peer_match`,
  `20260823120000_rank_by_shared_count`, `20260827110000_find_peers_semantic`,
  `20260913120000_count_shared_concepts_for_user`, `20260914120000_score_onion_candidates`
