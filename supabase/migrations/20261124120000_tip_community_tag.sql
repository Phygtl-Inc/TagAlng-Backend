-- ---------------------------------------------------------------------------
-- C-RECO-COMMUNITY — a recommendation dropped INTO a community lives only there.
--
-- Until now "a tip from CF Fitness" was inferred at read time: keep the rows whose
-- AUTHOR is a member. That is a different question from the one the share button asks
-- ("who did you mean this for?"), and it fails both ways — a tip you dropped for your
-- neighbourhood shows under CF Fitness because you happen to belong, and a member
-- outside the read radius never sees a tip meant for them, because membership can only
-- narrow what distance already returned.
--
-- So the destination is recorded at WRITE time (local_signals.circle_place_ref) and both
-- reads take it as scope, not as a filter:
--
--   community selected  -> only that community's tips, NO distance bound (product
--                          decision 2026-09-07: a member is a member, however far)
--   nothing selected    -> only untagged tips, distance as before
--
-- Untagged is the default, so every pre-existing row keeps behaving exactly as it does
-- today and there is nothing to backfill.
-- ---------------------------------------------------------------------------

alter table public.local_signals
  add column if not exists circle_place_ref uuid references public.places (id);

comment on column public.local_signals.circle_place_ref is
  'The community this signal was shared INTO (places.id). NULL = the area feed. Set only '
  'via tag_local_signal, which verifies the author belongs to the place.';

-- Partial: tagged rows are the minority and the untagged read wants `is null` fast.
create index if not exists local_signals_circle_place_idx
  on public.local_signals (circle_place_ref, created_at desc)
  where circle_place_ref is not null;


-- ---------------------------------------------------------------------------
-- 1. Stamping it. A separate one-job RPC rather than an eighth argument on
--    save_local_signal: that function is 150 lines of dedupe/matching/notify and
--    re-declaring it whole to thread one column through is how a behaviour goes
--    missing in a copy-paste.
-- ---------------------------------------------------------------------------
create or replace function public.tag_local_signal(
  p_signal_id uuid,
  p_place_id  uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_owner uuid;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select user_id into v_owner from public.local_signals where id = p_signal_id;
  if v_owner is null or v_owner <> v_me then
    raise exception 'not_your_signal' using errcode = 'P0001';
  end if;

  -- You can only drop a tip into a place you actually belong to. Checked here and not
  -- at the caller because this is security definer: without it the client picks any
  -- place id and posts into a community it has never been near.
  if p_place_id is not null and not exists (
    select 1 from public.circle_affiliations ca
     where ca.user_id = v_me
       and ca.place_ref = p_place_id
       and ca.status in ('confirmed', 'curious')
  ) then
    raise exception 'not_a_member' using errcode = 'P0001';
  end if;

  update public.local_signals
     set circle_place_ref = p_place_id,
         updated_at = now()
   where id = p_signal_id;

  return jsonb_build_object('ok', true, 'circle_place_ref', p_place_id);
end;
$$;

comment on function public.tag_local_signal(uuid, uuid) is
  'Record which community a signal was shared into. Owner-only, membership-verified.';

revoke all on function public.tag_local_signal(uuid, uuid) from public, anon;
grant execute on function public.tag_local_signal(uuid, uuid) to authenticated, service_role;


-- ---------------------------------------------------------------------------
-- 2. find_neighbor_tips v5 — v4 plus p_circle_place_id. Rebuilt (new argument list),
--    OUT columns unchanged.
-- ---------------------------------------------------------------------------
drop function if exists public.find_neighbor_tips(text, text, text, int, text, double precision);

create function public.find_neighbor_tips(
  p_block_id        text default null,
  p_category        text default null,
  p_query           text default null,
  p_limit           int default 5,
  p_locale          text default 'en',
  p_radius_meters   double precision default null,
  p_circle_place_id uuid default null
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
    and public._signal_match_strength(p_category, p_query, s.category, s.detail_text) > 0
  order by strength desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

comment on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid) is
  'Neighbor tips matching an ask. v5 = v4 plus p_circle_place_id: with it, the community '
  'roster is the audience and distance does not apply; without it, community-scoped tips '
  'are excluded from the area read.';

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid)
  from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision, uuid)
  to authenticated, service_role;


-- ---------------------------------------------------------------------------
-- 3. recent_neighbor_tips v2 — the browse feed, same scope rule. With a community
--    selected the client shows ONE list (no Recent / My community tabs), so p_filter
--    is ignored except for its sort.
-- ---------------------------------------------------------------------------
drop function if exists public.recent_neighbor_tips(text, double precision, int, text);

create function public.recent_neighbor_tips(
  p_filter          text default 'recent',
  p_radius_meters   double precision default 25000,
  p_limit           int default 20,
  p_locale          text default 'en',
  p_circle_place_id uuid default null
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

comment on function public.recent_neighbor_tips(text, double precision, int, text, uuid) is
  'Browse feed of recent recommendations. v2 = v1 plus p_circle_place_id: one community, '
  'no distance bound, no tabs. Same visibility rules as find_neighbor_tips.';

revoke all on function public.recent_neighbor_tips(text, double precision, int, text, uuid)
  from public, anon;
grant execute on function public.recent_neighbor_tips(text, double precision, int, text, uuid)
  to authenticated, service_role;
