-- Finding a community that is not anywhere.
--
-- THE HOLE THIS FILLS
--   places_creator_has_no_geography (20261207120000) guarantees a creator community has no
--   lat, lng, zip or h3. discover_communities_near matches on "inside the radius OR (no
--   coordinates AND the place's ZIP equals mine)". A creator community satisfies neither
--   arm, so it is excluded BY CONSTRUCTION — which is intended, and which left it with no
--   discovery path inside the product at all. 20261207120000 says as much: members arrive
--   "through a link in a creator's bio".
--
--   There are two such rows in prod today and nobody inside the app can find either.
--
-- THE APPROACH — deliberately the same one as find_places_by_activity_semantic
--   That function solved the sibling problem (a library never matches the word "read") by
--   scoring a place through its MEMBERS' claim embeddings rather than its own name. The
--   place inherits a vector for free: no new column, no backfill, no second embed path.
--
--   Same trick here, one level up. "Iron Man Training" contains nothing about triathlon,
--   endurance or 5am swims — but the people in it do. So a community is matched on what its
--   members say they are, which is also the only honest description of a topic community.
--
-- WHY A SEPARATE FUNCTION RATHER THAN A THIRD ARM ON discover_communities_near
--   Because the two answer different questions and must be allowed to disagree. "Near me"
--   is a claim about geography and must stay false for a creator community forever. "Like
--   me" is a claim about content. Folding them together is how a global topic community
--   ends up rendered beside a neighbour's gym under the same heading — and a caller that
--   wants both can call both and label each section honestly.
--
-- WHAT THIS DOES NOT READ
--   hq_city / hq_lat / hq_lng (20261212120000). They are a map pin and a label. Using them
--   to decide what is discoverable would re-create the leak the constraint prevents. There
--   is no distance in this function and there is no ORDER BY distance. Keep it that way.

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
  'No geography is read and none is returned — hq_city rides along as a LABEL for the card, '
  'never as a distance. Counts and one matched label only; never member identities.';

revoke all on function public.discover_communities_semantic(uuid, extensions.vector, real, int, boolean)
  from public, anon;
grant execute on function public.discover_communities_semantic(uuid, extensions.vector, real, int, boolean)
  to authenticated, service_role;

-- ============================================================================
-- NOTES FOR THE CALLER
--   * Embed the user's words, not a category. "people who do long-distance triathlon"
--     beats "sports" for the same reason find_places_by_activity_semantic embeds the ask.
--   * p_min_similarity 0.55 mirrors find_places_by_activity_semantic's default and is a
--     STARTING VALUE on this surface — claim-to-ask is a different distribution from
--     activity-label-to-ask. Tune it against real queries before trusting it; it is a
--     function default precisely so that needs no deploy.
--   * A community with one member and one strong claim will surface. That is correct at
--     our density — one real person to join beats an empty list — but it means the card
--     must show member_count honestly rather than implying a crowd.
--
-- ROLLBACK
--   create or replace only; no table touched, nothing else calls it yet.
--     drop function if exists public.discover_communities_semantic(uuid, extensions.vector, real, int, boolean);
-- ============================================================================
