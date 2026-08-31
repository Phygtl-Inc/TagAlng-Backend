-- Recent recommendations: the browse companion to asking (C-FIND-RECENT).
--
-- Until now the ONLY way to read a neighbour's tip was to ask for one: find_neighbor_tips
-- scores every tip against a specific request and returns the matches, inside a chat turn.
-- There was no "show me what people have shared lately" at all, so the Recent-tips pill on
-- tip-P1 had nothing to open.
--
-- Two feedback verbs, two tables, on purpose. They are not the same claim:
--   ✓ I vouch    — "I know this place too" (adds YOUR voice to the recommendation)
--   👍 Helpful   — "this answer helped ME" (says nothing about the place)
-- Collapsing them into one counter would let a reader who has never been to the place
-- inflate its vouch count, which is the one number a stranger is meant to trust.
--
-- Both are idempotent per (signal, user): unique constraints, and the RPCs take the
-- desired state rather than toggling, so a double tap on a flaky connection cannot end up
-- inverting what the user chose.

create table if not exists public.tip_vouches (
  signal_id  uuid not null references public.local_signals (id) on delete cascade,
  user_id    uuid not null references public.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (signal_id, user_id)
);

create table if not exists public.tip_helpful (
  signal_id  uuid not null references public.local_signals (id) on delete cascade,
  user_id    uuid not null references public.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (signal_id, user_id)
);

comment on table public.tip_vouches is
  'One neighbour adding their own voice to someone else''s recommendation ("I know this '
  'place too"). Distinct from tip_helpful, which rates the ANSWER, not the place.';
comment on table public.tip_helpful is
  'One reader marking a recommendation useful to them. Says nothing about the place — '
  'never fold into the vouch count a stranger reads as social proof.';

create index if not exists tip_vouches_signal_idx on public.tip_vouches (signal_id);
create index if not exists tip_helpful_signal_idx on public.tip_helpful (signal_id);

alter table public.tip_vouches enable row level security;
alter table public.tip_helpful enable row level security;

-- Own rows only. Every count a reader sees is aggregated by the security-definer
-- functions below, so nobody needs to read anyone else's row to render a card.
drop policy if exists tip_vouches_own on public.tip_vouches;
create policy tip_vouches_own on public.tip_vouches
  for select using (user_id = auth.uid());
drop policy if exists tip_helpful_own on public.tip_helpful;
create policy tip_helpful_own on public.tip_helpful
  for select using (user_id = auth.uid());

revoke all on public.tip_vouches from anon;
revoke all on public.tip_helpful from anon;


-- ----------------------------------------------------------------------------
-- 1. The two writes. `p_on` is the desired state, not a toggle (see header).
-- ----------------------------------------------------------------------------
create or replace function public.set_tip_vouch(p_signal_id uuid, p_on boolean default true)
returns int
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_me uuid := auth.uid();
  v_author uuid;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  select s.user_id into v_author
  from public.local_signals s
  where s.id = p_signal_id and s.intent = 'tip_share';
  if v_author is null then
    raise exception 'tip_not_found' using errcode = 'P0001';
  end if;
  -- Vouching for your own tip would be counting yourself twice: the tip IS your voice.
  if v_author = v_me then
    raise exception 'cannot_vouch_own_tip' using errcode = 'P0001';
  end if;
  if coalesce(p_on, true) then
    insert into public.tip_vouches (signal_id, user_id)
    values (p_signal_id, v_me)
    on conflict (signal_id, user_id) do nothing;
  else
    delete from public.tip_vouches where signal_id = p_signal_id and user_id = v_me;
  end if;
  return (select count(*)::int from public.tip_vouches where signal_id = p_signal_id);
end;
$$;

create or replace function public.set_tip_helpful(p_signal_id uuid, p_on boolean default true)
returns int
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if not exists (
    select 1 from public.local_signals s where s.id = p_signal_id and s.intent = 'tip_share'
  ) then
    raise exception 'tip_not_found' using errcode = 'P0001';
  end if;
  -- Marking your own tip helpful is allowed to be a no-op rather than an error: it is a
  -- rating of the ANSWER, and the author simply has no vote to cast on their own.
  if coalesce(p_on, true) then
    insert into public.tip_helpful (signal_id, user_id)
    values (p_signal_id, v_me)
    on conflict (signal_id, user_id) do nothing;
  else
    delete from public.tip_helpful where signal_id = p_signal_id and user_id = v_me;
  end if;
  return (select count(*)::int from public.tip_helpful where signal_id = p_signal_id);
end;
$$;

