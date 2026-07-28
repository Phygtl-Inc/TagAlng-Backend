-- ---------------------------------------------------------------------------
-- RPC: score_onion_candidates_for_user
-- ---------------------------------------------------------------------------
-- Ranks introduction candidates for p_user_id using a composite Onion score.
--
-- [1] ZIP GATE NOT ENFORCED HERE.
--     This function does NOT enforce the ZIP unlock gate.  The gate
--     (LANA_ZIP_UNLOCK_GATE, zip_unlock.discovery_zip_gate(user_id, surface="peers"))
--     is enforced in the Python wrapper in a later phase.  A caller invoking this
--     RPC directly silently bypasses the gate.
--
-- [2] NO BLOCK-SCOPING — deliberate divergence.
--     find_my_fellows (20260823120000_rank_by_shared_count.sql) and
--     find_peers_by_claim_semantic (20260827110000_find_peers_semantic.sql) both
--     scope peers to home_block_id.  This function does not.  Locality belongs
--     to the ring layer, not here.
--
-- [3] NO PROXIMITY TERM.
--     Users store no coordinates; there is nothing to compute proximity against.
--
-- [4] SHARED-CONCEPT CTE IS RE-DERIVED INLINE — does not call
--     count_shared_concepts_for_user.  That function pre-truncates to a top-N
--     ranked on concept count alone, which would drop a same-place candidate
--     before scoring.  The shared-concept logic is reproduced inline on purpose.
--
-- [5] PLACES TABLE NOT JOINED.
--     A bare place_ref uuid is returned and disclosure is resolved by the caller
--     (open question O7).
--
-- [6] WEIGHTS HARDCODED 3/1/1; COMPONENTS RETURNED SEPARATELY.
--     same_place_bonus (+3), same_type_bonus (+1), and shared_concept_count (+1
--     per concept) are hardcoded but returned as individual columns so they can
--     be re-tuned without a migration.

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
        min(pc.place_ref) filter (where pc.place_ref = cc.place_ref) as shared_place_ref
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
        and c.dismissed_at is null         -- mirrors 20260823120000_rank_by_shared_count.sql:169
        and c.disclosure = 'public'        -- mirrors 20260823120000_rank_by_shared_count.sql:170
    ),
    peer_concepts as (
      -- For every other user, collect their public, non-dismissed concept_ids
      -- together with the peer user_id.
      select
        pc.user_id as peer_id,
        pcl.concept_id
      from public.user_identity_claims pc
      join public.claim_concept_links pcl on pcl.claim_id = pc.id
      where pc.dismissed_at is null        -- mirrors 20260823120000_rank_by_shared_count.sql:187
        and pc.disclosure = 'public'       -- mirrors 20260823120000_rank_by_shared_count.sql:188
        and pc.user_id <> p_user_id
        and not public.lana_is_blocked(p_user_id, pc.user_id)
    ),
    shared as (
      -- Inner-join on concept_id: only concepts present for both caller and peer count.
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
        )                                           as concept_labels
      from shared s
      group by s.peer_id
    ),
    candidates as (
      -- FULL OUTER: a peer may score on the circle side, the concept side, or both.
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
      -- circle_bonus is 3, 1 or 0; split it back out so weights stay re-tunable.
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

comment on function public.score_onion_candidates_for_user(uuid, int, int) is
  'Ranks introduction candidates for p_user_id by a composite Onion score: '
  'same-place circle overlap (+3), same-type circle overlap (+1), plus one point '
  'per shared public non-dismissed identity concept.  Score components are returned '
  'separately so weights can be re-tuned without a migration.  '
  'The ZIP unlock gate is NOT enforced here; it is enforced by the caller.';

grant execute on function public.score_onion_candidates_for_user(uuid, int, int) to service_role;
revoke execute on function public.score_onion_candidates_for_user(uuid, int, int) from public, authenticated, anon;
