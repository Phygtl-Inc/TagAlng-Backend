-- ---------------------------------------------------------------------------
-- Make the SUBJECT of a shared claim visible to the surfaces that describe it.
-- ---------------------------------------------------------------------------
-- 20261021120000 taught the matcher that a child's karate and an adult's karate
-- are different facts. The surfaces that TURN a match into a sentence never
-- learned it: they receive a bare label ("Does karate") and write "You both do
-- karate" — which, for two parents whose KIDS do karate, is a false statement
-- about the adults reading it.
--
-- Three changes, all additive to the caller:
--   1. count_shared_concepts_for_user + score_onion_candidates_for_user pair on
--      (concept, subject) and return shared_concept_subjects — one entry per
--      label, same order, so copy can say "your kids both" instead of "you both".
--      Both are dropped and recreated: the return TABLE gains a column, and
--      create-or-replace cannot change a function's result type.
--   2. get_peer_profile puts subject_kind on every claim it projects, so a peer
--      card can group "their kids" apart from "them". The name is NOT projected
--      and never will be — subject_name stays owner-only.
--   3. get_peer_profile.shared_claim_count pairs on subject too, matching (1).
--
-- ROLLBACK: re-run 20261021120000 (functions 1) and 20261018120000 (function 2).
-- ---------------------------------------------------------------------------

drop function if exists public.count_shared_concepts_for_user(uuid, int, int, boolean);

create function public.count_shared_concepts_for_user(
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
  shared_concept_subjects text[],
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
    select distinct ccl.concept_id, c.subject_kind
    from public.user_identity_claims c
    join public.claim_concept_links ccl on ccl.claim_id = c.id
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
  ),
  peer_concepts as (
    select
      pc.user_id as peer_id,
      pcl.concept_id,
      pc.subject_kind
    from public.user_identity_claims pc
    join public.claim_concept_links pcl on pcl.claim_id = pc.id
    where pc.dismissed_at is null
      and pc.disclosure = 'public'
      and (
        not p_exclude_self
        or pc.user_id <> p_user_id
      )
  ),
  shared as (
    -- A concept counts only when BOTH sides hold it about the same kind of
    -- person: my kid's karate does not overlap your karate.
    select
      pp.peer_id,
      pp.concept_id,
      pp.subject_kind
    from peer_concepts pp
    join caller_concepts cc
      on cc.concept_id = pp.concept_id
     and cc.subject_kind = pp.subject_kind
  ),
  aggregated as (
    select
      s.peer_id,
      count(distinct (s.subject_kind, s.concept_id))::int as shared_count,
      -- labels and subjects are aggregated over ONE ordered subquery, so index i
      -- of each array describes the same shared concept.
      (
        select array_agg(x.label order by x.label)
        from (
          select distinct ic2.label, s2.subject_kind
          from shared s2
          join public.identity_concepts ic2 on ic2.id = s2.concept_id
          where s2.peer_id = s.peer_id
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
          order by ic2.label
          limit 50
        ) x
      )                                           as concept_subjects,
      array_agg(distinct s.concept_id order by s.concept_id) as concept_ids
    from shared s
    group by s.peer_id
    having count(distinct (s.subject_kind, s.concept_id)) >= p_min_shared_count
  )
  select
    a.peer_id                                     as peer_user_id,
    u.nickname,
    u.profile_photo_url                           as avatar_url,
    a.shared_count                                as shared_concept_count,
    coalesce(a.concept_labels,   '{}')            as shared_concept_labels,
    coalesce(a.concept_subjects, '{}')            as shared_concept_subjects,
    coalesce(a.concept_ids,      '{}'::uuid[])    as shared_concept_ids
  from aggregated a
  join public.users u on u.id = a.peer_id
  order by a.shared_count desc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

