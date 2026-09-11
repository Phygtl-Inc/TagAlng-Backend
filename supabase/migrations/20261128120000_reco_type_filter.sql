-- The category toggle on "Find a peer recommendation" is a FILTER, not a hint.
--
-- The screen opens on six chips — Services / Restaurants / DIY / Products / Recipes /
-- Others — and until now a tap only shaped the PROSE the ask was sent as ("I need a
-- recommendation for a recipe"). Both reads then scored that sentence against every tip
-- in range, so "Recipes" could answer with a plumber whose tip happened to mention
-- chicken: the taxonomy bucket every tip already carries (local_signals.reco_type, the
-- column the browse is indexed on) was never consulted.
--
-- Both readers take the same new argument: `p_reco_types text[]`, matched by equality.
-- An ARRAY and not a single type because one chip is not one bucket — "Services" means
-- professional OR service (a dentist and a plumber sit in different rows of the
-- taxonomy), and "Others" means location. The client sends the buckets its chip stands
-- for; NULL or {} keeps today's behaviour, so an untoggled ask still sees everything.
--
-- Deliberately equality and not "fuzzy type matching": reco_type is a closed seven-value
-- taxonomy written by the extractor at share time, so a filter over it either holds or
-- the chip is a lie. The MEANING match (20261126120000) still decides which rows inside
-- the bucket answer the ask.

-- ---------------------------------------------------------------------------
-- 1. find_neighbor_tips v7 = v6 + p_reco_types.
-- ---------------------------------------------------------------------------
drop function if exists public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real);

create function public.find_neighbor_tips(
  p_block_id        text default null,
  p_category        text default null,
  p_query           text default null,
  p_limit           int default 5,
  p_locale          text default 'en',
  p_radius_meters   double precision default null,
  p_circle_place_id uuid default null,
  p_query_embedding extensions.vector(768) default null,
  p_min_similarity  real default 0.55,
  p_reco_types      text[] default null
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
  shared_circles   jsonb,
  same_block       boolean,
  vouch_count      int,
  helpful_count    int,
  i_vouched        boolean
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

  -- Inside a community the roster IS the audience, so neither a block nor a radius is
  -- required to have a read at all — that early return is a geography rule.
  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  -- Same bar as the roster: a community read is members-only, so this can never be
  -- used to enumerate a place the caller has nothing to do with.
  if p_circle_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
     where ca.user_id = v_me
       and ca.place_ref = p_circle_place_id
       and ca.status in ('confirmed', 'curious')
  ) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;
  select u.home_block_id into v_my_block from public.users u where u.id = v_me;

  if p_radius_meters is not null then
    v_radius := greatest(100, least(p_radius_meters, 200000));
    if v_origin is null then
      v_radius := null;
      if p_circle_place_id is null
         and (p_block_id is null or length(trim(p_block_id)) = 0) then
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
    left join public.blocks b on b.id = u.home_block_id and b.centroid is not null
    left join public.zip_centroids z on z.zip5 = public.normalize_zip5(u.home_zip)
    where u.id <> v_me
  ),
  visible as (
    select vm.place_ref, vm.user_id from public.visible_place_members(v_me) vm
  ),
  mine as (
    select v.place_ref from visible v where v.user_id = v_me
  ),
  overlap as (
    select
      v.user_id as peer_id,
      jsonb_agg(
        jsonb_build_object('place_id', p.id, 'name', p.name, 'circle_type', p.place_type)
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
    public._tip_match_strength(
      p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding, p_min_similarity
    ) as strength,
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
    (v_my_block is not null and u.home_block_id = v_my_block),
    (select count(*)::int from public.tip_vouches tv where tv.signal_id = s.id),
    (select count(*)::int from public.tip_helpful th where th.signal_id = s.id),
    exists (select 1 from public.tip_vouches tv where tv.signal_id = s.id and tv.user_id = v_me)
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  left join overlap o on o.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and not public.lana_is_blocked(v_me, s.user_id)
    -- Scope, not filter: the community read ignores geography entirely, and the area
    -- read never shows a tip that was meant for a community.
    and (
      case
        when p_circle_place_id is not null then s.circle_place_ref = p_circle_place_id
        else s.circle_place_ref is null
             and case
                   when v_radius is not null
                     then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
                   else s.block_id = p_block_id
                 end
      end
    )
    and public._tip_match_strength(
      p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding, p_min_similarity
    ) > 0
    -- The category toggle on the Find-a-rec screen ("Recipes", "Services"). One coarse
    -- taxonomy bucket per tip (local_signals.reco_type), so this is equality over a list:
    -- the client's "Services" chip covers both professional and service, and "Others"
    -- covers location. NULL/empty = no toggle picked = every type, as before.
    and (
      p_reco_types is null
      or cardinality(p_reco_types) = 0
      or s.reco_type = any (p_reco_types)
    )
  order by strength desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[]) is
  'Neighbor tips matching an ask. v7 = v6 plus p_reco_types — the Find-a-rec category '
  'toggle, matched by equality against local_signals.reco_type. NULL/{} = every type.';

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[])
  from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid, extensions.vector, real, text[])
  to authenticated;

-- ---------------------------------------------------------------------------
-- 2. recent_neighbor_tips + p_reco_types. Same toggle, browsed instead of asked: the
--    "Recent recommendations" pill sits in the same chip row and must honour the pick.
-- ---------------------------------------------------------------------------
drop function if exists public.recent_neighbor_tips(text, double precision, int, text, uuid);

