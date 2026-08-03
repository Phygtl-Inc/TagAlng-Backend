-- ============================================================================
-- PEER MATCHING BY RADIUS  (the people-side twin of 20260920120000)
--
-- PROBLEM
--   public.match_peers_by_claim_vectors scopes candidates with
--       where u.home_block_id = v_block_id
--   -- string equality on a block label. Two consequences, both live in prod:
--
--   1. A block boundary is a wall. On prod, "Lake Nona - Area A"
--      (8a2a1072b59ffff, 8 users) and "Lake Nona - Area B" (8a2a1072b5affff,
--      1 user) have centroids 0.70 km apart. Those 9 people can never be shown
--      to each other, while someone at the far edge of your own block can.
--      The app calls both areas "Lake Nona".
--   2. No home_block_id => the function returns zero rows immediately. 23 of 37
--      prod users are in that state, so "find neighbours" is empty for them
--      regardless of how much they have in common with someone.
--
--   The onion scorer (20260914120000) has no geographic term either, but it is
--   NOT the leak: onion_blend.py only lets a non-block candidate take a seat on
--   a PROVEN shared place. The geography lives here, in the equality filter.
--
-- THIS MIGRATION is additive: two new functions, nothing existing is altered.
-- match_peers_by_claim_vectors keeps its callers and its behaviour until the
-- worker is flipped over (LANA_PEER_RADIUS_MATCH, default off).
--
-- PRECISION IS BLOCK-LEVEL, DELIBERATELY. Users resolve to the centre point of
-- their block (or ZIP), never a street address -- the product stores coarse
-- points on purpose. Two people in the same block are therefore 0 m apart and
-- tie; ties are broken by match quality, which is what the scorer already does
-- well. Distance decides who is ELIGIBLE, not who ranks first.
--
-- See the ROLLBACK section at the foot of this file.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. user_origin_point(user) -> where do we consider this person to be?
--
-- Block centroid first, ZIP centroid second. This is the opposite order to
-- places.py::_centroid and resolve_search_origin(), and the difference is
-- deliberate: those resolve a search AREA, where a ZIP is the friendlier unit,
-- whereas this ranks PEOPLE and wants the finest point available. A zip-XXXXX
-- pseudo-block's centroid equals its ZIP centroid anyway, so block-first is
-- never worse -- only sharper for the H3 blocks.
--
-- Always returns exactly one row. origin_source='none' with a null point is a
-- normal answer (the 23 users above), not an error.
-- ----------------------------------------------------------------------------
create or replace function public.user_origin_point(p_user_id uuid)
returns table(
  origin extensions.geography,
  origin_source text,
  block_id text,
  zip5 text
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_block_id text;
  v_zip5 text;
  v_pt extensions.geography;
  v_src text := 'none';
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  select u.home_block_id, public.normalize_zip5(u.home_zip)
    into v_block_id, v_zip5
  from public.users u
  where u.id = p_user_id;

  if v_block_id is not null then
    select b.centroid into v_pt
    from public.blocks b
    where b.id = v_block_id and b.centroid is not null;
    if v_pt is not null then
      v_src := 'block';
    end if;
  end if;

  if v_pt is null and v_zip5 is not null then
    select extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      into v_pt
    from public.zip_centroids z
    where z.zip5 = v_zip5;
    if v_pt is not null then
      v_src := 'zip';
    end if;
  end if;

  return query select v_pt, v_src, v_block_id, v_zip5;
end;
$function$;

comment on function public.user_origin_point(uuid) is
  'Coarse point for a user: block centroid, else ZIP centroid, else null with '
  'origin_source=''none''. Block-first because this ranks people (finest point '
  'wins); resolve_search_origin is ZIP-first because it frames a search area.';

grant execute on function public.user_origin_point(uuid) to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 2. match_peers_within_radius(...) -> the radius twin of
--    match_peers_by_claim_vectors
--
-- Same cosine logic, same output columns, three differences:
--   (a) candidates are chosen by st_dwithin on the two coarse points instead of
--       home_block_id equality, so an adjacent-block neighbour is reachable and
--       a same-block stranger is not automatically preferred;
--   (b) blocked users are excluded -- match_peers_by_claim_vectors has NO
--       lana_is_blocked filter (grep: 0 occurrences), which is a real defect it
--       inherits from before the block feature existed. Not fixed in place here
--       because that function is still live for its current callers;
--   (c) distance_meters / distance_text come back so the caller can say "0.7 km
--       away" truthfully instead of implying same-block.
--
-- Ordering stays similarity-first with distance as the tiebreak. Distance
-- decides eligibility; it does not outrank a better match.
-- ----------------------------------------------------------------------------
create or replace function public.match_peers_within_radius(
  p_user_id uuid,
  p_radius_meters double precision default 8000,
  p_limit int default 20,
  p_min_similarity real default 0.65,
  p_locale text default 'en'
)
returns table(
  peer_user_id uuid,
  nickname text,
  avatar_url text,
  similarity_score real,
  matching_peer_label text,
  matching_peer_concept text,
  has_exact_concept_match boolean,
  distance_meters double precision,
  distance_text text
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_caller uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision := greatest(100, least(coalesce(p_radius_meters, 8000), 200000));
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;
  -- A user JWT may only match for itself; service role (auth.uid() null) is trusted.
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  select o.origin into v_origin from public.user_origin_point(p_user_id) o;

  -- No locatable home => no radius to search. Same graceful empty as the
  -- block-equality version returns for a null home_block_id.
  if v_origin is null then
    return;
  end if;

  return query
  with peer_points as (
    -- Every other user's coarse point, resolved with the same block-then-ZIP
    -- preference as user_origin_point. Inlined rather than called per row so
    -- the planner sees one join tree.
    select
      u.id as peer_id,
      coalesce(
        b.centroid,
        extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      ) as pt
    from public.users u
    left join public.blocks b
      on b.id = u.home_block_id and b.centroid is not null
    left join public.zip_centroids z
      on z.zip5 = public.normalize_zip5(u.home_zip)
    where u.id <> p_user_id
      and not public.lana_is_blocked(p_user_id, u.id)
  ),
  in_radius as (
    select
      pp.peer_id,
      extensions.st_distance(pp.pt, v_origin)::double precision as dist
    from peer_points pp
    where pp.pt is not null
      and extensions.st_dwithin(pp.pt, v_origin, v_radius)
  ),
  caller_claims as (
    select c.concept, c.label, c.embedding
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and c.embedding is not null
  ),
  peer_claim_pairs as (
    select
      pc.user_id as peer_id,
      pc.concept as peer_concept,
      pc.label as peer_label,
      (1 - (cc.embedding <=> pc.embedding))::real as sim
    from caller_claims cc
    join public.user_identity_claims pc
      on pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
     and pc.embedding is not null
    join in_radius ir on ir.peer_id = pc.user_id
    where (1 - (cc.embedding <=> pc.embedding)) >= p_min_similarity
  ),
  peer_best as (
    select distinct on (p.peer_id)
      p.peer_id,
      p.peer_concept,
      p.peer_label,
      p.sim
    from peer_claim_pairs p
    order by p.peer_id, p.sim desc
  ),
  exact_concepts as (
    select distinct pc.user_id as peer_id
    from public.user_identity_claims cc
    join public.user_identity_claims pc
      on pc.concept = cc.concept
     and pc.user_id <> p_user_id
     and pc.dismissed_at is null
     and pc.disclosure = 'public'
    join in_radius ir on ir.peer_id = pc.user_id
    where cc.user_id = p_user_id
      and cc.dismissed_at is null
      and cc.disclosure = 'public'
  )
  select
    pb.peer_id as peer_user_id,
    u.nickname,
    u.profile_photo_url as avatar_url,
    pb.sim as similarity_score,
    pb.peer_label as matching_peer_label,
    pb.peer_concept as matching_peer_concept,
    exists (select 1 from exact_concepts ec where ec.peer_id = pb.peer_id) as has_exact_concept_match,
    ir.dist as distance_meters,
    public.humanize_distance_text(ir.dist, p_locale) as distance_text
  from peer_best pb
  join in_radius ir on ir.peer_id = pb.peer_id
  join public.users u on u.id = pb.peer_id
  order by pb.sim desc, ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

comment on function public.match_peers_within_radius(uuid, double precision, int, real, text) is
  'Radius twin of match_peers_by_claim_vectors: candidates within '
  'p_radius_meters of the caller''s coarse point instead of the same block '
  'label, so an adjacent-block neighbour is reachable. Also excludes blocked '
  'users, which the block-equality original does not. Similarity-first order, '
  'distance as tiebreak.';

grant execute on function public.match_peers_within_radius(uuid, double precision, int, real, text)
  to authenticated, service_role;


-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
-- Both functions are new (verified: no collisions in prod's exposed RPCs before
-- this migration). Nothing existing is altered, and the worker only calls them
-- when LANA_PEER_RADIUS_MATCH is on, so a drop is clean in either order:
--
--   drop function if exists public.match_peers_within_radius(
--     uuid, double precision, int, real, text);
--   drop function if exists public.user_origin_point(uuid);
--
-- match_peers_within_radius depends on user_origin_point and on
-- humanize_distance_text (20260920120000) -- drop this pair before that one.
-- ============================================================================
