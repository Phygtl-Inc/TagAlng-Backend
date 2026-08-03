# PR11 · Embedding backfill — `capability_index` + `identity_concepts`

**Repo:** `Phygtl-Inc/TagAlng-Backend` · **Target:** Supabase `kmetmatfxdkrialwrnzj` (tagalng-prod)
**Origin:** data-layer audit 2026-07-30 · **Implemented:** 2026-07-31

Two shipped features have been running in prod as silent no-ops. Both fail *closed* —
they return an empty result set rather than an error — so there is no exception, no log
line, and no metric marking the failure.

> **Environment note.** `_CODE_TRUTH_2026-07-30.md` verified against `rjlcyvwogmfmngemhbmn`,
> which is **tagalng-dev**, not prod. Its volume figures (1,254 users · 979 claims · 5,692
> `suggestion_queue`) are dev figures. Prod is a much younger, smaller database — which is
> what makes the root cause legible.

---

## 0. Prod state (verified read-only 2026-07-31)

| table | prod | expected |
|---|---|---|
| `capability_index` | 8 rows | 8 |
| …with `embedding` | **0** | 8 |
| `identity_concepts` | 3 rows | ~48 |
| …with `canonical_embedding` | **0** | all |
| `claim_concept_links` | **3** | 51 |
| `user_identity_claims` | 51 (100% embedded) | — |
| `latent_signals` | 121 (100% embedded) | — |
| `suggestion_queue` | **0** | > 0 |

Everything *upstream* of the two matchers is alive. Only their reference data is missing.

---

## 0b. APPLIED TO PROD — 2026-07-31

All three steps are live. Sequence actually executed: migration → capability backfill → deploy.

| | before | after |
|---|---|---|
| `identity_concepts` | 3 | **51** |
| …NULL `canonical_embedding` | 3 | **0** |
| `claim_concept_links` | 3 | **55** |
| unlinked claims | 52 | **0** |
| `capability_index` embedded | 0/8 | **8/8** (768 dims) |
| `IDENTITY_CONCEPT_LINK_ENABLED` | absent | **`1` on revision `00030-v6m`** |

Gate 2 (matcher round-trip) returned `looking.tip` at `1.0000`, `sharing.tip` at `0.8629`,
`discovery.find_peers` at `0.6722`.

**Threshold evidence — `_MIN_MATCH_SCORE = 0.45` is confirmed correct.** Real entity strings
(exactly what `_embed_entity` builds) matched against the freshly embedded catalog in prod:

| entity | top matches |
|---|---|
| `karate (activity)` | `discovery.find_activities` 0.494 · `looking.meet` 0.484 |
| `pediatrician (service)` | `looking.tip` 0.524 · `sharing.host` 0.458 |
| `stroller (gear)` | `looking.swap` 0.579 · `sharing.swap` 0.529 |
| `playgroup (activity)` | `looking.meet` 0.756 · `sharing.host` 0.706 |
| `sourdough baking (interest)` | no match (correct — no capability covers a general interest) |

Every routing decision is semantically right. Note that the spec's original `0.65` would have
dropped `karate` (0.494) and `pediatrician` (0.524) — the two most obviously valid matches in
the set. This is direct empirical support for the drop to 0.45 and evidence against raising it.

Still to observe: `suggestion_queue > 0` on live traffic (§4 query 6), and the weekly
`unlinked = 0` check (§4 query 5).

**Two operational lessons from the rollout.** `supabase db push` takes no positional target and
pushes to the *linked* project — `supabase db push prod` silently pushed to dev. Use
`./scripts/db-push.sh prod`. And a prod deploy run before these changes existed carried no flag
at all while appearing to succeed; the `Concept links: ON` line added to
`scripts/deploy-lana-worker.sh` exists to make that visible.

---

## 1. Root causes

### 1.1 `capability_index` — a post-deploy step never run against prod

The Layer 3 migration seeds capability rows with `embedding = NULL` on purpose (pure SQL
cannot call an embedding model) and
`services/lana-worker/scripts/backfill_capability_embeddings.py` fills them in afterwards.
That script was run on dev and never on prod.

`match_latent_capabilities` carries `and ci.embedding is not null`, which removes all 8 rows
*before* the `<=>` operator is evaluated. The RPC returns an empty set; the caller
(`latent_extract._queue_capability_matches`) does `if not matches: return 0` and queues
nothing. Semantic capability routing has never fired in prod.

