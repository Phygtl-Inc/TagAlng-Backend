-- Aspect Find · a clause may also match an aspect's TOPIC, not only its words.
--
-- Prod QA 2026-09-28: "a barber who speaks Spanish with good prices" matched 1 of 2 on
-- every card. Two neighbours had answered about pricing ("About $25 for a fade…"), but a
-- price stated is not phrased like "good prices", so the content vectors never cleared
-- 0.72. The aspect they answered is literally "pricing".
--
-- So a clause now matches an aspect when EITHER
--   · its content is close (reco_aspect.embedding, "label: answer", >= p_min_similarity), or
--   · its topic is close (reco_aspect.label_embedding, "label (key)", >= p_min_label_similarity)
-- — the topic arm only for an answered, non-negative aspect (the negative gate below still
-- applies to both). Stricter floor on the topic arm (0.80 vs 0.72): a topic match says the
-- neighbour talked about pricing and was not unhappy with it, which is weaker evidence than
-- their words matching the ask, so it has to be a clearer match to count.
-- matched_aspects[].via says which arm matched, so a card can be honest about why.
--
-- Signature changes (new trailing parameter), so the old one is dropped first.

drop function if exists public.search_subjects_by_aspect(text[], uuid[], real, boolean, int);

create or replace function public.search_subjects_by_aspect(
  p_clauses          text[],                  -- one embedding per clause of the query
  p_subject_scope    uuid[]  default null,    -- pre-filtered candidates (geo, category…)
  p_min_similarity   real    default 0.72,
  p_exclude_negative boolean default true,    -- see note below
  p_limit            int     default 20,
  p_min_label_similarity real default 0.80    -- the topic arm's floor, see header
)
returns table (
  subject_ref        uuid,
  clauses_matched    int,
  clauses_total      int,
  mean_similarity    real,
  n_people           int,
  n_shared_community int,
  matched_aspects    jsonb   -- [{clause, aspect_key, aspect_label, quote, similarity}]
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
#variable_conflict use_column
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_clauses is null or cardinality(p_clauses) = 0 then
    return;
  end if;

  return query
  with clauses as (
    select t.ordinality::int as clause_idx, t.v::extensions.vector(768) as embedding
    from unnest(p_clauses[1:8]) with ordinality as t(v, ordinality)
    where t.v is not null
  ),
  -- Best-matching aspect per (subject, clause). One clause must not be able to win a
  -- subject twice by matching three phrasings of the same thing.
  scored as (
    select
      a.subject_ref,
      c.clause_idx,
      a.aspect_key,
      a.aspect_label,
      a.answer_verbatim,
      a.author_id,
      case when a.embedding is null then null
           else (1 - (a.embedding <=> c.embedding))::real end       as content_sim,
      case when a.label_embedding is null then null
           else (1 - (a.label_embedding <=> c.embedding))::real end as label_sim
    from clauses c
    join public.reco_aspect a
      on a.subject_ref is not null
     and a.answer_source not in ('open','skipped')
     and (a.embedding is not null or a.label_embedding is not null)
    where (p_subject_scope is null or a.subject_ref = any(p_subject_scope))
      -- Someone asking for a place where the owner is around does not want the
      -- restaurant where three people said the owner was rude. The gate is deliberately
      -- coarse and OFF the ranking path; it never reorders, it only excludes.
      and (not p_exclude_negative or a.sentiment is null or a.sentiment > -1)
      and public._reco_aspect_visible(v_me, a.signal_id)
  ),
  -- Best-matching aspect per (subject, clause), by whichever arm scored higher. One clause
  -- must not be able to win a subject twice by matching three phrasings of the same thing.
  hits as (
    select distinct on (s.subject_ref, s.clause_idx)
      s.subject_ref,
      s.clause_idx,
      s.aspect_key,
      s.aspect_label,
      s.answer_verbatim,
      s.author_id,
      greatest(coalesce(s.content_sim, 0), coalesce(s.label_sim, 0))::real as similarity,
      case when coalesce(s.content_sim, 0) >= p_min_similarity then 'content'
           else 'label' end                                              as via
    from scored s
    where coalesce(s.content_sim, 0) >= p_min_similarity
       or coalesce(s.label_sim, 0) >= p_min_label_similarity
    order by s.subject_ref, s.clause_idx,
             greatest(coalesce(s.content_sim, 0), coalesce(s.label_sim, 0)) desc
  ),
  shared as (
    select distinct h.subject_ref, h.author_id
    from hits h
    join public.visible_place_members(v_me) vm on vm.user_id = h.author_id
    join (
      select distinct vm2.place_ref
      from public.visible_place_members(v_me) vm2
      where vm2.user_id = v_me
    ) mine on mine.place_ref = vm.place_ref
    where h.author_id <> v_me
  )
  select
    h.subject_ref,
    count(distinct h.clause_idx)::int                    as clauses_matched,
    (select count(*)::int from clauses)                  as clauses_total,
    avg(h.similarity)::real                              as mean_similarity,
    count(distinct h.author_id)::int                     as n_people,
    (select count(distinct s.author_id)::int
       from shared s where s.subject_ref = h.subject_ref) as n_shared_community,
    jsonb_agg(jsonb_build_object(
      'clause',       h.clause_idx,
      'aspect_key',   h.aspect_key,
      'aspect_label', h.aspect_label,
      'quote',        h.answer_verbatim,
      'similarity',   round(h.similarity::numeric, 3),
      'via',          h.via
    ) order by h.clause_idx)                             as matched_aspects
  from hits h
  group by h.subject_ref
  -- COVERAGE FIRST. A place that satisfies both clauses beats a place that satisfies one
  -- of them brilliantly — that is the entire reason for splitting the query.
  order by count(distinct h.clause_idx) desc,
           avg(h.similarity) desc,
           count(distinct h.author_id) desc
  limit greatest(least(coalesce(p_limit, 20), 50), 0);
end;
$$;

revoke all on function public.search_subjects_by_aspect(text[], uuid[], real, boolean, int, real)
  from public, anon;
grant execute on function public.search_subjects_by_aspect(text[], uuid[], real, boolean, int, real)
  to authenticated, service_role;

comment on function public.search_subjects_by_aspect(text[], uuid[], real, boolean, int, real) is
  'Multi-clause aspect retrieval for the caller (auth.uid()). Each clause of a query ("the '
  'owner speaks Italian", "the porcelain is unique") is matched separately and subjects '
  'are ranked by COVERAGE first. Matching is on the content of what people said; the '
  'sentiment band is only a coarse exclusion gate, never a ranking term and never '
  'returned. Pass p_subject_scope from the existing geo/category recall step: this ranks, '
  'it does not replace recall. Visibility = _reco_aspect_visible.';

-- Returns the matched quotes so the caller can show WHY a result came back. A result
-- that cannot explain itself is indistinguishable from a guess, and this is the surface
-- where the whole method either reads as magic or as noise.
