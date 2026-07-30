# PR11 · Embedding backfill — `capability_index` + `identity_concepts`

**Target repo:** `Phygtl-Inc/TagAlng-Backend`
**Environment:** Supabase `kmetmatfxdkrialwrnzj` (**tagalng-prod**)
**Author:** data-layer audit · 2026-07-30
**Status:** specification — nothing applied, nothing committed, no PR opened

---

## 0. Environment note (read this first)

`_CODE_TRUTH_2026-07-30.md` states it verified against "live Supabase `rjlcyvwogmfmngemhbmn`".
That project is **`tagalng-dev`**, not prod:

| ref | name | created |
|---|---|---|
| `kmetmatfxdkrialwrnzj` | **tagalng-prod** | 2026-06-29 |
| `rjlcyvwogmfmngemhbmn` | tagalng-dev | 2026-05-25 |

Every volume figure in the CODE TRUTH doc (1,254 users · 979 claims · 5,692 `suggestion_queue`)
is a **dev** figure. Prod is a much younger, much smaller database. This distinction is what makes
the root cause legible — see §1.2.

---

## 1. FIX 1 · `capability_index.embedding` is NULL on all 8 rows

### 1.1 The RPC is correct — it fails closed, silently

`public.match_latent_capabilities` (from `pg_get_functiondef`, verbatim):

```sql
CREATE OR REPLACE FUNCTION public.match_latent_capabilities(
  p_query_embedding vector, p_limit integer DEFAULT 3, p_min_score real DEFAULT 0.65)
 RETURNS TABLE(capability_id text, capability_name text, similarity real,
               surface_priority integer, entity_triggers text[],
               identity_claim_triggers text[], required_state text[])
 LANGUAGE plpgsql STABLE SECURITY DEFINER
AS $function$
declare
  v_limit int := greatest(1, least(coalesce(p_limit, 3), 10));
begin
  return query
  select
    ci.capability_id,
    ci.capability_name,
    (1 - (ci.embedding <=> p_query_embedding))::real as similarity,
    ci.surface_priority,
    ci.entity_triggers,
    ci.identity_claim_triggers,
    ci.required_state
  from public.capability_index ci
  where ci.is_active
    and ci.embedding is not null                                   -- ← the guard
    and (1 - (ci.embedding <=> p_query_embedding)) >= coalesce(p_min_score, 0.65)
  order by ci.embedding <=> p_query_embedding
  limit v_limit;
end;
$function$
```

**Answer to "what does it return when embeddings are NULL":** the explicit
`and ci.embedding is not null` predicate removes every row **before** the `<=>` operator is
evaluated. There is no NULL-distance arithmetic and no error. The function returns an
**empty result set** — a clean, indistinguishable-from-"no match" zero.

This is why the failure is invisible: no exception, no log line, no metric. The caller
(`latent_extract._queue_capability_matches`) does:

```python
matches = resp.data or []
if not matches:
    return 0
```

…and quietly queues nothing. Semantic capability routing has never fired in prod.

### 1.2 Root cause: a required post-deploy step was never run against prod

The seeding migration deliberately leaves embeddings NULL. From
`services/lana-worker/scripts/backfill_capability_embeddings.py` (module docstring, verbatim):

> The Layer 3 migration seeds capability_index rows with embedding=NULL (pure SQL can't
> call the embedding model). Run this once after `supabase db push` so the capability
> matcher (match_latent_capabilities) has vectors to compare against.

`supabase/migrations/20260728120000_lana_latent_intent.sql` says the same in its header:

> `* embedding columns nullable + partial index -> seed rows / firehose rows insert first, embed in background.`

**The script was run on dev and never on prod.** Verified side by side:

| metric | tagalng-dev | tagalng-prod |
|---|---|---|
| `capability_index` rows | 8 | 8 |
| …with embedding | **8** | **0** |
| `latent_signals` | 3,257 | 108 |
| …with embedding | — | **108 (100%)** |
| `suggestion_queue` | **5,692** | **0** |

