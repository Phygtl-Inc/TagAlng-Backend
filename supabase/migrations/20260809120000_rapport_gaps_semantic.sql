-- Rapport · Ring C · semantic suppression (the "fast-follow" promised in 20260806120000_rapport_gaps.sql).
--
-- Two behaviours, both powered by the embeddings we already store on claims/questions:
--   1. DEDUP — never open a follow-up that means the same thing as one we already asked
--      (e.g. "which trails do you run?" vs "any local spots you like for running?").
--   2. COVERAGE — steer synthesis toward identity threads we have NOT asked about yet, so one
--      topic (running) can't hog every question while heritage / married / books go untouched.
--
-- Vector math stays in SQL (mirrors the RPC pattern the matching code already uses) — the app
-- never parses raw vectors.

alter table public.rapport_gaps
  add column if not exists question_embedding extensions.vector(768);

comment on column public.rapport_gaps.question_embedding is
  'Embedding of `question` (text-embedding-005, 768d). Powers semantic dedup + coverage steering.';

create index if not exists rapport_gaps_question_embedding_hnsw_idx
  on public.rapport_gaps
  using hnsw (question_embedding extensions.vector_cosine_ops)
  where question_embedding is not null;

-- Max cosine similarity of an embedding to any of the user's existing (non-skipped) gap
-- questions. Used for DEDUP (pass a candidate question's embedding) AND for COVERAGE (pass a
-- claim's embedding → "have we already asked about this thread?"). Returns 0 when none exist.
create or replace function public.rapport_question_max_similarity(
  p_user_id uuid,
  p_embedding extensions.vector(768)
) returns real
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  select coalesce(
    (select 1 - min(g.question_embedding <=> p_embedding)
     from public.rapport_gaps g
     where g.user_id = p_user_id
       and g.question_embedding is not null
       and g.status <> 'skipped'),
    0)::real;
$$;

comment on function public.rapport_question_max_similarity(uuid, extensions.vector) is
  'Rapport: max cosine similarity of an embedding to the user''s existing gap questions. Service role only.';

-- Active, durable identity claims whose theme we have NOT yet asked about — i.e. no existing gap
-- question sits within p_cover_threshold cosine of the claim. Least-covered first. Synthesis
-- draws its next topics from here; an empty result means "we know enough — go quiet".
create or replace function public.rapport_uncovered_claims(
  p_user_id uuid,
  p_cover_threshold real default 0.82,
  p_limit int default 8
) returns table (concept text, label text, bucket text, source_quote text, max_sim real)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  select c.concept, c.label, c.bucket, c.source_quote, sim.max_sim
  from public.user_identity_claims c
  cross join lateral (
    select coalesce(
      (select 1 - min(c.embedding <=> g.question_embedding)
       from public.rapport_gaps g
       where g.user_id = p_user_id
         and g.question_embedding is not null
         and g.status <> 'skipped'),
      0)::real as max_sim
  ) sim
  where c.user_id = p_user_id
    and c.dismissed_at is null
    and coalesce(c.transient, false) = false
    and c.embedding is not null
    and sim.max_sim < p_cover_threshold
  order by sim.max_sim asc, c.created_at asc
  limit p_limit;
$$;

comment on function public.rapport_uncovered_claims(uuid, real, int) is
  'Rapport: identity claims not yet covered by any gap question (least-covered first). Service role only.';

revoke all on function public.rapport_question_max_similarity(uuid, extensions.vector) from public, anon, authenticated;
revoke all on function public.rapport_uncovered_claims(uuid, real, int) from public, anon, authenticated;
grant execute on function public.rapport_question_max_similarity(uuid, extensions.vector) to service_role;
grant execute on function public.rapport_uncovered_claims(uuid, real, int) to service_role;
