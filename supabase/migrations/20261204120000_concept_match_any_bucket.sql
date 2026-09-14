-- match_concepts_by_embedding: let p_bucket => null mean "search every bucket".
--
-- The predicate was `ic.bucket = p_bucket`. With p_bucket null that is `bucket = NULL`,
-- which evaluates to NULL rather than true, so WHERE dropped every row: an unbucketed
-- lookup returned zero concepts, silently, with no error. Nothing surfaced the failure
-- because an empty result is indistinguishable from "no concept matched this text".
--
-- The one caller today (claims_persist._resolve_concept_id) always passes a real bucket
-- and is unaffected. The new caller — resolving a recommendation ASK to the concepts that
-- would grant domain standing — has no bucket to pass: an ask is not a claim, and the
-- standing behind "Turkish food" may sit in heritage ("grew up in Gaziantep"), activity
-- ("cooks Turkish weekly") or interest. Guessing one bucket loses the other two.
--
-- Signature, return shape and grants are unchanged, so this is a drop-in replacement
-- (create or replace, no overload created, no drop needed).

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
  where (p_bucket is null or ic.bucket = p_bucket)
    and ic.canonical_embedding is not null
    and (1 - (ic.canonical_embedding <=> p_embedding)) >= p_min_similarity
  order by ic.canonical_embedding <=> p_embedding
  limit p_limit;
$$;

comment on function public.match_concepts_by_embedding(text, extensions.vector, int, real) is
  'Nearest identity_concepts to an embedding. p_bucket null searches every bucket (an ask '
  'has no bucket); a non-null p_bucket scopes to it, as claim resolution requires.';

-- Unchanged from 20260905120000: worker-only. Restated because create or replace does not
-- reset privileges and a future re-run of this file must land on the same grants.
revoke execute on function public.match_concepts_by_embedding(text, extensions.vector, int, real)
  from public, authenticated, anon;
grant execute on function public.match_concepts_by_embedding(text, extensions.vector, int, real)
  to service_role;
