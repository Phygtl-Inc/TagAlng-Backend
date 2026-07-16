-- Phase 1 backfill: populate identity_concepts + user_concept_claims from user_identity_claims.
-- Idempotent (ON CONFLICT DO NOTHING). Does NOT modify user_identity_claims.

do $$
declare
  v_master_count int;
  v_join_count int;
  v_bucket_conflict_count int;
  r record;
begin
  -- Pre-check: emit counts so operator can abort if unexpectedly large.
  select count(distinct concept) into v_master_count from public.user_identity_claims;
  select count(*) into v_join_count from public.user_identity_claims;
  raise notice 'identity_concepts backfill: % distinct concepts -> master rows; % source rows -> join rows',
    v_master_count, v_join_count;

  -- Report bucket disagreements before inserting.
  for r in
    select concept,
           count(distinct coalesce(bucket, 'general')) as bucket_variants,
           array_agg(distinct coalesce(bucket, 'general')) as buckets
    from public.user_identity_claims
    group by concept
    having count(distinct coalesce(bucket, 'general')) > 1
  loop
    raise notice 'bucket disagreement on concept=%: buckets=% (using oldest row''s bucket)',
      r.concept, r.buckets;
    v_bucket_conflict_count := coalesce(v_bucket_conflict_count, 0) + 1;
  end loop;
  raise notice 'total bucket disagreements: %', coalesce(v_bucket_conflict_count, 0);
end $$;

-- Populate identity_concepts. Seed row = min(created_at) per concept.
insert into public.identity_concepts (
  concept, label, bucket, synonyms,
  canonical_example_quote, canonical_embedding,
  created_at, updated_at
)
select
  seed.concept,
  seed.label,
  coalesce(seed.bucket, 'general') as bucket,
  agg.synonyms,
  seed.source_quote,
  seed.embedding,
  seed.created_at,
  now()
from (
  select distinct on (concept)
    concept, label, bucket, source_quote, embedding, created_at
  from public.user_identity_claims
  order by concept, created_at asc
) seed
join (
  select
    concept,
    (
      -- Union synonyms across all rows for this concept, dedup, cap 20.
      select array_agg(distinct s)
      from (
        select unnest(synonyms) as s
        from public.user_identity_claims u2
        where u2.concept = uic.concept
      ) x
    )[1:20] as synonyms
  from public.user_identity_claims uic
  group by concept
) agg on agg.concept = seed.concept
on conflict (concept) do nothing;

-- Populate user_concept_claims. One-for-one from user_identity_claims.
-- Preserves original id, timestamps, and every per-user field.
insert into public.user_concept_claims (
  id, user_id, concept_id, label,
  tone, confidence, disclosure, source_quote,
  utterance_embedding, transient, dismissed_at,
  created_at, updated_at
)
select
  uic.id,
  uic.user_id,
  ic.id as concept_id,
  uic.label,
  uic.tone,
  uic.confidence,
  uic.disclosure,
  uic.source_quote,
  uic.embedding,
  uic.transient,
  uic.dismissed_at,
  uic.created_at,
  uic.updated_at
from public.user_identity_claims uic
join public.identity_concepts ic on ic.concept = uic.concept
on conflict (id) do nothing;