The prod pipeline upstream of the matcher is **fully alive**: 108 entities extracted, embedded and
persisted. `LANA_LATENT_EXTRACT` is evidently enabled in prod (its default in
`app/lana_paths.py` is `"0"`, but 108 signals exist, so it is on). Everything works right up to
the matcher, which returns nothing because there is nothing to match against.

### 1.3 Proof the matcher works once embeddings exist

Executed against PROD inside `begin; … rollback;`:

```sql
begin;
with v as (select embedding from public.user_identity_claims where embedding is not null limit 1)
update public.capability_index ci set embedding = v.embedding from v
where ci.capability_id='looking.tip';

select capability_id, round(similarity::numeric,4) as similarity, surface_priority, required_state
from public.match_latent_capabilities(
  (select embedding from public.user_identity_claims where embedding is not null limit 1), 3, 0.45);
rollback;
```

Result:

| capability_id | similarity | surface_priority | required_state |
|---|---|---|---|
| `looking.tip` | **1.0000** | 6 | `{}` |

The function is correct. **Only the data is missing.** No SQL change is needed for FIX 1.

### 1.4 The 8 rows, in full

| capability_id | capability_name | description | entity_triggers | identity_claim_triggers | required_state | surface_priority |
|---|---|---|---|---|---|---|
| `sharing.swap` | Offer gear/clothes to swap | Offer kids gear, clothes, or equipment for a neighbor to take or swap | gear, clothes, outgrew, item, toys, donate | has_kid | `{}` | 5 |
| `sharing.tip` | Share a recommendation | Share a tried-and-true tip or recommendation with neighbors | recommendation, tip, review, place, service | — | `{}` | 5 |
| `discovery.find_activities` | Find local activities | Find events, classes, or activities happening nearby | activity, event, class, festival, camp | — | `{}` | 6 |
| `looking.tip` | Find a neighbor-tested recommendation | Find a neighbor-tested recommendation for a service, professional, or place | recommendation, dentist, doctor, pediatrician, tutor, gym, restaurant, service | — | `{}` | 6 |
| `sharing.host` | Host a meet or playgroup | Host or organize a meet, playdate, or activity for nearby families | activity, playgroup, event, meetup, host | has_kid | `{}` | 6 |
| `discovery.find_peers` | Find similar neighbors | Find nearby people with a similar life stage, kids, or interests | neighbor, mom, parent, friend | — | `{zip_open}` | 7 |
| `looking.swap` | Find a gear/clothes swap | Find someone nearby willing to swap kids gear, clothes, or equipment | gear, equipment, clothes, item, outgrew, size, stroller, toys | has_kid | `{}` | 7 |
| `looking.meet` | Find a meet or playgroup | Find a meet, playgroup, or activity group with people at a similar life stage | activity, sport, hobby, class, lesson, playgroup | has_kid | `{zip_open}` | 8 |

All 8 are `is_active = true`. Lingo is already scrubbed (no "block" wording).

### 1.5 The fix — run the existing script

**Do not write a new script.** It already exists and is correct:

**Location:** `services/lana-worker/scripts/backfill_capability_embeddings.py`
**Function to reuse:** `_vertex_embed(text, dim=768)` — defined inline in that script.

Note the deliberate design decision in its docstring (verbatim):

> Deliberately does NOT import app.vertex_extract: that module pulls in the orchestrator
> package, which has a circular import that only resolves when app.main loads the graph
> first. As a standalone script we call google.genai directly to sidestep it.

So `_vertex_embed` calls `google.genai` directly:

```python
client = genai.Client(vertexai=True, project=project, location=location)
result = client.models.embed_content(model=model, contents=text)
values = list(result.embeddings[0].values)
if len(values) != dim:
    raise ValueError(f"expected_{dim}_dims_got_{len(values)}")
```