The RPC itself is correct — proven by injecting one real vector into `capability_index` and
calling the matcher, which returned `looking.tip` at similarity `1.0000`.

### 1.2 `identity_concepts` — three compounding causes

**(a) A one-shot migration fired a month too early.** `20260905120001_identity_concepts_backfill`
ran at prod bootstrap on `2026-06-29 18:48:19`, when `user_identity_claims` held 3 fixture
rows. It produced 3 concepts and 3 links, then never ran again. The 45+ real claims arrived
`2026-07-27..30`. Every organic claim is unlinked.

**(b) The migration is not embedding-idempotent.** It copies the claim's embedding at INSERT
time and ends in `on conflict (concept) do nothing`. At bootstrap the source claims were not
yet embedded, so `canonical_embedding` was written NULL. The claims were embedded *afterwards*
by `claims_persist.kick_claim_embedding_backfill`, and nothing propagates that repair forward.
Because of `do nothing`, re-running the migration can never fix an existing row.

**(c) The feature flag was set in no deploy artifact.** `IDENTITY_CONCEPT_LINK_ENABLED`
appeared repo-wide in only three places: `app/claims_persist.py`, one test, and a migration
comment. It defaults to `""` → falsy → the live linking step never ran. The test sets the
variable itself, so CI stayed green while the feature was dead in prod.

**Consequence.** `match_concepts_by_embedding` filters `canonical_embedding is not null`, so
with 3/3 NULL it returned empty for every bucket. `score_onion_candidates_for_user` collapsed
to its circle half (`place +3 / type +1`) over 4 confirmed affiliations, and
`count_shared_concepts_for_user` was dead.

---

## 2. What this PR changes

| # | Change | File |
|---|---|---|
| 1 | Catch-up + repair migration (Steps A–D) | `supabase/migrations/20260918120000_identity_concepts_embedding_repair.sql` |
| 2 | `get_or_create_concept` heals a NULL `canonical_embedding` (Step E) | same migration |
| 3 | Runtime self-heal for an un-embedded `capability_index`, with a loud log | `services/lana-worker/app/latent_extract.py` |
| 4 | Shared embedding-text builder so script and worker cannot drift | `services/lana-worker/app/capability_embed.py` |
| 5 | `IDENTITY_CONCEPT_LINK_ENABLED` + concept tuning vars plumbed through deploy | `deploy/*.env*`, `scripts/deploy-lana-worker.sh` |
| 6 | Tests for the self-heal and the shared text | `services/lana-worker/tests/test_capability_catalog_selfheal.py` |

### 2.1 Why a migration rather than ad-hoc SQL

The audit proposed running the concept backfill as a one-off ops step. It is shipped as a
migration instead so every environment (dev, prod, and any future bootstrap) converges to the
same state through the normal `db-push` path, and so the repair is reviewable in git.

`user_identity_claims.embedding` is 100% populated, so the vectors are copied **in-database** —
no worker script and no embedding API calls are needed for concepts.

Two deliberate improvements over the audit's SQL:

- **Step B seed order** is `order by concept, (embedding is null), created_at asc`, so an
  embedded claim wins the seed slot over an older un-embedded one. This prevents cause (b) at
  the source rather than relying on Step A to clean up after it.
- **Step E** makes `get_or_create_concept` fill a missing `canonical_embedding` on the next
  mention of that concept. Previously the fast path returned the existing id and never touched
  the vector, so a NULL row stayed permanently invisible to the matcher — meaning this catch-up
  would be needed again on every future bootstrap. An existing non-NULL vector is **never**
  overwritten; the canonical vector must stay stable or match results drift per turn.

### 2.2 Why the capability fix is not a per-turn script

`capability_index` is **8 static reference rows** — a catalog, not user data. It changes only
when a migration seeds new capabilities. Nothing needs to run per turn, and nothing needs to
run on a schedule:

- **Per-turn embedding already happens** and always did — `_embed_entity` embeds the user's
  entity live, and `claims_persist._embed_claim` embeds each claim live. Those were never
  broken.
- **New concepts embed themselves.** Once `IDENTITY_CONCEPT_LINK_ENABLED=1`,
  `get_or_create_concept` receives the claim's vector and stores it at creation. The catalog
  grows correctly on its own; the migration is a one-time catch-up for the backlog.
