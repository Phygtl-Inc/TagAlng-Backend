-- A creator community you can actually draw: the HQ point rides the semantic read.
--
-- 20261215120000 built the only discovery path a creator community has — matched on what
-- its MEMBERS say about themselves, because "Iron Man Training" contains nothing about
-- triathlon but the people in it do. It returns hq_city as a label and deliberately stops
-- there, with a note at the top saying why:
--
--     WHAT THIS DOES NOT READ
--       hq_city / hq_lat / hq_lng (20261214120000). They are a map pin and a label.
--       Using them TO DECIDE WHAT IS DISCOVERABLE would re-create the leak the constraint
--       prevents.
--
-- That rule is about the PREDICATE, and it is kept here exactly as written: hq_lat/hq_lng
-- appear in the return list and nowhere else. No WHERE arm reads them, no ORDER BY reads
-- them, and there is still no distance in this function. Which communities come back is
-- decided by similarity and member count alone, precisely as before — this only lets a
-- caller that already received the row put a pin on it instead of a line of text.
--
-- The distinction is the same one 20261214120000 draws in its own header: "Rendering is a
-- separate concern from discovery and is allowed to read them. Discovery is not."
--
-- WHY hq_lat/hq_lng AND NOT lat/lng
--   A creator community has neither, by constraint, and must keep having neither — a point
--   in places.lat/lng is a point discover_communities_near measures a radius against, and
--   every follower within 8km of the creator's home city would start seeing a global topic
--   community under Communities Near Me. hq_lat/hq_lng are separate columns for exactly
--   this: "run from here", not "here". A client draws them as such and never as a distance.

drop function if exists public.discover_communities_semantic(
  uuid, extensions.vector, real, int, boolean);

create or replace function public.discover_communities_semantic(
  p_user_id          uuid,
  p_query_embedding  extensions.vector(768),
  p_min_similarity   real default 0.55,
  p_limit            int  default 5,
  p_creator_only     boolean default true
)
returns table (
  place_id        uuid,
  name            text,
  place_type      text,
  hq_city         text,
  -- Display-only, and null until something geocodes hq_city. Both or neither, guaranteed
  -- upstream by places_hq_point_complete — a half-populated pin renders at the equator.
  hq_lat          double precision,
  hq_lng          double precision,
  member_count    int,
  is_member       boolean,
  matched_label   text,
  similarity      real
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with visible as (
    -- The one definition of "member here" (20261103120000): confirmed, not dismissed,
    -- blocked users excluded. Never a second copy of the predicate.
    select vm.place_ref, vm.user_id
    from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select
      v.place_ref                      as pid,
      count(distinct v.user_id)::int   as members,
      bool_or(v.user_id = p_user_id)   as mine
    from visible v
    group by v.place_ref
  ),
  -- A member's own public claim, scored against the ask. Public only: a mutual claim is
  -- withheld from strangers everywhere else in this codebase and a discovery surface is
  -- the definition of strangers.
  --
  -- subject_kind='self' is what makes "own" true. Claims carry a subject axis
  -- (20261021120000): "my kid does karate" is a claim the member HOLDS but is not about
  -- them, and `label` here becomes the card's public proof line — so without this filter a
  -- stranger searching for karate is shown a community because of somebody's child.
  scored as (
    select
      v.place_ref                                        as pid,
      c.label                                            as label,
      (1 - (c.embedding <=> p_query_embedding))::real    as sim
    from visible v
    join public.user_identity_claims c
      on c.user_id = v.user_id
     and c.dismissed_at is null
     and c.transient = false
     and c.disclosure = 'public'
     and c.subject_kind = 'self'
     and c.embedding is not null
  ),
  best as (
    select distinct on (s.pid) s.pid, s.label, s.sim
    from scored s
    where s.sim >= p_min_similarity
    order by s.pid, s.sim desc
  )
  select
    b.pid,
    p.name,
    p.place_type,
    p.hq_city,
    p.hq_lat,
    p.hq_lng,
    c.members,
    coalesce(c.mine, false),
    b.label,
    b.sim
  from best b
  join counted c            on c.pid = b.pid
  join public.places p      on p.id  = b.pid
  where c.members > 0
    -- Default true: this exists for the communities geography cannot reach. Pass false to
    -- run it over every community — useful for "communities like this one", not for a
    -- surface that already has discover_communities_near.
    and (not p_creator_only or p.place_type = 'creator')
  order by b.sim desc, c.members desc, p.name asc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
$$;

comment on function public.discover_communities_semantic(uuid, extensions.vector, real, int, boolean) is
  'Communities whose MEMBERS describe themselves like the ask, scored on the public claim '
  'embeddings we already store. The only discovery path a creator community has: it is '
  'excluded from discover_communities_near by construction (places_creator_has_no_geography). '
  'hq_city/hq_lat/hq_lng ride along for RENDERING — a label and a map pin, never a distance, '
  'never a predicate: no WHERE or ORDER BY in this function reads them. Counts and one '
  'matched label only; never member identities.';

revoke all on function public.discover_communities_semantic(uuid, extensions.vector, real, int, boolean)
  from public, anon;
grant execute on function public.discover_communities_semantic(uuid, extensions.vector, real, int, boolean)
  to authenticated, service_role;

-- ============================================================================
-- ROLLBACK
--   Drop this and re-run 20261215120000's function verbatim. The drop is required rather
--   than optional: `create or replace` cannot change a return type, and leaving both
--   shapes present is not possible for this one (identical argument list), so a bare
--   re-run would fail rather than silently overload. The worker reads hq_lat/hq_lng with
--   .get(), so the older shape degrades to a row with no pin rather than an error.
--
--     drop function if exists public.discover_communities_semantic(
--       uuid, extensions.vector, real, int, boolean);
--     -- then re-run 20261215120000_discover_communities_semantic.sql.
-- ============================================================================