revoke all on function public.set_tip_vouch(uuid, boolean) from public, anon;
revoke all on function public.set_tip_helpful(uuid, boolean) from public, anon;
grant execute on function public.set_tip_vouch(uuid, boolean) to authenticated, service_role;
grant execute on function public.set_tip_helpful(uuid, boolean) to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 2. The feed. Same visibility rules as find_neighbor_tips — this is a second VIEW of
--    the same rows, not a second source of truth, so a tip hidden from an ask must be
--    hidden from the browse.
--
--    p_filter: 'recent'  newest first, everything visible
--              'circles' only authors who share a circle with the caller
--              'nearest' same set as recent, ordered by distance
-- ----------------------------------------------------------------------------
create or replace function public.recent_neighbor_tips(
  p_filter         text default 'recent',
  p_radius_meters  double precision default 25000,
  p_limit          int default 20,
  p_locale         text default 'en'
)
returns table (
  signal_id        uuid,
  detail_text      text,
  category         text,
  affinity_tags    text[],
  created_at       timestamptz,
  peer_user_id     uuid,
  neighbor_label   text,
  avatar_url       text,
  distance_meters  double precision,
  distance_text    text,
  shared_circles   jsonb,
  same_block       boolean,
  vouch_count      int,
  helpful_count    int,
  i_vouched        boolean,
  i_marked_helpful boolean
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
      s.detail_text,
      s.category,
      coalesce(s.affinity_tags, '{}')::text[] as tags,
      s.created_at,
      s.user_id,
      coalesce(u.nickname, 'A neighbor') as label,
      u.profile_photo_url,
      case
        when v_origin is null or pp.pt is null then null
        else extensions.st_distance(pp.pt, v_origin)::double precision
      end as dist_m,
      coalesce(o.circles, '[]'::jsonb) as circles,
      (v_my_block is not null and u.home_block_id = v_my_block) as same_blk,
      (select count(*)::int from public.tip_vouches tv where tv.signal_id = s.id) as vouches,
      (select count(*)::int from public.tip_helpful th where th.signal_id = s.id) as helpfuls,
      exists (
        select 1 from public.tip_vouches tv where tv.signal_id = s.id and tv.user_id = v_me
      ) as did_vouch,
      exists (
        select 1 from public.tip_helpful th where th.signal_id = s.id and th.user_id = v_me
      ) as did_helpful
    from public.local_signals s
    join public.users u on u.id = s.user_id
    left join peer_points pp on pp.peer_id = s.user_id
    left join overlap o on o.peer_id = s.user_id
    where s.intent = 'tip_share'
      and s.status = 'listening'
      and s.expires_at > now()
      and s.user_id <> v_me
      and not public.lana_is_blocked(v_me, s.user_id)
      -- Unlocatable caller: fall back to the block rather than showing the whole table.
      and (
        case
          when v_origin is null then
            v_my_block is not null and u.home_block_id = v_my_block
          else pp.pt is not null and extensions.st_dwithin(pp.pt, v_origin, v_radius)
        end
      )
      -- "My circles" is a real filter, not a re-sort: a tip from someone she shares no
      -- place with does not belong in that tab at all.
      and (v_filter <> 'circles' or o.circles is not null)
  )
  select
    r.id, r.detail_text, r.category, r.tags, r.created_at,
    r.user_id, r.label, r.profile_photo_url,
    r.dist_m,
    case
      when r.dist_m is null then null
      else public.humanize_distance_text(r.dist_m, coalesce(p_locale, 'en'))
    end,
    r.circles, r.same_blk, r.vouches, r.helpfuls, r.did_vouch, r.did_helpful
  from rows_out r
  order by
    case when v_filter = 'nearest' then coalesce(r.dist_m, 1e9) end asc nulls last,
    r.created_at desc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

comment on function public.recent_neighbor_tips(text, double precision, int, text) is
  'C-FIND-RECENT: the browse feed behind the Recent-tips pill. Same visibility rules as '
  'find_neighbor_tips (a tip hidden from an ask stays hidden from the browse). p_filter '
  'recent | circles | nearest; "circles" FILTERS to authors sharing a place with the '
  'caller, each row carrying that shared circle for the card''s label.';

revoke all on function public.recent_neighbor_tips(text, double precision, int, text) from public, anon;
grant execute on function public.recent_neighbor_tips(text, double precision, int, text)
  to authenticated, service_role;


-- ----------------------------------------------------------------------------
-- 3. find_neighbor_tips v4 — the same two counts on the ASK results, so "2 vouched"
--    on C-FIND-V2 and the detail screen's vouch panel read one number, not two.
--    Rebuilt rather than replaced: OUT columns change (42P13).
-- ----------------------------------------------------------------------------
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
  'Neighbor tips matching an ask. v4 = v3 (shared_circles, same_block) plus vouch_count, '
  'helpful_count and i_vouched, so the results list and the detail panel read the same '
  'numbers as the browse feed.';

revoke all on function public.find_neighbor_tips(text, text, text, int, text, double precision) from public, anon;
grant execute on function public.find_neighbor_tips(text, text, text, int, text, double precision)
  to authenticated, service_role;