- **The capability catalog now self-heals.** `_kick_capability_catalog_selfheal` is reachable
  **only** from the empty-match path, and then at most once per hour per process
  (`_CATALOG_SELFHEAL_COOLDOWN_S`). On a healthy catalog the probe is a single indexed
  `count`-shaped query that finds nothing and returns. When it does find NULL rows it logs
  `capability_index_unembedded` at ERROR — the log line whose absence hid this for a month —
  and embeds them in a daemon thread.

So the backfill script stays the correct tool for a deliberate, verifiable prod fix, and the
self-heal is the backstop that prevents another silent month.

---

## 3. Deploy sequence — **order is load-bearing**

Backfill embeddings first, enable linking second. Reversed, the resolver sees an un-embedded
catalog, `match_concepts_by_embedding` returns empty, and every new claim mints a duplicate
concept instead of reusing one — fragmenting the catalog.

```bash
# 1. Migration (dev first, then prod). Adds Steps A-E.
./scripts/db-push.sh dev  --list      # confirm 20260918120000 is pending
./scripts/db-push.sh dev
./scripts/db-push.sh prod --list
./scripts/db-push.sh prod             # requires typing "push to prod"

# 2. Capability embeddings — 8 rows, 8 Vertex embed_content calls.
#    Idempotent by default (.is_("embedding","null")); safe to re-run.
#    Do NOT add batching or concurrency, and do NOT use --all unless the embedding
#    model or capability_embedding_text() changed.
cd services/lana-worker
set -a; source ../../deploy/lana-worker-prod.env; set +a
python -m scripts.backfill_capability_embeddings

# 3. Run §4 query 1 and 3 — both must PASS before continuing.

# 4. Deploy the worker with IDENTITY_CONCEPT_LINK_ENABLED=1 (already in the env files;
#    the deploy script prints the precondition and defaults the flag to 0 when unset).
./scripts/deploy-lana-worker-prod.sh
```

`LANA_CONCEPT_MIN_SIM` defaults to `0.75` and **has never been validated in prod**.
`latent_extract.py` documents the same embedding model topping out at ~0.55–0.60 for true
matches on entity-vs-description, which is why `_MIN_MATCH_SCORE` there is `0.45`.
Concept-to-concept is short-vs-short text, so 0.75 may well hold — but watch §4 query 5 for
the first week and lower it if `identity_concepts` grows faster than roughly one new concept
per two new claims. It is now an env var, so tuning it is a redeploy, not a code change.

---

## 4. Verification

**1 · Capability embeddings** — acceptance gate for the capability fix.
```sql
select count(*) as total, count(embedding) as embedded,
       count(*) filter (where is_active and embedding is null) as active_unembedded,
       min(vector_dims(embedding)) as min_dims, max(vector_dims(embedding)) as max_dims
from public.capability_index;
```
PASS ⇒ `total = embedded = 8` · `active_unembedded = 0` · `min_dims = max_dims = 768`.

**2 · The matcher returns rows** (round-trips a real capability vector).
```sql
select capability_id, round(similarity::numeric, 4) as similarity
from public.match_latent_capabilities(
  (select embedding from public.capability_index where capability_id = 'looking.tip'), 3, 0.45);
```
PASS ⇒ at least one row, `looking.tip` at `1.0000`.

**3 · Concept embeddings + link coverage** — acceptance gate for the concept fix.
```sql
select (select count(*) from public.identity_concepts) as concepts,
       (select count(*) from public.identity_concepts where canonical_embedding is null) as concepts_unembedded,
       (select count(*) from public.claim_concept_links) as links,
       (select count(*) from public.user_identity_claims c
          where not exists (select 1 from public.claim_concept_links l where l.claim_id = c.id)) as unlinked;
```
PASS ⇒ `concepts_unembedded = 0` · `unlinked = 0`.

**4 · The onion matcher produces non-zero concept scores** — the business outcome.
```sql
select count(*) as scored_peers, sum(shared_concept_count) as total_shared_concepts
from public.users u, lateral public.score_onion_candidates_for_user(u.id, 20, 1);
```
PASS ⇒ `total_shared_concepts > 0`. Currently 0.