comment on function public.count_shared_concepts_for_user(uuid, int, int, boolean) is
  'Peers sharing >= p_min_shared_count public, non-dismissed identity concepts HELD ABOUT '
  'THE SAME SUBJECT (self-to-self, child-to-child). shared_concept_subjects[i] names the '
  'subject of shared_concept_labels[i]. No embeddings; exact concept_id match.';

grant execute on function public.count_shared_concepts_for_user(uuid, int, int, boolean) to service_role;
revoke execute on function public.count_shared_concepts_for_user(uuid, int, int, boolean) from public, authenticated, anon;

-- ---------------------------------------------------------------------------
-- score_onion_candidates_for_user — same subject pairing (already in place from
-- 20261021120000), now also reporting WHOSE the shared concepts are.
-- ---------------------------------------------------------------------------

drop function if exists public.score_onion_candidates_for_user(uuid, int, int);

create function public.score_onion_candidates_for_user(
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
      select distinct ccl.concept_id, c.subject_kind
      from public.user_identity_claims c
      join public.claim_concept_links ccl on ccl.claim_id = c.id
      where c.user_id = p_user_id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ),
    peer_concepts as (
      select
        pc.user_id as peer_id,
        pcl.concept_id,
        pc.subject_kind
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
        pp.concept_id,
        pp.subject_kind
      from peer_concepts pp
      join caller_concepts cc
        on cc.concept_id = pp.concept_id
       and cc.subject_kind = pp.subject_kind
    ),
    concept_scored as (
      select
        s.peer_id,
        count(distinct (s.subject_kind, s.concept_id))::int as shared_concept_count,
        (
          select array_agg(x.label order by x.label)
          from (
            select distinct ic2.label, s2.subject_kind
            from shared s2
            join public.identity_concepts ic2 on ic2.id = s2.concept_id
            where s2.peer_id = s.peer_id
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
        ks.concept_labels,
        ks.concept_subjects
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

-- ---------------------------------------------------------------------------
-- get_peer_profile — subject_kind on every projected claim, and a shared count
-- that pairs on subject. Body is 20261018120000 verbatim apart from those two
-- edits; diff the files if in doubt. subject_name is NOT projected.
-- ---------------------------------------------------------------------------

create or replace function public.get_peer_profile(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  caller uuid := auth.uid();
  peer record;
  is_matched boolean;
  location_label text;
  location_precision text;
  result jsonb;
begin
  -- Fetch peer profile
  select u.id, u.nickname, u.profile_photo_url, u.home_block_id, u.home_location_visibility,
         b.display_name, b.cluster_id
  into peer
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  -- Anonymous visitor: blurred profile (no sensitive data)
  if caller is null then
    return jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_blurred', true,
      'is_matched', false,
      'public_claims', '[]'::jsonb,
      'mutual_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'location_label', null,
      'location_precision', null,
      'block_name', null,
      'upcoming_shared_events', '[]'::jsonb,
      'communities', '[]'::jsonb
    );
  end if;

  -- Choose label based on peer preference
  location_label := case peer.home_location_visibility
    when 'block' then peer.display_name
    when 'cluster' then peer.cluster_id
  end;
  location_precision := peer.home_location_visibility::text;

  -- Check if caller and peer are matched (same event or shared public claims)
  is_matched := public.are_users_matched(caller, p_user_id);

  -- Build authenticated response
  result := jsonb_build_object(
    'user_id', peer.id,
    'nickname', peer.nickname,
    'avatar_url', peer.profile_photo_url,
    'is_blurred', false,
    'is_matched', is_matched,
    -- Always show public claims
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence,
        -- NEW: what kind of thread it is, and when it was learned.
        'bucket', c.bucket,
        'subject_kind', c.subject_kind,
        'created_at', c.created_at
      ) order by c.confidence desc)
      from public.user_identity_claims c
      where c.user_id = peer.id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ), '[]'::jsonb),
    -- Show mutual claims only if matched (silent omission otherwise)
    'mutual_claims', case
      when is_matched then
        coalesce((
          select jsonb_agg(jsonb_build_object(
            'concept', c.concept,
            'label', c.label,
            'tone', c.tone,
            'confidence', c.confidence,
            'bucket', c.bucket,
        'subject_kind', c.subject_kind,
            'created_at', c.created_at
          ) order by c.confidence desc)
          from public.user_identity_claims c
          where c.user_id = peer.id
            and c.dismissed_at is null
            and c.disclosure = 'mutual'
        ), '[]'::jsonb)
      else '[]'::jsonb
    end,
    'shared_claim_count', (
      select count(*)::int
      from public.user_identity_claims c1
      join public.user_identity_claims c2
        on c1.concept = c2.concept
       and c1.subject_kind = c2.subject_kind
      where c1.user_id = caller
        and c2.user_id = peer.id
        and c1.dismissed_at is null
        and c2.dismissed_at is null
        and c1.disclosure = 'public'
        and c2.disclosure = 'public'
    ),
    'location_label', location_label,
    'location_precision', location_precision,
    'block_name', case when peer.home_location_visibility = 'block' then peer.display_name else null end,
    -- home_block_id is HIDDEN from peer views (only owner sees it via get_my_profile)
    'upcoming_shared_events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', e.id,
        'title', e.title,
        'starts_at', e.starts_at
      ) order by e.starts_at asc)
      from public.events e
      where e.status = 'open'
        and e.starts_at > now()
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = caller
            and r.status in ('approved', 'attended')
        )
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = peer.id
            and r.status in ('approved', 'attended')
        )
    ), '[]'::jsonb),
    -- The peer's communities, disclosed by the two gates described in 20261011.
    -- Places the viewer also belongs to lead the list (those are the useful rows).
    'communities', coalesce((
      select jsonb_agg(jsonb_build_object(
        'id', c.id,
        -- NEW: our places.id, on the rows whose name travels. Null on a locked row.
        'place_id', c.place_id,
        'circle_type', c.circle_type,
        'shared', c.shared,
        'place_name', c.place_name,
        'detail', c.detail,
        'sub_groups', c.sub_groups
      ) order by c.shared desc, c.created_at asc)
      from (
        select
          a.id,
          a.circle_type,
          a.created_at,
          (mine.id is not null) as shared,
          case when is_matched or mine.id is not null then a.place_ref end as place_id,
          case when is_matched or mine.id is not null
            then coalesce(p.name, a.place_name) end as place_name,
          case when is_matched then a.detail end as detail,
          case when is_matched or mine.id is not null then coalesce((
            select jsonb_agg(pa.label order by pa.created_at asc)
            from public.place_activities pa
            where pa.place_id = a.place_ref
              and pa.user_id = peer.id
          ), '[]'::jsonb) else '[]'::jsonb end as sub_groups
        from public.circle_affiliations a
        join public.places p on p.id = a.place_ref
        left join public.circle_affiliations mine
          on mine.place_ref = a.place_ref
         and mine.user_id = caller
         and mine.status = 'confirmed'
         and mine.dismissed_at is null
        where a.user_id = peer.id
          and a.status = 'confirmed'
          and a.dismissed_at is null
        order by (mine.id is not null) desc, a.created_at asc
        limit 12
      ) c
    ), '[]'::jsonb)
  );

  return result;
end;
$$;

comment on function public.get_peer_profile(uuid) is
  'Returns peer profile with disclosure-aware claims. Shows public claims to all, mutual '
  'claims only to matched users; each claim carries its bucket (category) and created_at '
  '(when Lana learned it). Returns location label based on peer''s chosen precision '
  'and hides home_block_id. `communities` lists the peer''s confirmed grounded '
  'affiliations: the place NAME and place_id (and their activities there) only when the '
  'pair is matched or the viewer belongs to the same place, their private detail only '
  'when matched — otherwise the row is its kind alone.';

grant execute on function public.get_peer_profile(uuid) to authenticated, anon;
