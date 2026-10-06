-- ============================================================================
-- 20270114120000_peer_match_near_point.sql
--
-- PEER MATCHING AROUND A PIN (backend-asks §36, issues #100)
--
-- WHY
--   /lana/fellows can only search around where the account says she LIVES:
--   match_peers_within_radius (20260921120000, re-pointed by 20260922120000)
--   takes its origin from user_origin_point(p_user_id) — her home block
--   centroid, else her ZIP centroid. The PWA's search scope has a third option,
--   "around me right now", backed by a real device fix, and the fellows list
--   ignored it.
--
-- WHAT (purely additive — no existing function is touched)
--   1. peers_within_radius_of_point(user, lat, lng, radius)
--        The candidate query of peers_within_radius (20260922120000), copied
--        verbatim except for the origin: the supplied point instead of
--        user_origin_point(). Peers are still placed by THEIR coarse point
--        (block centroid, else ZIP centroid) — a pin moves the caller, never
--        reveals anyone else's real position.
--   2. match_peers_near_point(user, lat, lng, radius, limit, min_sim, locale)
--        match_peers_within_radius's current body (20260922120000), verbatim,
--        with in_radius read from (1). Same return shape, so the worker's row
--        shaping is shared; distance_meters / distance_text are measured from
--        the pin.
--
--   New functions rather than an extra defaulted parameter on the existing
--   pair: adding (p_lat, p_lng) to peers_within_radius(uuid, float8) would
--   create an overload that makes every existing 2-argument call ambiguous.
--   Keeping them separate also keeps a no-pin call byte-identical.
--
-- SECURITY
--   Internal-only, like the pair they mirror (20261231120000 §B): the worker
--   calls them on service_client() with the authenticated caller's id. Revoked
--   from public, anon AND authenticated (revoke-from-public alone is a no-op on
--   Supabase). The IDOR guard is kept for any JWT-bearing caller anyway.
--
-- ROLLBACK
--   drop function if exists public.match_peers_near_point(
--     uuid, double precision, double precision, double precision, int, real, text);
--   drop function if exists public.peers_within_radius_of_point(
--     uuid, double precision, double precision, double precision);
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. peers_within_radius_of_point
-- ----------------------------------------------------------------------------
create or replace function public.peers_within_radius_of_point(
  p_user_id uuid,
  p_lat double precision,
  p_lng double precision,
  p_radius_meters double precision default 8000
)
returns table(peer_id uuid, distance_meters double precision)
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
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  -- An unusable pin is an empty answer, never an error and never a silent
  -- fall-back to home: the caller asked about THIS point.
  if p_lat is null or p_lng is null
     or p_lat not between -90 and 90
     or p_lng not between -180 and 180 then
    return;
  end if;

  v_origin := extensions.st_setsrid(
    extensions.st_makepoint(p_lng, p_lat), 4326
  )::extensions.geography;

  return query
  select
    u.id,
    extensions.st_distance(
      coalesce(
        b.centroid,
        extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      ),
      v_origin
    )::double precision
  from public.users u
  left join public.blocks b
    on b.id = u.home_block_id and b.centroid is not null
  left join public.zip_centroids z
    on z.zip5 = public.normalize_zip5(u.home_zip)
  where u.id <> p_user_id
    and not public.lana_is_blocked(p_user_id, u.id)
    and coalesce(
          b.centroid,
          extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
        ) is not null
    and extensions.st_dwithin(
          coalesce(
            b.centroid,
            extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
          ),
          v_origin,
          v_radius
        );
end;
$function$;

comment on function public.peers_within_radius_of_point(uuid, double precision, double precision, double precision) is
  'peers_within_radius with the origin supplied (a device pin) instead of the '
  'caller''s home point. Peers are still placed by their own coarse point.';

revoke all on function public.peers_within_radius_of_point(uuid, double precision, double precision, double precision)
  from public, anon, authenticated;
grant execute on function public.peers_within_radius_of_point(uuid, double precision, double precision, double precision)
  to service_role;


-- ----------------------------------------------------------------------------
-- 2. match_peers_near_point
-- ----------------------------------------------------------------------------
create or replace function public.match_peers_near_point(
  p_user_id uuid,
  p_lat double precision,
  p_lng double precision,
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
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;
  if v_caller is not null and v_caller <> p_user_id then
    raise exception 'forbidden' using errcode = 'P0001';
  end if;

  return query
  with in_radius as (
    select r.peer_id, r.distance_meters as dist
    from public.peers_within_radius_of_point(p_user_id, p_lat, p_lng, p_radius_meters) r
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
      p.peer_id, p.peer_concept, p.peer_label, p.sim
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
    pb.peer_id,
    u.nickname,
    u.profile_photo_url,
    pb.sim,
    pb.peer_label,
    pb.peer_concept,
    exists (select 1 from exact_concepts ec where ec.peer_id = pb.peer_id),
    ir.dist,
    public.humanize_distance_text(ir.dist, p_locale)
  from peer_best pb
  join in_radius ir on ir.peer_id = pb.peer_id
  join public.users u on u.id = pb.peer_id
  order by pb.sim desc, ir.dist asc, u.nickname asc nulls last
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

comment on function public.match_peers_near_point(uuid, double precision, double precision, double precision, int, real, text) is
  'match_peers_within_radius anchored on a supplied point (POST /lana/fellows '
  'lat/lng) instead of the caller''s home. Same rows, distance from the pin.';

revoke all on function public.match_peers_near_point(uuid, double precision, double precision, double precision, int, real, text)
  from public, anon, authenticated;
grant execute on function public.match_peers_near_point(uuid, double precision, double precision, double precision, int, real, text)
  to service_role;
