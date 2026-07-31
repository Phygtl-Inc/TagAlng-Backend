-- NOTE: This function assumes IDENTITY_CONCEPT_LINK_ENABLED (lana-worker env flag) is on
-- and that the concept backfill migration 20260905120001_identity_concepts_backfill.sql
-- has run. Peers without concept links will undercount shared concepts until backfill
-- completes. This is a known PR caveat recorded in-code.

-- ---------------------------------------------------------------------------
-- RPC: count_shared_concepts_for_user
-- ---------------------------------------------------------------------------
-- Returns each peer who shares at least p_min_shared_count public, non-dismissed
-- identity concepts with p_user_id, ranked by shared count desc then nickname asc.
-- No embeddings, no similarity, no block scoping — matching on identical concept_id
-- is exact by construction; cross-concept heritage/faith blocking is moot here.

create or replace function public.count_shared_concepts_for_user(
  p_user_id            uuid,
  p_limit              int     default 20,
  p_min_shared_count   int     default 1,
  p_exclude_self       boolean default true
) returns table (
  peer_user_id            uuid,
  nickname                text,
  avatar_url              text,
  shared_concept_count    int,
  shared_concept_labels   text[],
  shared_concept_ids      uuid[]
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  return query
  with caller_concepts as (
    -- Distinct concept_ids the caller holds via public, non-dismissed claims.
    select distinct ccl.concept_id
    from public.user_identity_claims c
    join public.claim_concept_links ccl on ccl.claim_id = c.id
    where c.user_id = p_user_id
      and c.dismissed_at is null         -- mirrors 20260823120000_rank_by_shared_count.sql:169
      and c.disclosure = 'public'        -- mirrors 20260823120000_rank_by_shared_count.sql:170
  ),
  peer_concepts as (
    -- For every other (or same, controlled by p_exclude_self) user, collect their
    -- public, non-dismissed concept_ids together with the peer user_id.
    select
      pc.user_id as peer_id,
      pcl.concept_id
    from public.user_identity_claims pc
    join public.claim_concept_links pcl on pcl.claim_id = pc.id
    where pc.dismissed_at is null        -- mirrors 20260823120000_rank_by_shared_count.sql:187
      and pc.disclosure = 'public'       -- mirrors 20260823120000_rank_by_shared_count.sql:188
      and (
        not p_exclude_self
        or pc.user_id <> p_user_id
      )
  ),
  shared as (
    -- Inner-join on concept_id: only concepts present for both caller and peer count.
    select
      pp.peer_id,
      pp.concept_id
    from peer_concepts pp
    join caller_concepts cc on cc.concept_id = pp.concept_id
  ),
  aggregated as (
    select
      s.peer_id,
      count(distinct s.concept_id)::int          as shared_count,
      -- Alphabetically-ordered labels, capped at 50.
      (
        select array_agg(ic.label order by ic.label)
        from (
          select ic2.label
          from public.identity_concepts ic2
          where ic2.id in (
            select s2.concept_id from shared s2 where s2.peer_id = s.peer_id
          )
          order by ic2.label
          limit 50
        ) ic
      )                                           as concept_labels,
      array_agg(distinct s.concept_id order by s.concept_id) as concept_ids
    from shared s
    group by s.peer_id
    having count(distinct s.concept_id) >= p_min_shared_count
  )
  select
    a.peer_id                                     as peer_user_id,
    u.nickname,
    u.profile_photo_url                           as avatar_url,
    a.shared_count                                as shared_concept_count,
    coalesce(a.concept_labels, '{}')              as shared_concept_labels,
    coalesce(a.concept_ids,    '{}'::uuid[])      as shared_concept_ids
  from aggregated a
  join public.users u on u.id = a.peer_id
  order by a.shared_count desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

comment on function public.count_shared_concepts_for_user(uuid, int, int, boolean) is
  'Returns peers who share at least p_min_shared_count public, non-dismissed identity '
  'concepts with p_user_id, ranked by shared count descending then nickname. '
  'Concept overlap is determined by identical concept_id (exact match); no embeddings used.';

grant execute on function public.count_shared_concepts_for_user(uuid, int, int, boolean) to service_role;
revoke execute on function public.count_shared_concepts_for_user(uuid, int, int, boolean) from public, authenticated, anon;