**5 · Ongoing health** (weekly for the first month) — is the live path writing?
```sql
select date_trunc('day', c.created_at)::date as day, count(*) as claims,
       count(l.claim_id) as linked, count(*) - count(l.claim_id) as unlinked,
       count(distinct l.concept_id) as distinct_concepts
from public.user_identity_claims c
left join public.claim_concept_links l on l.claim_id = c.id
where c.created_at > now() - interval '30 days'
group by 1 order by 1;
```
PASS ⇒ `unlinked = 0` for every day **after** the flag deploy. A non-zero `unlinked` on a later
day means the flag is not actually live in the running revision — check it with
`gcloud run services describe tagalng-lana-worker-prod --format='value(spec.template.spec.containers[0].env)'`.
Watch `distinct_concepts` vs `claims` for the dedup-rate concern in §3.

**6 · Suggestion queue starts filling** — proves the capability fix end-to-end.
```sql
select count(*) as queued, count(distinct capability_id) as capabilities_hit, max(created_at) as latest
from public.suggestion_queue;
```
PASS ⇒ `queued > 0` within a few hours of live traffic. Currently 0.

**7 · Self-heal never fires on a healthy catalog.** Grep worker logs for
`capability_index_unembedded`. Expected: absent. If present, the catalog regressed and the
worker is repairing it — the message names the script to run.

---

## 5. Rollback

Both fixes are additive; no user data is dropped or overwritten.

**Capability embeddings** — `update public.capability_index set embedding = null;`
Nothing is lost: the vectors are deterministically regenerable by re-running the script.
Note the runtime self-heal will re-fill them within an hour of traffic, so this is only useful
alongside a worker rollback.

**Concepts / links** — the migration snapshots the pre-state first:
```sql
begin;
delete from public.claim_concept_links l
where not exists (select 1 from public._pr11_links_before b where b.claim_id = l.claim_id);
delete from public.identity_concepts ic          -- FK is on delete restrict: links go first
where not exists (select 1 from public._pr11_concepts_before b where b.id = ic.id);
update public.identity_concepts ic
set canonical_embedding = null, bucket = b.bucket
from public._pr11_concepts_before b
where b.id = ic.id and b.was_null;
commit;
```
Once satisfied, `drop table public._pr11_concepts_before, public._pr11_links_before;`.

If the snapshots are missing, the known pre-state is exactly: 3 concepts
(`faith_community`, `heritage_brazilian`, `parents_toddlers`), all `canonical_embedding IS NULL`,
all `bucket = 'general'`, and 3 links — every row stamped `2026-06-29 18:48:19.144262+00`.

**Flag** — set `IDENTITY_CONCEPT_LINK_ENABLED=0` and redeploy. The gate is purely additive;
`user_identity_claims` writes are unconditional, so turning it off cannot lose claims. Links
already written simply stop growing.

**Step E (`get_or_create_concept`)** — re-apply the function body from
`20260905120000_identity_concepts_schema.sql`. Nothing else depends on the new behaviour.

---

## 6. Pre-merge validation performed

- The concept migration was executed against a scratch Postgres 17 database seeded to
  reproduce prod's exact history (3 fixture claims with NULL embeddings + concepts/links as
  `20260905120001` produced them, then the claims embedded afterwards, then 46 organic claims).
  Result: concepts 3 → 45, NULL `canonical_embedding` 3 → 0, links 3 → 49, unlinked 46 → 0.
- **Idempotency:** a second run produced zero drift in `identity_concepts` and
  `claim_concept_links` (set-difference both directions).
- **Step B seed order:** a concept whose oldest claim is un-embedded took its vector and quote
  from the embedded younger claim rather than being written NULL.
- **Step D:** all three fixture concepts moved to `heritage` / `stage` / `faith`.
- **Step E:** a NULL vector healed on the next call; an existing vector was *not* overwritten;
  a caller with no vector left the row NULL without error; a brand-new concept still inserted.
- **Re-mentions did not duplicate**: 3 concepts for 3 re-mentioned slugs; synonyms unioned.
- Worker tests: 10 new + 44 existing pass (`tests/test_capability_catalog_selfheal.py`,
  `test_latent_extract.py`, `test_claims_persist_links.py`).
- Prod state re-confirmed read-only via PostgREST on 2026-07-31 (table in §0).

The local run substitutes `text` for `extensions.vector(768)` because pgvector is not installed
locally. The migration performs no vector arithmetic, so all real logic is exercised; the type
is copied verbatim from the already-applied `20260905120000`.
