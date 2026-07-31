-- Phase 1: identity_concepts master table + claim_concept_links link table + RPCs.
-- No changes to user_identity_claims. Feature-flagged additive step in lana-worker.

-- ---------------------------------------------------------------------------
-- 1. identity_concepts  (globally unique concept catalog)
-- ---------------------------------------------------------------------------

create table public.identity_concepts (
  id                       uuid primary key default gen_random_uuid(),
  concept                  text not null unique,
  label                    text not null,
  bucket                   text not null,
  synonyms                 text[] not null default '{}',
  canonical_example_quote  text,
  canonical_embedding      extensions.vector(768),
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),

  constraint identity_concepts_concept_format
    check (concept ~ '^[a-z][a-z0-9_]{1,63}$'),
  constraint identity_concepts_bucket_check
    check (bucket in ('heritage','stage','vicinity','faith','activity','interest','general'))
);

comment on table public.identity_concepts is
  'Master catalog of identity concepts (bucket-partitioned, globally unique). Populated by lana-worker via get_or_create_concept RPC. Phase 1.';

create index identity_concepts_bucket_idx
  on public.identity_concepts (bucket);

create index identity_concepts_embedding_hnsw_idx
  on public.identity_concepts using hnsw (canonical_embedding extensions.vector_cosine_ops)
  where canonical_embedding is not null;

create trigger identity_concepts_updated_at
  before update on public.identity_concepts
  for each row execute function public.set_updated_at();

alter table public.identity_concepts enable row level security;

-- No SELECT policy: authenticated clients get default-deny SELECT (Phase 1 intent).
-- Service-role bypasses RLS.
create policy "concepts_no_client_write"
  on public.identity_concepts
  for all
  to authenticated
  using (false)
  with check (false);


-- ---------------------------------------------------------------------------
-- 2. claim_concept_links  (link each user_identity_claims row to identity_concepts)
-- ---------------------------------------------------------------------------

-- Defensive no-op; table only ever existed in un-applied drafts.
drop table if exists public.user_concept_claims;

create table public.claim_concept_links (
  id          uuid primary key default gen_random_uuid(),
  claim_id    uuid not null unique references public.user_identity_claims (id) on delete cascade,
  concept_id  uuid not null references public.identity_concepts (id) on delete restrict,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index claim_concept_links_concept_id_idx
  on public.claim_concept_links (concept_id);

create trigger claim_concept_links_updated_at
  before update on public.claim_concept_links
  for each row execute function public.set_updated_at();

alter table public.claim_concept_links enable row level security;
-- Deliberately NO policies: default deny; worker writes via service_role.

comment on table public.claim_concept_links is
  'Links each user_identity_claims row to the shared identity_concepts row it resolved to; worker-only.';


-- ---------------------------------------------------------------------------
-- 3. RPC: get_or_create_concept
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
begin
  -- Fast path: already exists
  select id, synonyms into v_id, v_existing_syns
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


-- ---------------------------------------------------------------------------
-- 4. RPC: match_concepts_by_embedding
-- ---------------------------------------------------------------------------

create or replace function public.match_concepts_by_embedding(
  p_bucket         text,
  p_embedding      extensions.vector(768),
  p_limit          int,
  p_min_similarity real
) returns table (
  id uuid,
  concept text,
  label text,
  bucket text,
  synonyms text[],
  canonical_example_quote text,
  similarity real
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  select
    ic.id,
    ic.concept,
    ic.label,
    ic.bucket,
    ic.synonyms,
    ic.canonical_example_quote,
    (1 - (ic.canonical_embedding <=> p_embedding))::real as similarity
  from public.identity_concepts ic
  where ic.bucket = p_bucket
    and ic.canonical_embedding is not null
    and (1 - (ic.canonical_embedding <=> p_embedding)) >= p_min_similarity
  order by ic.canonical_embedding <=> p_embedding
  limit p_limit;
$$;

grant execute on function public.match_concepts_by_embedding(text, extensions.vector, int, real) to service_role;
revoke execute on function public.match_concepts_by_embedding(text, extensions.vector, int, real) from public, authenticated, anon;