create function public.recent_neighbor_tips(
  p_filter          text default 'recent',
  p_radius_meters   double precision default 25000,
  p_limit           int default 20,
  p_locale          text default 'en',
  p_circle_place_id uuid default null,
  p_reco_types      text[] default null
)
returns table (
  signal_id          uuid,
  reco_name          text,
  category           text,
  reco_type          text,
  reco_place         text,
  reco_description   text,
  reco_fields        jsonb,
  detail_text        text,
  created_at         timestamptz,
  peer_user_id       uuid,
  neighbor_label     text,
  avatar_url         text,
  distance_meters    double precision,
  distance_text      text,
  shared_circles     jsonb,
  same_block         boolean,
  helpful_count      int,
  unhelpful_count    int,
  i_marked_helpful   boolean,
  i_marked_unhelpful boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
declare
  v_me uuid := auth.uid();
  v_origin extensions.geography;
  v_radius double precision := greatest(1000, least(coalesce(p_radius_meters, 25000), 200000));
  v_my_block text;
  v_filter text := lower(coalesce(p_filter, 'recent'));
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_circle_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
     where ca.user_id = v_me
       and ca.place_ref = p_circle_place_id
       and ca.status in ('confirmed', 'curious')
  ) then
    return;
  end if;

  select o.origin into v_origin from public.user_origin_point(v_me) o;
  select u.home_block_id into v_my_block from public.users u where u.id = v_me;

  return query
  with peer_points as (
    select
      u.id as peer_id,
      coalesce(
        b.centroid,
        extensions.st_setsrid(extensions.st_makepoint(z.lng, z.lat), 4326)::extensions.geography
      ) as pt
    from public.users u
    left join public.blocks b on b.id = u.home_block_id and b.centroid is not null
    left join public.zip_centroids z on z.zip5 = public.normalize_zip5(u.home_zip)
    where u.id <> v_me
  ),
  visible as (
    select vm.place_ref, vm.user_id from public.visible_place_members(v_me) vm
  ),
  mine as (
    select v.place_ref from visible v where v.user_id = v_me
  ),
  overlap as (
    select
      v.user_id as peer_id,
      jsonb_agg(
        jsonb_build_object('place_id', p.id, 'name', p.name, 'circle_type', p.place_type)
        order by p.name
      ) as circles
    from visible v
    join mine m on m.place_ref = v.place_ref
    join public.places p on p.id = v.place_ref
    where v.user_id <> v_me
    group by v.user_id
  ),
  rows_out as (
    select
      s.id,
      s.reco_name,
      s.category,
      s.reco_type,
      s.reco_place,
      s.reco_description,
      coalesce(s.reco_fields, '[]'::jsonb) as fields,
      s.detail_text,
      s.created_at,
      s.user_id,
      coalesce(nullif(btrim(u.nickname), ''), 'A neighbor') as label,
      u.profile_photo_url,
      case
        when v_origin is null or pp.pt is null then null
        else extensions.st_distance(pp.pt, v_origin)::double precision
      end as dist_m,
      coalesce(o.circles, '[]'::jsonb) as circles,
      (v_my_block is not null and u.home_block_id = v_my_block) as same_blk,
      (select count(*)::int from public.tip_helpful th
        where th.signal_id = s.id and th.is_helpful) as helpfuls,
      (select count(*)::int from public.tip_helpful th
        where th.signal_id = s.id and not th.is_helpful) as unhelpfuls,
      exists (
        select 1 from public.tip_helpful th
         where th.signal_id = s.id and th.user_id = v_me and th.is_helpful
      ) as did_helpful,
      exists (
        select 1 from public.tip_helpful th
         where th.signal_id = s.id and th.user_id = v_me and not th.is_helpful
      ) as did_unhelpful
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
          when p_circle_place_id is not null then s.circle_place_ref = p_circle_place_id
          else s.circle_place_ref is null
               and case
                     when v_origin is null then
                       v_my_block is not null and u.home_block_id = v_my_block
                     else pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
                   end
        end
      )
      -- "My circles" is a real filter, not a re-sort: a tip from someone she shares no
      -- place with does not belong in that tab at all. Meaningless once ONE community is
      -- selected — that view has no tabs.
      and (p_circle_place_id is not null or v_filter <> 'circles' or o.circles is not null)
      -- Same category toggle as the ask path, browsed instead of asked.
      and (
        p_reco_types is null
        or cardinality(p_reco_types) = 0
        or s.reco_type = any (p_reco_types)
      )
  )
  select
    r.id, r.reco_name, r.category, r.reco_type, r.reco_place, r.reco_description,
    r.fields, r.detail_text, r.created_at,
    r.user_id, r.label, r.profile_photo_url,
    r.dist_m,
    case
      when r.dist_m is null then null
      else public.humanize_distance_text(r.dist_m, coalesce(p_locale, 'en'))
    end,
    r.circles, r.same_blk, r.helpfuls, r.unhelpfuls, r.did_helpful, r.did_unhelpful
  from rows_out r
  order by
    case when v_filter = 'nearest' then coalesce(r.dist_m, 1e9) end asc nulls last,
    r.created_at desc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

comment on function public.recent_neighbor_tips(text, double precision, int, text, uuid, text[]) is
  'Recent neighbour recommendations, newest / circles / nearest. p_reco_types filters on '
  'the Find-a-rec category toggle (local_signals.reco_type); NULL/{} = every type.';

revoke all on function public.recent_neighbor_tips(text, double precision, int, text, uuid, text[])
  from public, anon;
grant execute on function public.recent_neighbor_tips(text, double precision, int, text, uuid, text[])
  to authenticated;
