-- Find, at aspect level. This is the other half of the loop.
--
--   "I'm looking for a restaurant where the owner actually speaks Italian and the
--    porcelain is unique."
--
-- That query is not one embedding. It is TWO clauses, each about a different aspect, and
-- the right answer is a subject that satisfies BOTH. Averaging them into one vector gives
-- a restaurant that is vaguely Italian-ish and vaguely nice, which is what every existing
-- search already does and the reason none of them can answer this.
--
-- So: the worker splits the query into clauses, embeds each, and this function scores
-- subjects by how many clauses they actually satisfy — coverage first, closeness second.
--
-- WHAT MATCHES WHAT
--   reco_aspect.embedding embeds "label: answer" — "the owner: he grew up in Naples and
--   switched to Italian with us". So a clause matches on the CONTENT of what people said,
--   not on a band. That is deliberate:
--
--     · "the owner speaks Italian"  → content match. Sentiment is irrelevant.
--     · "a good doctor"             → no content, pure band. Sentiment carries it.
--
--   Most real queries are the first kind, which is why content leads and sentiment is
--   only ever a gate here — never a ranking term. The merge threshold and the bands are
--   uncalibrated at time of writing, and a term that reorders results without anyone
--   being able to see it do so is the failure mode to avoid.

create or replace function public.search_subjects_by_aspect(
  p_clauses        extensions.vector(768)[],  -- one embedding per clause of the query
  p_viewer_id      uuid,
  p_subject_scope  uuid[] default null,       -- pre-filtered candidates (geo, category…)
  p_min_similarity real    default 0.72,
  p_exclude_negative boolean default true,    -- see note below
  p_limit          int     default 20
)
returns table (
  subject_ref       uuid,
  clauses_matched   int,
  clauses_total     int,
  mean_similarity   real,
  n_people          int,
  n_shared_community int,
  matched_aspects   jsonb   -- [{clause, aspect_key, aspect_label, quote, similarity}]
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with clauses as (
    select ordinality::int as clause_idx, v as embedding
    from unnest(p_clauses) with ordinality as t(v, ordinality)
  ),
  -- Best-matching aspect per (subject, clause). One clause must not be able to win a
  -- subject twice by matching three phrasings of the same thing.
  hits as (
    select distinct on (a.subject_ref, c.clause_idx)
      a.subject_ref,
      c.clause_idx,
      a.aspect_key,
      a.aspect_label,
      a.answer_verbatim,
      a.author_id,
      (1 - (a.embedding <=> c.embedding))::real as similarity
    from clauses c
    join public.reco_aspect a
      on a.embedding is not null
     and a.subject_ref is not null
     and a.answer_source not in ('open','skipped')
    where (p_subject_scope is null or a.subject_ref = any(p_subject_scope))
      -- Someone asking for a place where the owner is around does not want the
      -- restaurant where three people said the owner was rude. The gate is deliberately
      -- coarse and OFF the ranking path; it never reorders, it only excludes.
      and (not p_exclude_negative or a.sentiment is null or a.sentiment > -1)
      and (1 - (a.embedding <=> c.embedding)) >= p_min_similarity
    order by a.subject_ref, c.clause_idx, a.embedding <=> c.embedding
  ),
  shared as (
    select distinct h.subject_ref, h.author_id
    from hits h
    join public.visible_place_members(p_viewer_id) vm on vm.user_id = h.author_id
    join (
      select distinct vm2.place_ref
      from public.visible_place_members(p_viewer_id) vm2
      where vm2.user_id = p_viewer_id
    ) mine on mine.place_ref = vm.place_ref
  )
  select
    h.subject_ref,
    count(distinct h.clause_idx)::int                   as clauses_matched,
    (select count(*)::int from clauses)                 as clauses_total,
    avg(h.similarity)::real                             as mean_similarity,
    count(distinct h.author_id)::int                    as n_people,
    (select count(distinct s.author_id)::int
       from shared s where s.subject_ref = h.subject_ref) as n_shared_community,
    jsonb_agg(jsonb_build_object(
      'clause',       h.clause_idx,
      'aspect_key',   h.aspect_key,
      'aspect_label', h.aspect_label,
      'quote',        h.answer_verbatim,
      'similarity',   round(h.similarity::numeric, 3)
    ) order by h.clause_idx)                            as matched_aspects
  from hits h
  group by h.subject_ref
  -- COVERAGE FIRST. A place that satisfies both clauses beats a place that satisfies one
  -- of them brilliantly — that is the entire reason for splitting the query.
  order by count(distinct h.clause_idx) desc,
           avg(h.similarity) desc,
           count(distinct h.author_id) desc
  limit greatest(p_limit, 0);
$$;

revoke all on function public.search_subjects_by_aspect(
  extensions.vector[], uuid, uuid[], real, boolean, int) from public, anon;
grant execute on function public.search_subjects_by_aspect(
  extensions.vector[], uuid, uuid[], real, boolean, int) to authenticated, service_role;

comment on function public.search_subjects_by_aspect(
  extensions.vector[], uuid, uuid[], real, boolean, int) is
  'Multi-clause aspect retrieval. Each clause of a query ("the owner speaks Italian", '
  '"the porcelain is unique") is matched separately and subjects are ranked by COVERAGE '
  'first. Matching is on the content of what people said; the sentiment band is only a '
  'coarse exclusion gate and never a ranking term — it is uncalibrated and a term that '
  'silently reorders results is unreviewable. Pass p_subject_scope from the existing geo/'
  'category recall step: this ranks, it does not replace recall.';

-- Returns the matched quotes so the caller can show WHY a result came back. A result
-- that cannot explain itself is indistinguishable from a guess, and this is the surface
-- where the whole method either reads as magic or as noise.
