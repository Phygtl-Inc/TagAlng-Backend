-- ---------------------------------------------------------------------------
-- Child-subject identity claims — "my 7-year-old does karate" is now a claim
-- ABOUT THE CHILD, not a fact discarded into users.kids_count.
-- ---------------------------------------------------------------------------
-- Until now every claim was implicitly about the caller, and the extractor was
-- told "never capture a child's name, age, or school". That threw away the
-- strongest neighbor signal there is (kids the same age, doing the same thing,
-- at the same school) — see docs/LANA_RELATIONAL_IDENTITY_CLAIMS.md §1.
--
-- Three additive columns carry the subject. Privacy contract, enforced here and
-- in claims_persist.py:
--   * subject_name is OWNER-ONLY. No peer-facing RPC selects it; the matcher
--     below never reads it; labels/quotes/synonyms still run through
--     redact_pii, so the name cannot ride into matchable text.
--   * subject_birth_year, not an age — an age is wrong within a year of being
--     written. Age is derived at read time.
--   * subject_kind is the full relation enum from the design doc even though
--     only 'self' and 'child' are written today; a second enum migration to add
--     'spouse' later costs more than the extra labels cost now.
-- ---------------------------------------------------------------------------

do $$
begin
  if not exists (select 1 from pg_type where typname = 'claim_subject_kind') then
    create type public.claim_subject_kind as enum (
      'self','child','parent','spouse','sibling','grandparent','household','other'
    );
  end if;
end
$$;

alter table public.user_identity_claims
  add column if not exists subject_kind public.claim_subject_kind not null default 'self',
  add column if not exists subject_name text,
  add column if not exists subject_birth_year smallint;

comment on column public.user_identity_claims.subject_kind is
  'Who the claim is about. Default self — every pre-existing row is about the caller.';
comment on column public.user_identity_claims.subject_name is
  'First name of the relation (a child), OWNER-ONLY. Never returned to another user, never used in matching.';
comment on column public.user_identity_claims.subject_birth_year is
  'Derived from a stated age at capture time, so the age stays correct as the year turns.';

alter table public.user_identity_claims
  drop constraint if exists user_identity_claims_subject_self_anonymous;
alter table public.user_identity_claims
  add constraint user_identity_claims_subject_self_anonymous
    check (subject_kind <> 'self' or (subject_name is null and subject_birth_year is null));

alter table public.user_identity_claims
  drop constraint if exists user_identity_claims_subject_birth_year_range;
alter table public.user_identity_claims
  add constraint user_identity_claims_subject_birth_year_range
    check (subject_birth_year is null or subject_birth_year between 1900 and 2100);

-- The old unique index was (user_id, concept): two children who both do karate
-- would collide on one row and the second would silently overwrite the first.
-- The identity of a claim is now (whose, which relation, which concept).
drop index if exists public.user_identity_claims_user_concept_active_idx;
create unique index if not exists user_identity_claims_user_subject_concept_active_idx
  on public.user_identity_claims (user_id, subject_kind, coalesce(subject_name, ''), concept)
  where dismissed_at is null;

create index if not exists user_identity_claims_subject_kind_idx
  on public.user_identity_claims (user_id, subject_kind)
  where dismissed_at is null and subject_kind <> 'self';

-- ---------------------------------------------------------------------------
-- Matcher: a concept only matches the SAME subject.
-- ---------------------------------------------------------------------------
-- "my kid does karate" and "I do karate" are different facts about different
-- people; scoring them as a shared concept produces a match reason that is
-- simply untrue ("you both do karate"). Both concept sets now carry
-- subject_kind and the join requires it on both sides. Body is otherwise
-- identical to 20261014120000 — see that file for the design notes.
--
-- Unchanged and still worth revisiting: both arms filter disclosure = 'public',
-- so a 'mutual' claim contributes nothing to ranking on either side.
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
      -- Distinct (subject, concept) pairs the caller holds via public, non-dismissed
      -- claims. subject_name is deliberately NOT selected — it never reaches matching.
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

-- ponytail: two sibling surfaces still pair claims on concept alone, so they can
-- still cross a parent's claim with a child's —
--   * count_shared_concepts_for_user (20260913, community_surface)
--   * get_peer_profile's shared_claim_count (20261018)
-- Both need the same `and c1.subject_kind = c2.subject_kind`. Left alone here
-- because each means re-emitting a large function body; do it when that surface
-- is next touched.
--
-- ALSO PENDING before a kid-based match reason can be SHOWN: the peer projection
-- exposes label but not subject_kind, so copy has no way to say "your kids both
-- do karate" and would fall back to "you both do karate" — untrue. Add
-- subject_kind to get_peer_profile's claim objects at that point.
