-- One second angle per thread when the first question dies unanswered.
--
-- 20260810120000 marks an identity thread COVERED as soon as any gap points at it, and
-- carries `and g.status <> 'skipped'` -- plainly intending "a question the user skipped
-- should not count as coverage; come back at that thread differently". That clause has
-- never fired: nothing writes status='skipped'. record_skip calls increment_skip_and_reopen,
-- which writes only 'open' or 'expired', and the tile's 24h rotation (rapport_ranker,
-- daily mode) goes through the same RPC. The only writers of the literal 'skipped' in this
-- repo are the three read-side `<> 'skipped'` clauses themselves.
--
-- So the ceiling was absolute: one question per thread, forever, whatever became of it.
-- Lana asks "How often do you get to it these days?" about the running Maria volunteered,
-- Maria ignores it into expiry, and running is covered for good -- Lana never asks anything
-- else about a thread the user raised herself.
--
-- The rule now: a thread is spent once it has had TWO questions, or while any question on
-- it is still live (open/asked/answered/muted). Only the single-ask, all-expired case
-- reopens, so a thread gets exactly one retry and the loop is bounded by construction --
-- no counter, no new column, no way to ping-pong. `<> 'skipped'` is dropped rather than
-- carried: dead code that reads as a working escape hatch is worse than none.
--
-- Unchanged: the answer_claim_id branch (a claim produced BY an answer is covered), the
-- claim<->claim cosine clustering, the transient/embedding filters, and the signature.

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
  with asks as (
    select g.deepens_concept as concept,
           count(*) as total,
           count(*) filter (where g.status = 'expired') as expired
    from public.rapport_gaps g
    where g.user_id = p_user_id
      and g.deepens_concept is not null
    group by g.deepens_concept
  ),
  answered_claims as (
    select g.answer_claim_id as id
    from public.rapport_gaps g
    where g.user_id = p_user_id and g.answer_claim_id is not null
  ),
  -- Every claim that has ever ANCHORED a question, whatever became of it. This is the
  -- cluster anchor set, and it is deliberately wider than `covered` below: a thread
  -- taking its retry stops being covered but must keep absorbing its own neighbours,
  -- or "running" reopening would reopen "jogging" beside it and Lana would ask the
  -- same question twice in different words -- the very thing the clustering prevents.
  themed as (
    select c.id, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and (c.concept in (select a.concept from asks a) or c.id in (select id from answered_claims))
  ),
  -- Spent: two asks, or one still alive. A lone expired ask is absent here -- that is
  -- the retry.
  covered as (
    select c.id
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and (
        c.concept in (
          select a.concept from asks a
          where a.total >= 2 or a.expired < a.total
        )
        or c.id in (select id from answered_claims)
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
      select 1 from themed t
      -- Excluding self matters only now that a themed claim can be eligible: without
      -- it, a retry thread would suppress itself at cosine 1.0 and never come back.
      where t.id <> c.id
        and t.embedding is not null
        and (1 - (c.embedding <=> t.embedding)) >= p_cluster_threshold
    )
  order by c.created_at asc
  limit p_limit;
$$;

comment on function public.rapport_uncovered_claims(uuid, real, int) is
  'Rapport: identity threads with no LIVE question (exact provenance + claim<->claim '
  'clustering). A thread whose only ask expired unanswered reopens exactly once; two asks '
  'spend it permanently. Service role only.';

revoke all on function public.rapport_uncovered_claims(uuid, real, int) from public, anon, authenticated;
grant execute on function public.rapport_uncovered_claims(uuid, real, int) to service_role;
