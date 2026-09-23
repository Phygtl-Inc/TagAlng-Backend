-- Community discovery becomes mappable: a point on every row, and a point to search FROM.
--
-- Reported 2026-09-23 as "Orlando has no communities on /map". It has dozens. The map was
-- asking the wrong question: discover_communities_near only ever searched a radius around
-- the CALLER'S OWN home/ZIP point, so a user whose home is not near Orlando gets none of
-- Orlando's communities no matter how many exist — silently, with an empty list that reads
-- as "nothing here" rather than "you asked about somewhere else".
--
-- Two gaps, both in this one function.
--
-- (a) NO COORDINATES ON THE WIRE. The function holds each place's geography in hand — it
--     computes distance_meters off it — and projects neither lat nor lng. So the client
--     forward-geocodes every row's `name, address` through Mapbox to find a point it
--     already had: one paid lookup per community per session, and a row Mapbox cannot
--     match is dropped instead of drawn at the location we knew all along.
--
-- (b) NO WAY TO ASK AROUND AN ARBITRARY POINT. The read took p_user_id and a radius and
--     centred on her home. There was no way to ask "what is near THIS point".
--
-- DISCLOSURE (§F). Neither part discloses anything new.
--   (a) is strictly LESS than the row already carries: place_address names the spot
--       precisely and distance_text already said how far it is from her. A point that
--       round-trips to an address we already printed tells a reader nothing further.
--   (b) discloses nothing at all — it only changes which point the radius is measured
--       from. Member identities stay out of this function, as they always were.
--
-- ============================================================================
-- WHY DROP AND RECREATE RATHER THAN ADD TWO DEFAULTED PARAMETERS
-- ============================================================================
-- Two reasons, either one sufficient.
--
--   1. `create or replace` cannot change a function's return type, and this adds lat/lng
--      to the RETURNS TABLE.
--
--   2. Adding parameters makes a NEW function rather than replacing the old one —
--      f(uuid, float8, int, text, text) and f(uuid, float8, int, text, text, float8,
--      float8) are different functions. Both would then match the worker's five-named-
--      argument call, and PostgREST would get `function is not unique` (42725) on every
--      discovery read. An overload left behind here is not dead code, it is an outage.
--
-- The body below is 20261103120000's, with the origin resolved up front and two columns
-- added. Nothing else about it changes.
-- ============================================================================

drop function if exists public.discover_communities_near(uuid, double precision, int, text, text);

