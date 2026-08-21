-- Tips grouped by the circle you share with whoever recommended it (C-FIND-V2).
--
-- The design groups results under the circle, not under the place:
--
--     YOUR BLOCK
--       Narcoossee Kids' Clinic   "Rosa, two doors down — walk-in friendly"
--     ST MARY'S CHURCH
--       Dr. Sarah                 "coral88 + sunny from St. Mary's — gentle"
--     TUESDAY TODDLERS
--       ...
--
-- and the spec is explicit about why: "the grouping IS the explanation". A stranger's
-- recommendation and a recommendation from someone you sit next to at St Mary's are not
-- the same claim, and until now the wire could not tell them apart -- find_neighbor_tips
-- returned the rec, the author and a distance, with nothing about how the two are
-- connected. That one missing field is what the grouping headers, the "My circles" sort
-- tab and the voucher card's community list all read.
--
-- SHARED, not "theirs": a circle appears only when BOTH people are visible members of it.
-- Listing the recommender's other communities would disclose their memberships to someone
-- with no connection to them; an overlap is a fact about the pair, which is why it is safe
-- to show and is also the only version that explains anything.
--
-- same_block is separate from the circle list on purpose. "Your block" is the group header
-- for a neighbour you share no named place with, and a block is not a circle -- it has no
-- place_ref, no members table and nobody joined it.

-- `create or replace` cannot change a function's OUT columns (42P13: "Row type defined by
-- OUT parameters is different"), and this adds two. Dropped by its exact 6-arg signature,
-- so the 4-arg v1 overload from 20261001120000 -- which local_signals.py still falls back
-- to against an un-migrated database -- is left alone. One migration, one transaction:
-- there is no window where the function is missing.
drop function if exists public.find_neighbor_tips(text, text, text, int, text, double precision);

create function public.find_neighbor_tips(
  p_block_id       text default null,
  p_category       text default null,
  p_query          text default null,
  p_limit          int default 5,
  p_locale         text default 'en',
  p_radius_meters  double precision default null
)
returns table (
  signal_id        uuid,
  detail_text      text,
  category         text,
  match_strength   real,
  neighbor_label   text,
  peer_user_id     uuid,
  avatar_url       text,
  affinity_tags    text[],
  distance_meters  double precision,
  distance_text    text,
  created_at       timestamptz,
  -- NEW: how the caller and this recommender are connected.
  shared_circles   jsonb,
  same_block       boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $function$
declare
  v_me uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision;
  v_my_block text;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;
  select u.home_block_id into v_my_block from public.users u where u.id = v_me;

  if p_radius_meters is not null then
    v_radius := greatest(100, least(p_radius_meters, 200000));
    if v_origin is null then
      v_radius := null;
      if p_block_id is null or length(trim(p_block_id)) = 0 then
        return;
      end if;
    end if;
  end if;

  return query
  with peer_points as (
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
    where u.id <> v_me
  ),
  -- One read of the caller's own memberships, reused for every tip row. Through the
  -- shared definition (20261103120000), so "member" cannot mean two things.
  visible as (
    select vm.place_ref, vm.user_id
    from public.visible_place_members(v_me) vm
  ),
  mine as (
    select v.place_ref from visible v where v.user_id = v_me
  ),
  -- place_ref -> the circles that place is, as the caller may see them.
  overlap as (
    select
      v.user_id as peer_id,
      jsonb_agg(
        jsonb_build_object(
          'place_id',    p.id,
          'name',        p.name,
          'circle_type', p.place_type
        )
        order by p.name
      ) as circles
    from visible v
    join mine m on m.place_ref = v.place_ref
    join public.places p on p.id = v.place_ref
    where v.user_id <> v_me
    group by v.user_id
  )
  select
    s.id,
    s.detail_text,
    s.category,
    public._signal_match_strength(p_category, p_query, s.category, s.detail_text) as strength,
    coalesce(u.nickname, 'A neighbor on your block') as neighbor_label,
    s.user_id,
    u.profile_photo_url,
    coalesce(s.affinity_tags, '{}')::text[],
    case
      when v_origin is null or pp.pt is null then null
      else extensions.st_distance(pp.pt, v_origin)::double precision
    end as dist_m,
    case
      when v_origin is null or pp.pt is null then null
      else public.humanize_distance_text(
             extensions.st_distance(pp.pt, v_origin)::double precision,
             coalesce(p_locale, 'en')
           )
    end as dist_text,
    s.created_at,
    coalesce(o.circles, '[]'::jsonb),
    (v_my_block is not null and u.home_block_id = v_my_block)
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  left join overlap o on o.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and not public.lana_is_blocked(v_me, s.user_id)
    and (
      case
        when v_radius is not null
          then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
        else s.block_id = p_block_id
      end
    )
    and public._signal_match_strength(p_category, p_query, s.category, s.detail_text) > 0
  order by strength desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_neighbor_tips(text, text, text, int, text, double precision) is
  'Neighbor tips matching an ask. v3 adds shared_circles (places BOTH the caller and the '
  'recommender belong to — the C-FIND-V2 grouping header, and the reason the rec is '
  'trustworthy) and same_block. A circle is never listed unless it is shared: one '
  'person''s memberships are not disclosed to a stranger.';

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision) from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision)
  to authenticated, service_role;
