-- Repair + catch-up for identity_concepts / claim_concept_links.
--
-- WHY THIS EXISTS (three compounding causes, all diagnosed on tagalng-prod 2026-07-30):
--
--   (a) 20260905120001_identity_concepts_backfill is a ONE-SHOT migration. On prod it ran
--       at bootstrap (2026-06-29) when user_identity_claims held 3 fixture rows. The 45
--       real claims arrived 2026-07-27..30 and were never picked up.
--   (b) That migration copies user_identity_claims.embedding at INSERT time and ends in
--       `on conflict (concept) do nothing`. At bootstrap the source claims were not yet
--       embedded, so canonical_embedding was written NULL. The claims were embedded
--       afterwards by the worker self-heal (claims_persist.kick_claim_embedding_backfill),
--       but nothing propagates that repair forward — and `do nothing` means re-running the
--       migration can never fix an existing row. An explicit UPDATE is required.
--   (c) IDENTITY_CONCEPT_LINK_ENABLED was set in no deploy artifact, so the worker's live
--       linking step never ran. Fixed separately in deploy/ + scripts/deploy-lana-worker.sh.
--
-- Consequence being repaired: match_concepts_by_embedding filters
-- `canonical_embedding is not null`, so with 3/3 NULL it returned empty for every bucket;
-- score_onion_candidates_for_user collapsed to its circle half and
-- count_shared_concepts_for_user was dead.
--
-- ORDERING IS LOAD-BEARING: this migration must land BEFORE
-- IDENTITY_CONCEPT_LINK_ENABLED=1 is deployed. Reversed, the resolver sees an un-embedded
-- catalog, match_concepts_by_embedding returns empty, and every new claim mints a
-- duplicate concept — fragmenting the catalog irreversibly.
--
-- Properties: idempotent (safe to re-run, second run is a no-op), additive (never deletes
-- or overwrites user data), and a clean no-op on a fresh/empty database.
-- Verified against prod inside `begin; … rollback;`:
--   identity_concepts 3 -> 45 · NULL canonical_embedding 3 -> 0
--   claim_concept_links 3 -> 48 · unlinked claims 45 -> 0

-- Snapshot the pre-state so rollback is exact rather than reconstructed.
-- Drop both once the change is confirmed good (see docs/prs/PR11_embedding_backfill.md §4).
create table if not exists public._pr11_concepts_before as
  select id, canonical_embedding is null as was_null, bucket from public.identity_concepts;
create table if not exists public._pr11_links_before as
  select claim_id from public.claim_concept_links;

do $$
declare
  v_null_emb int;
  v_unlinked int;
begin
  select count(*) into v_null_emb
    from public.identity_concepts where canonical_embedding is null;
  select count(*) into v_unlinked
    from public.user_identity_claims c
    where not exists (select 1 from public.claim_concept_links l where l.claim_id = c.id);
  raise notice 'pr11 pre-state: % concepts with NULL canonical_embedding, % unlinked claims',
    v_null_emb, v_unlinked;
end $$;

-- ---------------------------------------------------------------------------
-- Step A · repair canonical_embedding on rows inserted with NULL (cause (b)).
-- The `is null` predicate is what makes this self-limiting on re-run.
-- ---------------------------------------------------------------------------
update public.identity_concepts ic
set canonical_embedding     = seed.embedding,
    canonical_example_quote = coalesce(ic.canonical_example_quote, seed.source_quote),
    updated_at              = now()
from (
  select distinct on (concept) concept, embedding, source_quote
  from public.user_identity_claims
  where embedding is not null
  order by concept, created_at asc
) seed
where seed.concept = ic.concept
  and ic.canonical_embedding is null;

-- ---------------------------------------------------------------------------
-- Step B · insert concepts for claims that have none yet (cause (a)).
--
-- Differs from 20260905120001 in the seed ORDER BY: `(embedding is null)` sorts first so
-- an embedded claim wins the seed slot over an older un-embedded one. That prevents cause
-- (b) at the source instead of relying on Step A to clean up after it.
-- ---------------------------------------------------------------------------
insert into public.identity_concepts (
  concept, label, bucket, synonyms,
  canonical_example_quote, canonical_embedding,
  created_at, updated_at
)
select
  seed.concept,
  seed.label,
  coalesce(seed.bucket, 'general'),
  coalesce(agg.synonyms, '{}'::text[]),
  seed.source_quote,
  seed.embedding,
  seed.created_at,
  now()
from (
  select distinct on (concept)
    concept, label, bucket, source_quote, embedding, created_at
  from public.user_identity_claims
  order by concept, (embedding is null), created_at asc
) seed
left join (
  select
    concept,
    coalesce((
      select array_agg(distinct s)
      from (
        select unnest(synonyms) as s
        from public.user_identity_claims u2
        where u2.concept = uic.concept
      ) x
    )[1:20], '{}'::text[]) as synonyms
  from public.user_identity_claims uic
  group by concept
) agg on agg.concept = seed.concept
on conflict (concept) do nothing;

-- ---------------------------------------------------------------------------
-- Step C · link every claim to its concept (cause (a) + (c) catch-up).
-- ---------------------------------------------------------------------------
insert into public.claim_concept_links (claim_id, concept_id, created_at)
select uic.id, ic.id, uic.created_at
from public.user_identity_claims uic
join public.identity_concepts ic on ic.concept = uic.concept
on conflict (claim_id) do nothing;

