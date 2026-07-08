-- Rapport · Ring C · coverage by RECORDED provenance (replaces the fuzzy claim↔question
-- similarity coverage from 20260809120000, which almost never fired — a claim descriptor and a
-- question rarely score above ~0.6 even on the same topic, so steering silently no-op'd).
--
-- New model: record which claim each question is ABOUT (deepens_concept). A thread is "covered"
-- when it has a question pointing at it OR it produced a gap's answer. Sub-claims spun off by
-- answers ("runs mornings" from a running question) are folded in by claim↔claim similarity
-- (same-space, reliable — unlike claim↔question). So "uncovered" = threads we genuinely haven't
-- touched, and heritage stops getting starved while running gets asked four times.

alter table public.rapport_gaps
  add column if not exists deepens_concept text;

comment on column public.rapport_gaps.deepens_concept is
  'The identity-claim concept this question was generated to deepen. Powers exact coverage '
  '(vs the old fuzzy claim↔question similarity). Null for legacy rows.';

create index if not exists rapport_gaps_user_deepens_idx
  on public.rapport_gaps (user_id, deepens_concept)
  where deepens_concept is not null;

-- Signature/param names change, so drop the old definition before recreating.
drop function if exists public.rapport_uncovered_claims(uuid, real, int);

-- Identity threads we have NOT asked about yet. A claim is COVERED when it either has a question
-- pointing at it (deepens_concept) or is the answer-product of some gap (answer_claim_id); and any
-- claim within p_cluster_threshold cosine of a covered claim is folded into that covered theme
-- (so answer-spawned sub-claims don't resurface as "new" topics). Everything else is uncovered.
create or replace function public.rapport_uncovered_claims(
  p_user_id uuid,
  p_cluster_threshold real default 0.8,
  p_limit int default 8
) returns table (concept text, label text, bucket text, source_quote text)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with covered as (
    select c.id, c.concept, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and (
        c.concept in (
          select g.deepens_concept from public.rapport_gaps g
          where g.user_id = p_user_id and g.deepens_concept is not null and g.status <> 'skipped'
        )
        or c.id in (
          select g.answer_claim_id from public.rapport_gaps g
          where g.user_id = p_user_id and g.answer_claim_id is not null
        )
      )
  )
  select c.concept, c.label, c.bucket, c.source_quote
  from public.user_identity_claims c
  where c.user_id = p_user_id
    and c.dismissed_at is null
    and coalesce(c.transient, false) = false
    and c.embedding is not null
    and c.id not in (select id from covered)
    and not exists (
      select 1 from covered cv
      where cv.embedding is not null
        and (1 - (c.embedding <=> cv.embedding)) >= p_cluster_threshold
    )
  order by c.created_at asc
  limit p_limit;
$$;

comment on function public.rapport_uncovered_claims(uuid, real, int) is
  'Rapport: identity threads with no question yet (exact provenance + claim↔claim clustering). Service role only.';

revoke all on function public.rapport_uncovered_claims(uuid, real, int) from public, anon, authenticated;
grant execute on function public.rapport_uncovered_claims(uuid, real, int) to service_role;
