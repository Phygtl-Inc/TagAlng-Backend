-- The ZIP-equality arm now requires an address.
--
-- PAIR WITH 20261214120000. That migration drops places_creator_has_no_geography, whose
-- job was to keep coordinate-less communities out of geographic discovery. This one does
-- the same job properly: at the source, for every row, with no reference to place_type.
--
-- THE ARM, AND WHAT IT IS FOR
--   discover_communities_near matches "inside the radius OR (no coordinates AND the
--   place's ZIP equals the caller's home ZIP)". The second arm exists — per its own
--   comment — because "a place with no coordinates can still qualify on ZIP equality,
--   which is how imported rows behave". An imported row is a real street address that
--   simply has not been geocoded yet.
--
--   A community with NO ADDRESS was never that arm's subject. It is not a place that we
--   failed to geocode; it is a place that is not anywhere. Requiring an address
--   distinguishes the two exactly, and needs to know nothing about what kind of
--   community it is.
--
-- WHAT CHANGES
--   One predicate. `and l.address is not null` is added to the no-coordinates arm in both
--   branches of the CASE. l.address is already selected in `located`. Everything else in
--   this function — the explicit-origin handling, v_zip5 suppression on a pinned search,
--   the visible-member predicate, ordering, limits — is unchanged and reproduced verbatim.
--
-- AFTER THIS
--   A community appears on the map if and only if it has lat/lng.
--   A community appears in "near me" if it has lat/lng in range, or an address in the
--   caller's home ZIP.
--   hq_city puts a community NOWHERE. It is a label (see 20261214120000).

create or replace function public.discover_communities_near(
  p_user_id       uuid,
  p_radius_meters double precision default 8000,
  p_limit         integer default 20,
  p_locale        text default 'en'::text,
  p_query         text default null::text,
  p_lat           double precision default null::double precision,
  p_lng           double precision default null::double precision
)
returns table(
  place_id uuid, name text, address text, place_type text, zip text,
  lat double precision, lng double precision, member_count integer,
  member_types text[], distance_meters double precision, distance_text text,
  is_member boolean
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
      -- With a point: everything inside the radius. A place with no coordinates can
      -- still qualify on ZIP equality — but ONLY if it has an address (20261215120000).
      -- That arm rescues imported rows awaiting a geocode; a community with no address
      -- is not un-geocoded, it is not anywhere, and it must not surface as "near me".
      when v_origin is not null then
        (l.meters is not null and l.meters <= p_radius_meters)
        or (l.meters is null and v_zip5 is not null and l.zip = v_zip5
            and l.address is not null)
      -- No point at all: ZIP equality is the only honest scope left — same address rule.
      else v_zip5 is not null and l.zip = v_zip5 and l.address is not null
    end
  order by l.members desc, coalesce(l.meters, 1e9) asc, l.name asc
  limit greatest(coalesce(p_limit, 20), 1);
end;
$function$;

comment on function public.discover_communities_near(uuid, double precision, integer, text, text, double precision, double precision) is
  'Communities near the caller. On the map if lat/lng; in "near me" if lat/lng in range '
  'OR an address in her home ZIP. A community with no address never surfaces here — that '
  'is what keeps coordinate-less communities (creator communities, and any other) out of '
  'geographic discovery, replacing places_creator_has_no_geography (20261214120000).';

-- ============================================================================
-- ROLLBACK
--   create-or-replace back to the 20260920120000-lineage body by removing the two
--   `and l.address is not null` clauses. Strictly more permissive; no data changes.
-- ============================================================================