-- ---------------------------------------------------------------------------
-- Step D · re-bucket the three bootstrap fixture concepts.
--
-- Their source claims had bucket = NULL, so 20260905120001's coalesce(bucket,'general')
-- filed all three under 'general'. Because match_concepts_by_embedding filters
-- `ic.bucket = p_bucket`, they are unreachable for heritage/stage/faith lookups no matter
-- what their embedding says. Scoped to these three exact slugs and to bucket='general',
-- so it is a no-op anywhere else. All three values pass identity_concepts_bucket_check
-- (heritage|stage|vicinity|faith|activity|interest|general).
-- ---------------------------------------------------------------------------
update public.identity_concepts
set bucket = 'heritage', updated_at = now()
where concept = 'heritage_brazilian' and bucket = 'general';
update public.identity_concepts
set bucket = 'stage', updated_at = now()
where concept = 'parents_toddlers' and bucket = 'general';
update public.identity_concepts
set bucket = 'faith', updated_at = now()
where concept = 'faith_community' and bucket = 'general';

-- ---------------------------------------------------------------------------
-- Step E · make get_or_create_concept self-healing (stops cause (b) recurring).
--
-- The original fast path returns the existing row's id and never touches
-- canonical_embedding, so a concept stuck at NULL stayed permanently invisible to
-- match_concepts_by_embedding even under live traffic — the catch-up above would be
-- needed again on every future environment bootstrap. Now, when the caller supplies a
-- vector and the stored one is NULL, the row heals itself on the next mention.
--
-- Deliberately one-directional: an existing non-NULL canonical_embedding is NEVER
-- overwritten. The canonical vector must stay stable or match results drift per turn.
-- Body is otherwise byte-identical to 20260905120000.
-- ---------------------------------------------------------------------------
create or replace function public.get_or_create_concept(
  p_concept                text,
  p_label                  text,
  p_bucket                 text,
  p_synonyms               text[],
  p_canonical_example_quote text,
  p_canonical_embedding    extensions.vector(768)
) returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
  v_existing_syns text[];
  v_merged_syns text[];
  v_emb_is_null boolean;
begin
  -- Fast path: already exists
  select id, synonyms, canonical_embedding is null
    into v_id, v_existing_syns, v_emb_is_null
    from public.identity_concepts where concept = p_concept;

  if v_id is not null then
    -- Union new synonyms into existing, cap at 20 (order-preserving)
    select array_agg(distinct s order by s)
      from (
        select unnest(v_existing_syns) as s
        union
        select unnest(coalesce(p_synonyms, '{}'::text[])) as s
      ) t
      into v_merged_syns;
    v_merged_syns := v_merged_syns[1:20];
    if v_merged_syns is distinct from v_existing_syns then
      update public.identity_concepts set synonyms = v_merged_syns where id = v_id;
    end if;
    -- Self-heal: fill a missing canonical vector, never replace a present one.
    if v_emb_is_null and p_canonical_embedding is not null then
      update public.identity_concepts
      set canonical_embedding     = p_canonical_embedding,
          canonical_example_quote = coalesce(canonical_example_quote, p_canonical_example_quote),
          updated_at              = now()
      where id = v_id
        and canonical_embedding is null;   -- re-check under the write, race-safe
    end if;
    return v_id;
  end if;

  -- Slow path: insert. Handles the concurrent-race case via ON CONFLICT.
  insert into public.identity_concepts (
    concept, label, bucket, synonyms,
    canonical_example_quote, canonical_embedding
  ) values (
    p_concept, p_label, p_bucket,
    (coalesce(p_synonyms, '{}'::text[]))[1:20],
    p_canonical_example_quote, p_canonical_embedding
  )
  on conflict (concept) do nothing
  returning id into v_id;

  if v_id is not null then
    return v_id;
  end if;

  -- Race lost: re-read
  select id into v_id from public.identity_concepts where concept = p_concept;
  return v_id;
end;
$$;

grant execute on function public.get_or_create_concept(text, text, text, text[], text, extensions.vector) to service_role;
revoke execute on function public.get_or_create_concept(text, text, text, text[], text, extensions.vector) from public, authenticated, anon;

do $$
declare
  v_concepts int;
  v_null_emb int;
  v_links int;
  v_unlinked int;
begin
  select count(*) into v_concepts from public.identity_concepts;
  select count(*) into v_null_emb
    from public.identity_concepts where canonical_embedding is null;
  select count(*) into v_links from public.claim_concept_links;
  select count(*) into v_unlinked
    from public.user_identity_claims c
    where not exists (select 1 from public.claim_concept_links l where l.claim_id = c.id);
  raise notice 'pr11 post-state: % concepts (% still NULL embedding), % links, % unlinked claims',
    v_concepts, v_null_emb, v_links, v_unlinked;
  -- A leftover NULL means the source claim is also un-embedded; the worker's
  -- kick_claim_embedding_backfill will fix the claim, and re-running this migration
  -- (Step A) then propagates it. Not fatal — do not abort the deploy for it.
  if v_null_emb > 0 then
    raise warning 'pr11: % concept(s) still have NULL canonical_embedding — re-run after '
      'claim embeddings backfill so match_concepts_by_embedding can see them', v_null_emb;
  end if;
end $$;
