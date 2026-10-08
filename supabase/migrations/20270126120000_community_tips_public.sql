-- ============================================================================
-- A recommendation shared INTO a community is visible to everyone, not only its members.
--
-- Product decision 2026-10-08: a tip someone shares in their gym's community should reach
-- anyone looking for one — the person who names the gym ("any good trainers at CF
-- Fitness?") and the neighbour who just asks nearby ("good gym near me?"). Until now both
-- walls stood: a community read returned nothing unless the caller was a member
-- (20261124120000: "same bar as the roster"), and an area read excluded every tagged tip
-- ("s.circle_place_ref is null"). So a community tip reached nobody outside it, in any mode.
--
-- What changes, in every reader of tip visibility:
--   community scope -> that community's tips, for ANY signed-in caller (no membership check)
--   area scope      -> every tip in range, tagged or not (same distance / block rule as before)
-- What does not: the community TAG itself (circle_place_ref) is still recorded and still
-- scopes a community read; blocked users, expiry, status and the reco_type chips are as
-- before. WHO is in a community stays members-only — the roster is not touched here, and
-- shared_circles still only names communities the caller and the author both belong to.
--
-- Applies to existing tips too (decision: all tips, old and new). The share confirmation no
-- longer tells sharers a tip stays inside the community (app/tip_share.py).
--
-- Signatures, grants and comments are unchanged: these are create-or-replace of the
-- latest bodies (find_neighbor_tips 20261222120000, recent_neighbor_tips 20261128120000,
-- neighbor_tip_type_counts 20261130120000, _reco_aspect_visible 20261229120000) with only
-- the two predicates removed.
--
-- ROLLBACK: re-run those four function bodies from their migrations.
-- ============================================================================

create or replace function public.find_neighbor_tips(
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
  signal_id           uuid,
  detail_text         text,
  category            text,
  match_strength      real,
  neighbor_label      text,
  peer_user_id        uuid,
  avatar_url          text,
  affinity_tags       text[],
  distance_meters     double precision,
  distance_text       text,
  created_at          timestamptz,
  shared_circles      jsonb,
  same_block          boolean,
  vouch_count         int,
  helpful_count       int,
  i_vouched           boolean,
  -- The author's own card fields. Carried because the 80/20 clustering reads what each
  -- neighbour actually SAID, and detail_text is a joined recap, not their words.
  reco_name           text,
  reco_description    text,
  reco_fields         jsonb,
  reco_type           text,
  -- The subject, and how its contributions may be combined.
  subject_ref         uuid,
  subject_name        text,
  subject_category    text,
  subject_locality    text,
  subject_lat         double precision,
  subject_lng         double precision,
  -- How far the THING is, which is not how far its recommender lives. The card reads
  -- "Pediatric clinic · 0.2 mi" about the clinic; the recommender's own distance stays on
  -- their row, where it belongs. Null until a subject is grounded, so a card falls back to
  -- the recommender's distance rather than inventing one.
  subject_distance_meters double precision,
  subject_distance_text   text,
  subject_merge_mode  text,
  subject_vouch_count int,
  i_contributed       boolean
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

  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
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
    exists (select 1 from public.tip_vouches tv where tv.signal_id = s.id and tv.user_id = v_me),
    s.reco_name,
    s.reco_description,
    coalesce(s.reco_fields, '[]'::jsonb),
    s.reco_type,
    s.subject_ref,
    rs.display_name,
    rs.category,
    rs.locality,
    rs.lat,
    rs.lng,
    case
      when v_origin is null or rs.lat is null or rs.lng is null then null
      else extensions.st_distance(
             extensions.st_setsrid(extensions.st_makepoint(rs.lng, rs.lat), 4326)::extensions.geography,
             v_origin
           )::double precision
    end,
    case
      when v_origin is null or rs.lat is null or rs.lng is null then null
      else public.humanize_distance_text(
             extensions.st_distance(
               extensions.st_setsrid(extensions.st_makepoint(rs.lng, rs.lat), 4326)::extensions.geography,
               v_origin
             )::double precision,
             coalesce(p_locale, 'en')
           )
    end,
    rs.merge_mode,
    -- Across every visible live contribution, not just this page (see the header). Same
    -- block rule as the row itself: a contribution from someone the caller blocked must
    -- not be counted any more than it may be shown.
    case
      when s.subject_ref is null then 1
      else (
        select count(*)::int
        from public.local_signals ls
        where ls.subject_ref = s.subject_ref
          and ls.intent = 'tip_share'
          and ls.status = 'listening'
          and ls.expires_at > now()
          and not public.lana_is_blocked(v_me, ls.user_id)
      )
    end,
    -- Did the caller contribute to this subject? Their row is excluded from the list but
    -- counted above, so the card can say "you and 2 neighbours" rather than miscrediting.
    case
      when s.subject_ref is null then false
      else exists (
        select 1 from public.local_signals ls
        where ls.subject_ref = s.subject_ref
          and ls.user_id = v_me
          and ls.intent = 'tip_share'
          and ls.status = 'listening'
      )
    end
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join public.reco_subjects rs on rs.id = s.subject_ref
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
        else case
                   when v_radius is not null
                     then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
                   else s.block_id = p_block_id
                 end
      end
    )
    and (
      p_reco_types is null
      or cardinality(p_reco_types) = 0
      or s.reco_type = any (p_reco_types)
    )
    and (
      (p_reco_types is not null and cardinality(p_reco_types) > 0)
      or public._tip_match_strength(
           p_query, s.detail_text, s.affinity_tags, p_query_embedding, s.embedding,
           p_min_similarity
         ) > 0
    )
  order by strength desc, s.created_at desc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
end;
$function$;

create or replace function public.recent_neighbor_tips(
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
          else case
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

create or replace function public.neighbor_tip_type_counts(
  p_block_id        text default null,
  p_radius_meters   double precision default null,
  p_circle_place_id uuid default null
)
returns table (
  reco_type text,
  n         int
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
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  -- Same three-way scope rule as find_neighbor_tips: a community read needs no geography,
  -- an area read needs either a block or a radius.
  if p_circle_place_id is null
     and p_radius_meters is null
     and (p_block_id is null or length(trim(p_block_id)) = 0) then
    return;
  end if;

  -- Members-only, exactly as the roster and the tip search are.
  select o.origin into v_origin from public.user_origin_point(v_me) o;

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
  )
  select s.reco_type, count(*)::int
  from public.local_signals s
  join public.users u on u.id = s.user_id
  left join peer_points pp on pp.peer_id = s.user_id
  where s.intent = 'tip_share'
    and s.status = 'listening'
    and s.expires_at > now()
    and s.user_id <> v_me
    and s.reco_type is not null
    and not public.lana_is_blocked(v_me, s.user_id)
    and (
      case
        when p_circle_place_id is not null then s.circle_place_ref = p_circle_place_id
        else case
                   when v_radius is not null
                     then pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
                   else s.block_id = p_block_id
                 end
      end
    )
  group by s.reco_type
  order by 2 desc, 1;
end;
$function$;

create or replace function public._reco_aspect_visible(p_viewer uuid, p_signal_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.local_signals s
    where s.id = p_signal_id
      and (
        s.user_id = p_viewer
        or (
          s.intent = 'tip_share'
          and s.status = 'listening'
          and s.expires_at > now()
          and not public.lana_is_blocked(p_viewer, s.user_id)
        )
      )
  );
$$;
