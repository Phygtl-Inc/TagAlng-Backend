-- ---------------------------------------------------------------------------
-- Mutual claims count toward RANKING, never toward the sentence.
-- ---------------------------------------------------------------------------
-- Relational Identity Claims §7 splits one question in two: may this claim
-- contribute to a match, and may its text be shown to a peer. get_peer_profile
-- shipped the display half (20261022120000: mutual claims render for a
-- connection, not for a stranger). The matcher never got the ranking half —
-- both concept arms filtered disclosure = 'public', so a mutual claim was
-- invisible to scoring on either side.
--
-- That drops precisely the high-signal set. On dev every mutual row is faith
-- (Christian, attends church), and the extractor tags "I'm 5 years sober" as
-- mutual on sight — the claims most worth pairing two strangers on were the
-- ones the matcher could not see (evals, 2026-08-25: 6 mutual / 5 private of
-- 971 active claims).
--
-- One change, and the important half is what does NOT change:
--   * score now pairs on public + mutual, both sides (ranked_concept_count).
--   * shared_concept_count / _labels / _subjects stay PUBLIC-ONLY. They are
--     display: app/onion.py:54 feeds them straight into "you both run", and
--     drop_connected_peers guarantees this surface only ever describes people
--     the caller is NOT connected to — exactly whom a mutual claim is withheld
--     from. A mutual overlap therefore lifts a peer up the list silently, and
--     the copy still names only claims both sides published.
--
-- Body is 20261022120000 verbatim apart from the disclosure predicates, the
-- both_public flag and the score expression; diff the files if in doubt.
-- Return type is unchanged, so this replaces in place.
--
-- ROLLBACK: re-run 20261022120000.
-- ---------------------------------------------------------------------------

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
  shared_concept_subjects text[],
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
      -- public + mutual now, and disclosure rides along: a mutual claim may
      -- lift a peer's score (below) but never reaches the label arrays.
      select distinct ccl.concept_id, c.subject_kind, c.disclosure
      from public.user_identity_claims c
      join public.claim_concept_links ccl on ccl.claim_id = c.id
      where c.user_id = p_user_id
        and c.dismissed_at is null
        and c.disclosure in ('public', 'mutual')
    ),
    peer_concepts as (
      select
        pc.user_id as peer_id,
        pcl.concept_id,
        pc.subject_kind,
        pc.disclosure
      from public.user_identity_claims pc
      join public.claim_concept_links pcl on pcl.claim_id = pc.id
      where pc.dismissed_at is null
        and pc.disclosure in ('public', 'mutual')
        and pc.user_id <> p_user_id
        and not public.lana_is_blocked(p_user_id, pc.user_id)
    ),
    shared as (
      select
        pp.peer_id,
        pp.concept_id,
        pp.subject_kind,
        -- Showable only when BOTH sides published it. Anything else scores in
        -- silence: this surface describes strangers, and a mutual claim is
        -- withheld from exactly them (get_peer_profile, 20261022120000).
        (cc.disclosure = 'public' and pp.disclosure = 'public') as both_public
      from peer_concepts pp
      join caller_concepts cc
        on cc.concept_id = pp.concept_id
       and cc.subject_kind = pp.subject_kind
    ),
    concept_scored as (
      select
        s.peer_id,
        (count(distinct (s.subject_kind, s.concept_id))
           filter (where s.both_public))::int          as shared_concept_count,
        count(distinct (s.subject_kind, s.concept_id))::int as ranked_concept_count,
        (
          select array_agg(x.label order by x.label)
          from (
            select distinct ic2.label, s2.subject_kind
            from shared s2
            join public.identity_concepts ic2 on ic2.id = s2.concept_id
            where s2.peer_id = s.peer_id
              and s2.both_public
            order by ic2.label
            limit 50
          ) x
        )                                           as concept_labels,
        (
          select array_agg(x.subject_kind::text order by x.label)
          from (
            select distinct ic2.label, s2.subject_kind
            from shared s2
            join public.identity_concepts ic2 on ic2.id = s2.concept_id
            where s2.peer_id = s.peer_id
              and s2.both_public
            order by ic2.label
            limit 50
          ) x
        )                                           as concept_subjects
      from shared s
      group by s.peer_id
    ),
    candidates as (
      select
        coalesce(cs.peer_id, ks.peer_id)         as peer_id,
        coalesce(cs.circle_bonus, 0)             as circle_bonus,
        cs.shared_place_ref,
        coalesce(ks.shared_concept_count, 0)     as shared_concept_count,
        coalesce(ks.ranked_concept_count, 0)     as ranked_concept_count,
        ks.concept_labels,
        ks.concept_subjects
      from circle_scored cs
      full outer join concept_scored ks on ks.peer_id = cs.peer_id
    ),
    scored as (
      select
        c.peer_id,
        (c.circle_bonus + c.ranked_concept_count)::int         as score,
        (case when c.circle_bonus = 3 then 3 else 0 end)::int  as same_place_bonus,
        (case when c.circle_bonus = 1 then 1 else 0 end)::int  as same_type_bonus,
        c.shared_concept_count,
        c.concept_labels,
        c.concept_subjects,
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
    coalesce(s.concept_labels,   '{}')            as shared_concept_labels,
    coalesce(s.concept_subjects, '{}')            as shared_concept_subjects,
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
