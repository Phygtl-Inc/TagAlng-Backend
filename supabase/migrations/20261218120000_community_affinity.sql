-- How well does THIS caller fit THIS community — and the cache for the sentence that
-- says why.
--
-- WHAT WAS MISSING
--   /lana/circles/discover answered "what is near me and how alive is it" (place, member
--   count, coarse distance) and nothing about the caller. Every row looked equally
--   relevant, so the panel ranked a gym the caller has nothing to do with beside the one
--   full of people who describe themselves exactly as she does.
--
--   Both halves of the fix need the same read, so it is ONE function: the 0-1 affinity
--   the card shows, and the evidence the "why Lana sees a fit" line is authored from. A
--   number computed from one set of facts and a sentence written from another is how a
--   card ends up saying "you both run" over a 12%.
--
-- THE ARMS — deliberately the peer matcher's, one level up
--   score_onion_candidates_for_user scores a PERSON by shared public concepts (+1 each),
--   same place (+3) and same circle type (+1). A community has no claims of its own, so
--   it is scored through its MEMBERS' claims — the same trick discover_communities_semantic
--   (20261215120000) uses to give a place a vector for free.
--
--     concepts  — public concepts the caller and a member BOTH hold. Proven overlap:
--                 the label is the caller's own word too, so putting it on the card
--                 discloses nothing a stranger could not already read on herself.
--     semantic  — best cosine between the caller's public self-claims and the members'.
--                 Reaches the overlap the concept table has no row for ("gymmer" /
--                 "Gym enthusiast").
--     type      — the caller already belongs to a community of this kind.
--
--   Components are returned SEPARATELY and un-weighted, exactly as the onion returns its
--   bonuses: the blend into 0-1 lives in app/community_affinity.py so it can be re-tuned
--   without a migration, and a card that shows a number can always name what built it.
--
-- SUBJECT AXIS. A shared concept counts only when both sides hold it for the SAME kind of
-- subject (20261021120000). The caller's child doing karate and a member doing karate
-- himself is not one thread, and a line that renders it as "you both" is a lie the chips
-- would repeat. Matching subjects ride back alongside the labels so the composer can say
-- "your kids both…" where that is what is true.
--
-- DISCLOSURE (§F). Counts and labels, never an identity — the caller learns that someone
-- at this place shares her words, not who. Public claims only on both sides: a mutual
-- claim is withheld from strangers everywhere else and a discovery panel is the
-- definition of strangers. subject_kind='self' on the semantic arm for the same reason
-- discover_communities_semantic filters it — without it a stranger is shown a community
-- because of somebody's child.
--
-- NO GEOGRAPHY. There is no distance term and no distance column here. Every row this
-- scores is already inside discover_communities_near's radius, so distance has been
-- spent; what is left to answer is fit. (The wire lost `distance_text` in the same change
-- — a coarse home-centroid phrase cannot be right for a user who has moved since.)

