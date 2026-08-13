-- ---------------------------------------------------------------------------
-- FIX: score_onion_candidates_for_user threw on every call.
-- ---------------------------------------------------------------------------
-- 20260914120000_score_onion_candidates.sql:86 aggregated a uuid with min():
--
--   min(pc.place_ref) filter (where pc.place_ref = cc.place_ref)
--
-- Postgres defines no min(uuid) aggregate and no migration adds one, so every
-- invocation raised 42883 undefined_function (PostgREST surfaces it as a 404).
-- plpgsql resolves function names at execution, not creation, so the original
-- migration applied cleanly on dev AND prod and the RPC has never once
-- returned a row.  app/onion.py fails open to an empty candidate list, so the
-- onion contribution was a silent no-op in every environment.
--
-- The filter clause guarantees every value reaching the aggregate already
-- equals cc.place_ref, so this is only ever "pick that one shared uuid, or
-- null".  Aggregate over ::text and cast the result back.  Note the parens:
-- the cast applies to the whole aggregate-with-filter, not to the filter.
--
-- Body is otherwise byte-identical to 20260914120000; see that file for the
-- design notes [1]-[6] (ZIP gate, block-scoping, weights, ...).

create or replace function public.score_onion_candidates_for_user(
  p_user_id   uuid,
  p_limit     int default 20,
  p_min_score int default 1
) returns table (
  peer_user_id            uuid,
  nickname                text,
  avatar_url              text,
  score                   int,
  same_place_bonus        int,
  same_type_bonus         int,
  shared_concept_count    int,
  shared_concept_labels   text[],
  shared_place_ref        uuid
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

  return query with
    caller_circles as (
      select a.place_ref, a.circle_type
      from public.circle_affiliations a
      where a.user_id = p_user_id
        and a.status = 'confirmed'
        and a.dismissed_at is null
        and a.place_ref is not null
    ),
    peer_circles as (
      select a.user_id as peer_id, a.place_ref, a.circle_type
      from public.circle_affiliations a
      where a.user_id <> p_user_id
        and a.status = 'confirmed'
        and a.dismissed_at is null
        and a.place_ref is not null
        and not public.lana_is_blocked(p_user_id, a.user_id)
    ),
    circle_scored as (
      select
        pc.peer_id,
        max(case
              when pc.place_ref = cc.place_ref then 3
              when pc.circle_type = cc.circle_type then 1
              else 0
            end)::int as circle_bonus,
        -- min(uuid) does not exist; aggregate as text and cast back.
        (min(pc.place_ref::text) filter (where pc.place_ref = cc.place_ref))::uuid
          as shared_place_ref
      from peer_circles pc
      join caller_circles cc
        on pc.place_ref = cc.place_ref
        or pc.circle_type = cc.circle_type
      group by pc.peer_id
    ),
    caller_concepts as (
      -- Distinct concept_ids the caller holds via public, non-dismissed claims.
      select distinct ccl.concept_id
      from public.user_identity_claims c
      join public.claim_concept_links ccl on ccl.claim_id = c.id
      where c.user_id = p_user_id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ),
    peer_concepts as (
      select
        pc.user_id as peer_id,
        pcl.concept_id
      from public.user_identity_claims pc
      join public.claim_concept_links pcl on pcl.claim_id = pc.id
      where pc.dismissed_at is null
        and pc.disclosure = 'public'
        and pc.user_id <> p_user_id
        and not public.lana_is_blocked(p_user_id, pc.user_id)
    ),
    shared as (
      select
        pp.peer_id,
        pp.concept_id
      from peer_concepts pp
      join caller_concepts cc on cc.concept_id = pp.concept_id
    ),
    concept_scored as (
      select
        s.peer_id,
        count(distinct s.concept_id)::int          as shared_concept_count,
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
        )                                           as concept_labels
      from shared s
      group by s.peer_id
    ),
    candidates as (
      select
        coalesce(cs.peer_id, ks.peer_id)         as peer_id,
        coalesce(cs.circle_bonus, 0)             as circle_bonus,
        cs.shared_place_ref,
        coalesce(ks.shared_concept_count, 0)     as shared_concept_count,
        ks.concept_labels
      from circle_scored cs
      full outer join concept_scored ks on ks.peer_id = cs.peer_id
    ),
    scored as (
      select
        c.peer_id,
        (c.circle_bonus + c.shared_concept_count)::int         as score,
        (case when c.circle_bonus = 3 then 3 else 0 end)::int  as same_place_bonus,
        (case when c.circle_bonus = 1 then 1 else 0 end)::int  as same_type_bonus,
        c.shared_concept_count,
        c.concept_labels,
        c.shared_place_ref
      from candidates c
    )
  select
    s.peer_id                                     as peer_user_id,
    u.nickname,
    u.profile_photo_url                           as avatar_url,
    s.score,
    s.same_place_bonus,
    s.same_type_bonus,
    s.shared_concept_count,
    coalesce(s.concept_labels, '{}')              as shared_concept_labels,
    s.shared_place_ref
  from scored s
  join public.users u on u.id = s.peer_id
  where s.score >= p_min_score
  order by s.score desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));

end;
$$;

grant execute on function public.score_onion_candidates_for_user(uuid, int, int) to service_role;
revoke execute on function public.score_onion_candidates_for_user(uuid, int, int) from public, authenticated, anon;