`model` = `os.environ["VERTEX_EMBED_MODEL"]` defaulting to `text-embedding-005` — matches the
`vector(768)` column. (The canonical worker-side helper is `app.vertex_extract.vertex_embed`;
the script's copy exists only to dodge the circular import above.)

**What gets embedded** — `_embedding_text(row)`:

```python
parts = [row.get("capability_name") or "", row.get("description") or ""]
triggers = row.get("entity_triggers") or []
if triggers:
    parts.append(", ".join(triggers))
return " — ".join(p for p in parts if p)
```

i.e. `name — description — trigger, trigger, …`. `identity_claim_triggers` and `required_state`
are **not** embedded (they are post-filters, not semantics). Keep it that way — dev was
embedded with this exact text and dev works.

**Run procedure:**

```bash
cd services/lana-worker
# load the PROD worker env (SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must point at
# kmetmatfxdkrialwrnzj; GCP_VERTEX_PROJECT/LOCATION/VERTEX_EMBED_MODEL as in
# deploy/lana-worker-prod.env)
set -a; source ../../deploy/lana-worker-prod.env; set +a
python -m scripts.backfill_capability_embeddings
```

- **Batching:** not required. 8 rows → 8 Vertex `embed_content` calls, one row per `UPDATE`.
  Well inside any quota. Do not add concurrency.
- **Idempotency:** default mode selects `.is_("embedding", "null")`, so re-running is a no-op
  once complete ("Nothing to backfill…", exit 0). `--all` force-re-embeds; only use it if the
  embedding model or `_embedding_text` changes.
- **Failure mode:** per-row `try/except` prints `FAILED <cap_id>` to stderr and continues;
  process exits `1` if any row failed. Safe to re-run — it will pick up only the still-NULL rows.
- **Ordering:** run this **before** enabling any surfacing of `suggestion_queue`.

### 1.6 Also required: the deploy flag for FIX 2

`IDENTITY_CONCEPT_LINK_ENABLED` appears **only** in application code, tests, and one migration
comment. It is present in **no** deploy artifact — not `deploy/lana-worker.env.example`, not
`deploy/lana-worker-prod.env`, not `scripts/deploy-lana-worker.sh`. See §2.3.

---

## 2. FIX 2 · `identity_concepts` embeddings NULL + `claim_concept_links` at 6% coverage

### 2.1 Observed prod state

```
identity_concepts        3 rows   ·  3 with canonical_embedding NULL (100%)
claim_concept_links      3 rows
user_identity_claims    48 rows   ·  48 with embedding NOT NULL (100%)
→ 45 of 48 claims (94%) unlinked
```

The three concept rows:

| concept | label | bucket | synonyms | canonical_example_quote | canonical_embedding | created_at |
|---|---|---|---|---|---|---|
| `faith_community` | faith | `general` | `{}` | NULL | **NULL** | 2026-06-29 18:48:19.144262+00 |
| `heritage_brazilian` | Paulista | `general` | `{brazilian,latina}` | NULL | **NULL** | 2026-06-29 18:48:19.144262+00 |
| `parents_toddlers` | 14-month-old | `general` | `{mom,toddler}` | NULL | **NULL** | 2026-06-29 18:48:19.144262+00 |

**All three timestamps are identical to the microsecond**, and the three linked claims carry the
*same* timestamp and `bucket = NULL`. These are bootstrap fixtures, not organic data.

Claim creation timeline vs. linkage:

| day | claims created | of which linked |
|---|---|---|
| 2026-06-29 | 3 | **3** |
| 2026-07-27 | 2 | 0 |
| 2026-07-28 | 26 | 0 |
| 2026-07-29 | 7 | 0 |
| 2026-07-30 | 10 | 0 |

Every organic claim since prod opened for traffic is unlinked.

### 2.2 Root cause (a) — the backfill migration ran against an empty database

`20260905120001_identity_concepts_backfill` **is applied** to prod (confirmed in
`supabase_migrations.schema_migrations`, alongside `20260905120000_identity_concepts_schema`).

It ran at **prod bootstrap on 2026-06-29**, when `user_identity_claims` contained only the 3
fixture rows. It faithfully produced 3 concepts and 3 links, then never ran again. The 45
real claims arrived a month later, on 2026-07-27 → 07-30.

This is not a broken write path. It is a **one-shot migration that fired too early**.

### 2.3 Root cause (b) — the worker's linking step is switched off

`services/lana-worker/app/claims_persist.py`, module docstring (verbatim):

> Legacy table user_identity_claims is written ALWAYS, unconditionally.
> The flag IDENTITY_CONCEPT_LINK_ENABLED gates only an ADDITIVE step that resolves
> the claim to a shared identity_concepts row and records a link in claim_concept_links.

```python
def _identity_concept_link_enabled() -> bool:
    """Gates ONLY the additive concept-resolution + link step; never gates legacy reads/writes."""
    import os
    return os.environ.get("IDENTITY_CONCEPT_LINK_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
```

The default is the **empty string** → falsy → the step is skipped. The call site:

```python
if _identity_concept_link_enabled() and claim_id:
    _link_claim_to_concept(sb, claim_id, c, embedding)
```

A repo-wide search for `IDENTITY_CONCEPT_LINK_ENABLED` returns exactly three hits:
`app/claims_persist.py`, `tests/test_claims_persist_links.py`, and a comment inside
`20260913120000_count_shared_concepts_for_user.sql`. **It is set nowhere in deploy config.**

That migration's own header already predicted this outcome (verbatim):

> NOTE: This function assumes IDENTITY_CONCEPT_LINK_ENABLED (lana-worker env flag) is on
> and that the concept backfill migration 20260905120001_identity_concepts_backfill.sql
> has run. Peers without concept links will undercount shared concepts until backfill
> completes. This is a known PR caveat recorded in-code.

**So: not a threshold problem, and not a broken write path — a disabled feature flag plus a
one-shot migration that ran a month too early.** Both halves must be fixed.

### 2.4 Root cause (c) — the migration is not embedding-idempotent

The backfill copies the embedding **at INSERT time only**:

```sql
insert into public.identity_concepts (
  concept, label, bucket, synonyms, canonical_example_quote, canonical_embedding, created_at, updated_at)
select seed.concept, seed.label, coalesce(seed.bucket, 'general'), agg.synonyms,
       seed.source_quote, seed.embedding, seed.created_at, now()
from ( select distinct on (concept) concept, label, bucket, source_quote, embedding, created_at
       from public.user_identity_claims order by concept, created_at asc ) seed
join ( … ) agg on agg.concept = seed.concept
on conflict (concept) do nothing;          -- ← never repairs an existing row
```

At 2026-06-29 18:48:19 the 3 source claims had `embedding IS NULL`, so `canonical_embedding`
was written NULL. Those claims were embedded **afterwards** by the worker's self-heal path
(`claims_persist.kick_claim_embedding_backfill`, described in `discovery_route.py` as
"claims saved without embeddings (best-effort write-time embed) get re-embedded in the
background"). Nothing propagates that repair back into `identity_concepts`.

Because of `on conflict (concept) do nothing`, **simply re-running the migration will not fix the
three existing rows.** An explicit `UPDATE` is required. This is a latent bug in the migration
that will recur on any future environment bootstrap.

### 2.5 Consequence for the matchers

`match_concepts_by_embedding` has the identical guard to the capability matcher:

```sql
  from public.identity_concepts ic
  where ic.bucket = p_bucket
    and ic.canonical_embedding is not null
    and (1 - (ic.canonical_embedding <=> p_embedding)) >= p_min_similarity
```

With 3/3 NULL it returns empty for every bucket — so even with the flag on, the worker's
resolve path (`_resolve_concept_id` → `match_concepts_by_embedding` → LLM verifier →
`get_or_create_concept`) would fall straight through to creating a fresh concept every time
instead of reusing one. **Enabling the flag without first backfilling embeddings would
fragment the concept catalog.** Order matters: backfill first, then flip the flag.

`score_onion_candidates_for_user` scores `circle_bonus + shared_concept_count`, where the
concept half joins `user_identity_claims → claim_concept_links`. At 3/48 linked, and with those
3 links all belonging to fixture rows, `shared_concept_count` is effectively always 0. The
function currently reduces to its circle half alone (`place +3 / type +1`), over just 4
confirmed circle affiliations. `count_shared_concepts_for_user` is dead for the same reason.

**Separately (not caused by the NULLs):** the 3 fixture concepts all sit in `bucket = 'general'`
because their source claims had `bucket = NULL` and the migration applies
`coalesce(seed.bucket, 'general')`. Their slugs say otherwise — `heritage_brazilian` belongs in
`heritage`, `parents_toddlers` in `stage`, `faith_community` in `faith`. Since
`match_concepts_by_embedding` filters `ic.bucket = p_bucket`, these rows are **unreachable**
for heritage/stage/faith lookups no matter what. Compare dev's healthy distribution:
activity 217 · interest 110 · stage 62 · vicinity 44 · heritage 32 · general 24 · faith 4.
Prod claim buckets in use: interest 17 · activity 9 · general 6 · stage 5 · heritage 4 ·
NULL 3 · vicinity 3 · faith 1.

### 2.6 The fix — pure SQL is sufficient here

Unlike `capability_index`, **no worker script is needed**: `user_identity_claims.embedding` is
already 100% populated (48/48), so the vectors can be copied within the database.

```sql
-- ============================================================================
-- PR11 · identity_concepts + claim_concept_links backfill (tagalng-prod)
-- Idempotent. Non-destructive. Run inside a transaction.
-- ============================================================================
begin;

-- Snapshot for rollback (see §4).
create table if not exists public._pr11_concepts_before as
  select id, canonical_embedding is null as was_null from public.identity_concepts;
create table if not exists public._pr11_links_before as
  select claim_id from public.claim_concept_links;

-- Step A · repair canonical_embedding on rows the migration inserted with NULL.
--          (`on conflict do nothing` never did this — see §2.4.)
update public.identity_concepts ic
set canonical_embedding    = seed.embedding,
    canonical_example_quote = coalesce(ic.canonical_example_quote, seed.source_quote)
from (
  select distinct on (concept) concept, embedding, source_quote
  from public.user_identity_claims
  where embedding is not null
  order by concept, created_at asc
) seed
where seed.concept = ic.concept
  and ic.canonical_embedding is null;

-- Step B · insert concepts for claims that have none yet.
insert into public.identity_concepts (
  concept, label, bucket, synonyms, canonical_example_quote,
  canonical_embedding, created_at, updated_at)
select seed.concept, seed.label, coalesce(seed.bucket, 'general'),
       coalesce(agg.synonyms, '{}'::text[]), seed.source_quote,
       seed.embedding, seed.created_at, now()
from (
  select distinct on (concept) concept, label, bucket, source_quote, embedding, created_at
  from public.user_identity_claims
  order by concept, created_at asc
) seed
left join (
  select concept,
         coalesce((select array_agg(distinct s)
                   from (select unnest(synonyms) as s
                         from public.user_identity_claims u2
                         where u2.concept = uic.concept) x)[1:20], '{}'::text[]) as synonyms
  from public.user_identity_claims uic
  group by concept
) agg on agg.concept = seed.concept
on conflict (concept) do nothing;

-- Step C · link every claim to its concept.
insert into public.claim_concept_links (claim_id, concept_id, created_at)
select uic.id, ic.id, uic.created_at
from public.user_identity_claims uic
join public.identity_concepts ic on ic.concept = uic.concept
on conflict (claim_id) do nothing;

commit;
```

**Verified against PROD inside `begin; … rollback;`** — result:

| metric | before | after backfill |
|---|---|---|
| `identity_concepts` | 3 | **45** |
| …with `canonical_embedding` NULL | 3 | **0** |
| `claim_concept_links` | 3 | **48** |
| unlinked claims | 45 | **0** |

Idempotency: Step A's `where ic.canonical_embedding is null` is self-limiting; Steps B and C are
`on conflict … do nothing`. A second run changes nothing.

**Optional — repair the 3 mis-bucketed fixture rows** (§2.5). Only do this deliberately; it is
a semantic change, not a mechanical one:

```sql
update public.identity_concepts set bucket = 'heritage' where concept = 'heritage_brazilian' and bucket = 'general';
update public.identity_concepts set bucket = 'stage'    where concept = 'parents_toddlers'   and bucket = 'general';
update public.identity_concepts set bucket = 'faith'    where concept = 'faith_community'    and bucket = 'general';
```

`identity_concepts_bucket_check` permits exactly
`heritage|stage|vicinity|faith|activity|interest|general`, so all three values are legal.

### 2.7 Then, and only then, enable the flag

Add to `deploy/lana-worker-prod.env` (and `deploy/lana-worker.env.example` for discoverability),
and plumb through `scripts/deploy-lana-worker.sh` alongside the other `--set-env-vars` entries:

```
IDENTITY_CONCEPT_LINK_ENABLED=1
# Optional tuning — current code defaults:
# LANA_CONCEPT_TOP_K=5
# LANA_CONCEPT_MIN_SIM=0.75
```

**Sequence is load-bearing:** backfill embeddings → deploy the flag. Reversed, the resolver sees an
un-embedded catalog, `match_concepts_by_embedding` returns empty, and every new claim mints a
duplicate concept (§2.5).

`LANA_CONCEPT_MIN_SIM` defaults to `0.75`. Note that `latent_extract.py` documents a calibration
finding for the *same* embedding model on the capability side:

```python
# Calibrated for Vertex text-embedding-005: a short entity vs a longer capability description
# tops out ~0.55-0.60 for true matches (vs ~0.35-0.40 for unrelated). The spec's 0.65 assumed
# OpenAI 1536-dim embeddings and never cleared here, leaving suggestion_queue empty.
_MIN_MATCH_SCORE = 0.45
```

Concept-to-concept comparisons are short-text-vs-short-text, so 0.75 is not obviously wrong —
but it has never been validated in prod. **Watch the dedup rate for the first week** (§3, query 5);
if `identity_concepts` grows faster than roughly one new concept per two new claims, lower it.

---

## 3. Verification queries

Run all five against `kmetmatfxdkrialwrnzj` after the backfill.

**1 · Capability embeddings — the acceptance gate for FIX 1.**

```sql
select count(*)                                             as total,
       count(embedding)                                     as embedded,
       count(*) filter (where is_active and embedding is null) as active_unembedded,
       min(vector_dims(embedding))                          as min_dims,
       max(vector_dims(embedding))                          as max_dims
from public.capability_index;
```
PASS ⇒ `total = embedded = 8` · `active_unembedded = 0` · `min_dims = max_dims = 768`.

**2 · The matcher actually returns rows** (round-trips a real capability vector; similarity must be 1.0).

```sql
select capability_id, round(similarity::numeric, 4) as similarity
from public.match_latent_capabilities(
       (select embedding from public.capability_index where capability_id = 'looking.tip'),
       3, 0.45);
```
PASS ⇒ at least one row, `looking.tip` at `1.0000`.

**3 · Concept embeddings + link coverage — the acceptance gate for FIX 2.**

```sql
select (select count(*) from public.identity_concepts)                                  as concepts,
       (select count(*) from public.identity_concepts where canonical_embedding is null) as concepts_unembedded,
       (select count(*) from public.user_identity_claims)                                as claims,
       (select count(*) from public.claim_concept_links)                                 as links,
       (select count(*) from public.user_identity_claims c
          where not exists (select 1 from public.claim_concept_links l where l.claim_id = c.id)) as unlinked,
       round(100.0 * (select count(*) from public.claim_concept_links)
                   / nullif((select count(*) from public.user_identity_claims), 0), 1)   as pct_linked;
```
PASS ⇒ `concepts_unembedded = 0` · `unlinked = 0` · `pct_linked = 100.0`.

**4 · The onion matcher produces non-zero concept scores** (the business outcome).

```sql
select count(*)                                    as scored_peers,
       sum(shared_concept_count)                   as total_shared_concepts,
       max(shared_concept_count)                   as best
from public.users u,
     lateral public.score_onion_candidates_for_user(u.id, 20, 1);
```
PASS ⇒ `total_shared_concepts > 0`. It is currently 0.

**5 · Ongoing health (run weekly for the first month) — is the live path writing?**

```sql
select date_trunc('day', c.created_at)::date            as day,
       count(*)                                          as claims,
       count(l.claim_id)                                 as linked,
       count(*) - count(l.claim_id)                      as unlinked,
       count(distinct l.concept_id)                      as distinct_concepts
from public.user_identity_claims c
left join public.claim_concept_links l on l.claim_id = c.id
where c.created_at > now() - interval '30 days'
group by 1 order by 1;
```
PASS ⇒ `unlinked = 0` for every day **after** the flag deploy. A non-zero `unlinked` on a later day
means the flag is not actually live in the running revision.
Watch `distinct_concepts` vs `claims` for the dedup-rate concern in §2.7.

**6 · Suggestion queue starts filling** (proves FIX 1 end-to-end through the worker).

```sql
select count(*) as queued,
       count(distinct capability_id) as capabilities_hit,
       round(min(confidence)::numeric,3) as min_conf,
       round(max(confidence)::numeric,3) as max_conf,
       max(created_at) as latest
from public.suggestion_queue;
```
PASS ⇒ `queued > 0` within a few hours of live traffic. Currently 0.

---

## 4. Rollback

Neither fix drops or overwrites user data; both are additive. Rollback is nonetheless exact,
because the pre-state is precisely known.

**FIX 1 — `capability_index`:**

```sql
-- Restores the (broken) status quo ante. Only useful to prove causation.
update public.capability_index set embedding = null;
```

There is no data to lose: the vectors are deterministically regenerable by re-running
`python -m scripts.backfill_capability_embeddings`.

**FIX 2 — `identity_concepts` / `claim_concept_links`, using the §2.6 snapshot tables:**

```sql
begin;

-- Remove links the backfill created.
delete from public.claim_concept_links l
where not exists (select 1 from public._pr11_links_before b where b.claim_id = l.claim_id);

-- Remove concepts the backfill created (FK is on delete restrict, so links must go first).
delete from public.identity_concepts ic
where not exists (select 1 from public._pr11_concepts_before b where b.id = ic.id);

-- Re-NULL the embeddings Step A repaired.
update public.identity_concepts ic
set canonical_embedding = null
from public._pr11_concepts_before b
where b.id = ic.id and b.was_null;

commit;

-- Once satisfied:
-- drop table public._pr11_concepts_before;
-- drop table public._pr11_links_before;
```

If the snapshot tables were not created, the known pre-state is exactly: 3 concepts
(`faith_community`, `heritage_brazilian`, `parents_toddlers`, all `canonical_embedding IS NULL`,
all `bucket = 'general'`) and 3 links, the ones stamped `2026-06-29 18:48:19.144262+00`.

**Flag rollback:** unset `IDENTITY_CONCEPT_LINK_ENABLED` (or set `0`) and redeploy. The gate is
purely additive — `user_identity_claims` writes are unconditional, so turning it off cannot
lose claims. Links already written simply stop growing.

---

## 5. Summary of changes requested

| # | Change | Type | Where |
|---|---|---|---|
| 1 | Run `backfill_capability_embeddings.py` against prod | ops (no code change) | `services/lana-worker/scripts/` |
| 2 | Run the §2.6 concept backfill SQL against prod | ops / one-off migration | `supabase/` |
| 3 | Add `IDENTITY_CONCEPT_LINK_ENABLED=1` | config | `deploy/lana-worker-prod.env` + `.env.example` + `scripts/deploy-lana-worker.sh` |
| 4 | Make `20260905120001` embedding-idempotent (add the Step A `UPDATE`) | code fix | `supabase/migrations/` |
| 5 | *(optional)* Repair the 3 mis-bucketed fixture concepts | data | `identity_concepts` |

Items 1–3 are the ones that unblock Lana's semantic routing and the onion matcher.
Item 4 prevents this recurring the next time an environment is bootstrapped.

**No GitHub PR has been created; nothing has been pushed or applied.**