create or replace function public.discover_communities_near(
  p_user_id        uuid,
  p_radius_meters  double precision default 8000,
  p_limit          int default 20,
  p_locale         text default 'en',
  p_query          text default null,
  p_lat            double precision default null,
  p_lng            double precision default null
) returns table (
  place_id         uuid,
  name             text,
  address          text,
  place_type       text,
  zip              text,
  -- The place's own geography, the same one `distance_meters` is computed from — so a
  -- point and the distance beside it can never disagree. Null for a place we have no
  -- coordinates for (an imported row, a creator community); the client drops those
  -- exactly as it drops an address Mapbox could not match today.
  lat              double precision,
  lng              double precision,
  member_count     int,
  member_types     text[],
  distance_meters  double precision,
  distance_text    text,
  is_member        boolean
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_origin extensions.geography;
  v_zip5   text;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  -- An explicit origin wins over her home point — that is the whole of (b). It is taken
  -- only as a COMPLETE, in-range pair: half a pair, or a swapped lat/lng that lands off
  -- the globe, is a caller bug, and centring the search on the equator because of it
  -- returns a plausible-looking empty list instead of her own neighbourhood. Falling
  -- back is the answer that stays true.
  if p_lat is not null and p_lng is not null
     and p_lat between -90 and 90
     and p_lng between -180 and 180 then
    v_origin := extensions.st_setsrid(
                  extensions.st_makepoint(p_lng, p_lat), 4326
                )::extensions.geography;
    -- v_zip5 deliberately stays NULL. The ZIP-equality arm below exists to rescue places
    -- we have no coordinates for by matching the CALLER'S HOME ZIP — which says nothing
    -- about whether they are near the point she is looking at. On a pinned search that
    -- arm would import her home town into somebody else's city. A coordinate-less place
    -- cannot be drawn on a map anyway, so nothing that the caller could have rendered is
    -- lost by leaving it out.
  else
    select o.origin, o.zip5 into v_origin, v_zip5
    from public.user_origin_point(p_user_id) o;
  end if;

  return query
  with visible_members as (
    -- The shared definition, not a second copy of the predicate (20261103120000).
    select vm.place_ref, vm.user_id, vm.circle_type
    from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select
      vm.place_ref                                        as pid,
      count(distinct vm.user_id)::int                     as members,
      array_agg(distinct vm.circle_type)                  as types,
      bool_or(vm.user_id = p_user_id)                     as mine
    from visible_members vm
    group by vm.place_ref
  ),
  located as (
    select
      c.pid, c.members, c.types, c.mine,
      p.name, p.address, p.place_type, p.zip,
      p.lat, p.lng,
      case
        when v_origin is null or p.lat is null or p.lng is null then null
        else extensions.st_distance(
               v_origin,
               extensions.st_setsrid(extensions.st_makepoint(p.lng, p.lat), 4326)::extensions.geography
             )
      end as meters
    from counted c
    join public.places p on p.id = c.pid
    where c.members > 0
      and (p_query is null or p_query = '' or p.name ilike '%' || p_query || '%')
  )
  select
    l.pid,
    l.name,
    l.address,
    l.place_type,
    l.zip,
    l.lat,
    l.lng,
    l.members,
    l.types,
    l.meters,
    public.humanize_distance_text(l.meters, p_locale),
    l.mine
  from located l
  where
    case
      -- With a point: everything inside the radius (a place with no coordinates
      -- can still qualify on ZIP equality, which is how imported rows behave —
      -- and only when the point IS her home, see v_zip5 above).
      when v_origin is not null then
        (l.meters is not null and l.meters <= p_radius_meters)
        or (l.meters is null and v_zip5 is not null and l.zip = v_zip5)
      -- No point at all: ZIP equality is the only honest scope left.
      else v_zip5 is not null and l.zip = v_zip5
    end
  order by l.members desc, coalesce(l.meters, 1e9) asc, l.name asc
  limit greatest(coalesce(p_limit, 20), 1);
end;
$function$;

comment on function public.discover_communities_near(uuid, double precision, int, text, text, double precision, double precision) is
  'Communities near a point that already have at least one visible confirmed member: '
  'place, its own lat/lng, member count, coarse distance, and is_member. The point is '
  'p_lat/p_lng when a complete in-range pair is given (what /map''s camera sends), else '
  'the caller''s home/ZIP centroid. Returns NO member identities (§F) — who is there is '
  'members-only, served by the worker. Places whose visible member count is zero (blocks) '
  'are omitted entirely. A pinned search does NOT fall back to the caller''s home ZIP: '
  'that arm rescues coordinate-less places near HER, which says nothing about the point '
  'she is looking at.';

grant execute on function public.discover_communities_near(uuid, double precision, int, text, text, double precision, double precision)
  to service_role;

-- ============================================================================
-- ROLLBACK
--   Restore the five-argument function from 20261103120000 verbatim, after dropping this
--   one — the same not-unique hazard applies in reverse, so the drop is not optional:
--
--     drop function if exists public.discover_communities_near(
--       uuid, double precision, int, text, text, double precision, double precision);
--     -- then re-run section 2 of 20261103120000_place_local_signal.sql.
--
--   The worker tolerates the old shape on its own: it reads lat/lng off each row with
--   .get(), so a row without them serialises as null and the client drops it, which is
--   precisely today's behaviour. p_lat/p_lng would 404 at PostgREST, which
--   discover_communities already catches into an empty list — so roll the worker back
--   with it rather than leaving discovery dark.
-- ============================================================================