create or replace function public.score_community_affinity_for_user(
  p_user_id       uuid,
  p_place_ids     uuid[],
  p_max_my_claims int default 40
)
returns table (
  place_id                uuid,
  member_count            int,
  shared_concept_count    int,
  shared_concept_labels   text[],
  shared_concept_subjects text[],
  same_type               boolean,
  matched_type            text,
  semantic_similarity     real,
  my_label                text,
  member_label            text
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with targets as (
    select distinct t.pid
    from unnest(coalesce(p_place_ids, '{}'::uuid[])) as t(pid)
    where t.pid is not null
  ),
  visible as (
    -- The one definition of "member here" (20261103120000): confirmed, not dismissed,
    -- blocked users excluded. Never a second copy of the predicate.
    select vm.place_ref, vm.user_id, vm.circle_type
    from public.visible_place_members(p_user_id) vm
    join targets t on t.pid = vm.place_ref
  ),
  counted as (
    select v.place_ref as pid, count(distinct v.user_id)::int as members
    from visible v
    group by v.place_ref
  ),
  -- Everyone but the caller. Scoring a place against her own claims through her own
  -- membership would hand every community she is already in a perfect score.
  members as (
    select distinct v.place_ref, v.user_id
    from visible v
    where v.user_id <> p_user_id
  ),
  caller_types as (
    select distinct a.circle_type
    from public.circle_affiliations a
    where a.user_id = p_user_id
      and a.status = 'confirmed'
      and a.dismissed_at is null
      and a.circle_type is not null
  ),
  typed as (
    select v.place_ref as pid, min(v.circle_type) as matched_type
    from visible v
    join caller_types ct on ct.circle_type = v.circle_type
    where v.user_id <> p_user_id
    group by v.place_ref
  ),
  caller_concepts as (
    select distinct l.concept_id, c.subject_kind
    from public.user_identity_claims c
    join public.claim_concept_links l on l.claim_id = c.id
    where c.user_id = p_user_id
      and c.dismissed_at is null        -- mirrors score_onion_candidates_for_user
      and c.disclosure = 'public'
  ),
  member_concepts as (
    select m.place_ref, l.concept_id, c.subject_kind
    from members m
    join public.user_identity_claims c
      on c.user_id = m.user_id
     and c.dismissed_at is null
     and c.disclosure = 'public'
    join public.claim_concept_links l on l.claim_id = c.id
  ),
  shared as (
    -- Same concept AND same subject kind — see SUBJECT AXIS above.
    select distinct mc.place_ref, mc.concept_id, mc.subject_kind
    from member_concepts mc
    join caller_concepts cc
      on cc.concept_id = mc.concept_id
     and cc.subject_kind = mc.subject_kind
  ),
  shared_labelled as (
    select s.place_ref, ic.label, s.subject_kind::text as subject
    from shared s
    join public.identity_concepts ic on ic.id = s.concept_id
  ),
  concept_scored as (
    -- Both arrays ordered by label, so subjects[i] describes labels[i] — the same
    -- contract score_onion_candidates_for_user's subjects column carries.
    select
      sl.place_ref                                   as pid,
      count(*)::int                                  as n,
      (array_agg(sl.label   order by sl.label))[1:10] as labels,
      (array_agg(sl.subject order by sl.label))[1:10] as subjects
    from shared_labelled sl
    group by sl.place_ref
  ),
  my_claims as (
    select c.label, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.transient = false
      and c.disclosure = 'public'
      and c.subject_kind = 'self'
      and c.embedding is not null
    -- Newest first and capped: the cross join below is |my claims| x |member claims|,
    -- and a user with two hundred claims must not make a discovery panel slow.
    order by c.created_at desc
    limit greatest(1, least(coalesce(p_max_my_claims, 40), 100))
  ),
  member_claims as (
    select m.place_ref, c.label, c.embedding
    from members m
    join public.user_identity_claims c
      on c.user_id = m.user_id
     and c.dismissed_at is null
     and c.transient = false
     and c.disclosure = 'public'
     and c.subject_kind = 'self'
     and c.embedding is not null
  ),
  pairs as (
    select
      mc.place_ref,
      (1 - (mc.embedding <=> my.embedding))::real as sim,
      my.label                                    as my_label,
      mc.label                                    as member_label
    from member_claims mc
    cross join my_claims my
  ),
  sem as (
    select distinct on (p.place_ref) p.place_ref, p.sim, p.my_label, p.member_label
    from pairs p
    order by p.place_ref, p.sim desc
  )
  select
    t.pid,
    coalesce(c.members, 0),
    coalesce(cs.n, 0),
    coalesce(cs.labels,   '{}'::text[]),
    coalesce(cs.subjects, '{}'::text[]),
    (ty.pid is not null),
    ty.matched_type,
    sem.sim,
    sem.my_label,
    sem.member_label
  from targets t
  left join counted        c  on c.pid  = t.pid
  left join concept_scored cs on cs.pid = t.pid
  left join typed          ty on ty.pid = t.pid
  left join sem               on sem.place_ref = t.pid;
$$;

comment on function public.score_community_affinity_for_user(uuid, uuid[], int) is
  'How the caller overlaps with the MEMBERS of each given community: shared public '
  'concepts (label + subject kind, matched on both), whether she already belongs to a '
  'community of this type, and the best cosine between her public self-claims and '
  'theirs. Components are returned un-weighted — the 0-1 blend lives in '
  'app/community_affinity.py so it can be re-tuned without a migration. Counts and '
  'labels only; never a member identity. No geography: distance was already spent '
  'choosing which places to score.';

revoke all on function public.score_community_affinity_for_user(uuid, uuid[], int)
  from public, anon, authenticated;
grant execute on function public.score_community_affinity_for_user(uuid, uuid[], int)
  to service_role;


-- ----------------------------------------------------------------------------
-- The authored "why Lana sees a fit" block on a discovery row.
--
-- peer_rec_lines (20261119120000) holds the same shape for a NEIGHBOUR and keys on
-- peer_user_id -> users(id), so a community cannot live in it. Same columns, same
-- cache contract, subject swapped for the place: a reload costs no LLM call, and a new
-- overlap (basis_sig) authors a new line instead of serving a stale one.
--
-- No lana_feedback column yet — the discovery card has no thumb. Add one the way
-- 20261119120000 did if it grows one.
-- ----------------------------------------------------------------------------
create table if not exists public.community_fit_lines (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  place_ref uuid not null references public.places(id) on delete cascade,
  -- The language the line was AUTHORED in (the viewer's locale at the time). Part of
  -- the key: a Spanish reader must not be served the English line.
  lang text not null default 'en',
  -- Fingerprint of the overlap behind the line (app/peer_rec_line.py::_basis_sig).
  basis_sig text not null,
  line text not null,
  -- Short authored facets shown above the line. NULL = authored before chips existed
  -- (recompose); [] = the model found no honest facet.
  chips jsonb,
  created_at timestamptz not null default now(),
  constraint community_fit_lines_uq unique (user_id, place_ref, lang, basis_sig)
);

-- The read on every discovery fetch: this viewer's lines for a page of places at once.
create index if not exists community_fit_lines_user_idx
  on public.community_fit_lines (user_id, lang);

comment on table public.community_fit_lines is
  'AI-authored "why Lana sees a fit" line + chips for a community the caller could join, '
  'per viewer + place + overlap basis + language. Cached so a reload costs no LLM call.';

alter table public.community_fit_lines enable row level security;
-- No policies: the worker (service_role) is the only reader/writer, same as peer_rec_lines.

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.score_community_affinity_for_user(uuid, uuid[], int);
--   drop table if exists public.community_fit_lines;
-- ============================================================================
