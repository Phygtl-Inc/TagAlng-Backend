-- Which of these Google places are already OUR communities?
--
-- A tip_seek that no neighbor has vouched for falls back to Google Places, and every
-- row came back as a stranger's listing — even when four neighbors belong to that exact
-- spot and one of them said what they do there. The membership fact is weaker proof than
-- a posted recommendation (nobody wrote "go here"), but far stronger than a rating from
-- someone who has never been on this block, and we already hold it.
--
-- Google rows carry a google_place_id and places.google_place_id is unique, so this is a
-- lookup, not a search: one call stamps a whole result page.
--
-- Counted members match discover_communities_near exactly (confirmed, not dismissed,
-- blocked users excluded) so the two surfaces can never disagree about how alive a place
-- is. Activity labels are joined THROUGH the same visible set — a blocked member's
-- activities must not leak in via the label list.

-- ----------------------------------------------------------------------------
-- 0. visible_place_members — ONE definition of "who counts as a member here".
--
-- discover_communities_near already carried this predicate inline. Adding a second
-- surface meant either writing it twice or extracting it, and a second copy is how the
-- rules in this repo rot: the classifier prompt said "plays/likes are not requirements"
-- while a Python word list said otherwise, and peer search silently returned nobody for
-- a year of phrasings. Same shape as peers_within_radius (20260922120000), for the same
-- reason: "who counts as near me" has one definition, so five callers cannot drift.
--
-- No data is copied anywhere — every caller reads live rows through this.
-- ----------------------------------------------------------------------------
create or replace function public.visible_place_members(p_user_id uuid)
returns table (place_ref uuid, user_id uuid, circle_type text)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  -- Confirmed members the caller is allowed to be counted alongside. The block filter
  -- is applied to the COUNT, not just to the display: a place kept alive only by
  -- someone the caller blocked must not surface at all.
  select a.place_ref, a.user_id, a.circle_type
  from public.circle_affiliations a
  where a.status = 'confirmed'
    and a.dismissed_at is null
    and a.place_ref is not null
    and (a.user_id = p_user_id or not public.lana_is_blocked(p_user_id, a.user_id));
$$;

comment on function public.visible_place_members(uuid) is
  'Members of every place, as one caller is allowed to see them (confirmed, not '
  'dismissed, blocked users excluded). The single definition behind '
  'discover_communities_near and local_signal_for_places.';

revoke all on function public.visible_place_members(uuid) from public, anon;
grant execute on function public.visible_place_members(uuid) to authenticated, service_role;


create or replace function public.local_signal_for_places(
  p_user_id           uuid,
  p_google_place_ids  text[]
)
returns table (
  google_place_id  text,
  place_id         uuid,
  member_count     int,
  is_member        boolean,
  activity_labels  text[]
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with matched as (
    select p.id, p.google_place_id
    from public.places p
    where p.google_place_id = any (coalesce(p_google_place_ids, '{}'::text[]))
  ),
  visible as (
    select vm.place_ref, vm.user_id
    from public.visible_place_members(p_user_id) vm
    join matched m on m.id = vm.place_ref
  ),
  counted as (
    select
      v.place_ref                        as pid,
      count(distinct v.user_id)::int     as members,
      bool_or(v.user_id = p_user_id)     as mine
    from visible v
    group by v.place_ref
  ),
  acts as (
    select
      pa.place_id                                          as pid,
      array_agg(distinct pa.label order by pa.label)       as labels
    from public.place_activities pa
    join visible v
      on v.place_ref = pa.place_id and v.user_id = pa.user_id
    group by pa.place_id
  )
  select
    m.google_place_id,
    m.id,
    c.members,
    coalesce(c.mine, false),
    coalesce(a.labels, '{}'::text[])
  from matched m
  join counted c on c.pid = m.id
  left join acts a on a.pid = m.id
  where c.members > 0;
$$;

comment on function public.local_signal_for_places(uuid, text[]) is
  'Google place ids -> our community for that spot (member count, whether the caller is '
  'one, what members do there). Same visibility predicate as discover_communities_near. '
  'Places with no visible member are omitted rather than returned as zero.';

revoke all on function public.local_signal_for_places(uuid, text[]) from public, anon;
grant execute on function public.local_signal_for_places(uuid, text[])
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 2. Re-point discover_communities_near at the shared definition. Behaviour is
--    identical -- this only removes the duplicated member predicate. Body below is
--    20261004120000's, with the inline CTE swapped for the function call.
-- ----------------------------------------------------------------------------
create or replace function public.discover_communities_near(
  p_user_id        uuid,
  p_radius_meters  double precision default 8000,
  p_limit          int default 20,
  p_locale         text default 'en',
  p_query          text default null
) returns table (
  place_id         uuid,
  name             text,
  address          text,
  place_type       text,
  zip              text,
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

  select o.origin, o.zip5 into v_origin, v_zip5
  from public.user_origin_point(p_user_id) o;

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
    l.members,
    l.types,
    l.meters,
    public.humanize_distance_text(l.meters, p_locale),
    l.mine
  from located l
  where
    case
      -- With a point: everything inside the radius (a place with no coordinates
      -- can still qualify on ZIP equality, which is how imported rows behave).
      when v_origin is not null then
        l.meters is not null and l.meters <= p_radius_meters
        or (l.meters is null and v_zip5 is not null and l.zip = v_zip5)
      -- No point at all: ZIP equality is the only honest scope left.
      else v_zip5 is not null and l.zip = v_zip5
    end
  order by l.members desc, coalesce(l.meters, 1e9) asc, l.name asc
  limit greatest(coalesce(p_limit, 20), 1);
end;
$function$;
