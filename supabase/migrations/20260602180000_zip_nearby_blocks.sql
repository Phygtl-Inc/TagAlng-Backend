-- TagAlng: ZIP → nearby blocks list + optional home_zip on assign

create table if not exists public.zip_centroids (
  zip5 text primary key check (zip5 ~ '^\d{5}$'),
  lat double precision not null,
  lng double precision not null,
  city text,
  cluster_id text not null default 'lake-nona',
  created_at timestamptz not null default now()
);

comment on table public.zip_centroids is
  'ZIP5 → approximate centroid for block picker. Not user PII; seed/expand per market.';

alter table public.users
  add column if not exists home_zip text check (home_zip is null or home_zip ~ '^\d{5}$');

comment on column public.users.home_zip is
  'US ZIP5 chosen by user (vicinity). Never store street address.';

insert into public.zip_centroids (zip5, lat, lng, city, cluster_id) values
  ('32827', 28.3647, -81.2568, 'Lake Nona', 'lake-nona'),
  ('32832', 28.4510, -81.2490, 'Orlando SE', 'lake-nona'),
  ('32828', 28.5340, -81.1450, 'Avalon Park', 'lake-nona'),
  ('32814', 28.5530, -81.3220, 'Baldwin Park', 'lake-nona'),
  ('34786', 28.4969, -81.5388, 'Windermere', 'lake-nona'),
  ('34787', 28.5650, -81.5860, 'Winter Garden', 'lake-nona'),
  ('34747', 28.3180, -81.5400, 'Celebration', 'lake-nona'),
  ('34711', 28.5580, -81.7680, 'Clermont', 'lake-nona')
on conflict (zip5) do update
  set lat = excluded.lat,
      lng = excluded.lng,
      city = excluded.city,
      cluster_id = excluded.cluster_id;

create or replace function public.normalize_zip5(p_zip text)
returns text
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
  v_digits text;
begin
  v_digits := regexp_replace(coalesce(p_zip, ''), '[^0-9]', '', 'g');
  if length(v_digits) < 5 then
    return null;
  end if;
  return substr(v_digits, 1, 5);
end;
$$;

revoke all on function public.normalize_zip5(text) from public, anon, authenticated;

create or replace function public.get_blocks_near_zip(
  p_zip text,
  p_cluster_id text default 'lake-nona',
  p_limit int default 10
)
returns table (
  block_id text,
  display_name text,
  block_state public.block_state,
  cluster_id text,
  distance_meters double precision,
  signup_count int
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_zip5 text;
  v_lat double precision;
  v_lng double precision;
  v_point extensions.geography;
begin
  v_zip5 := public.normalize_zip5(p_zip);
  if v_zip5 is null then
    raise exception 'invalid_zip' using errcode = 'P0001';
  end if;

  select z.lat, z.lng
  into v_lat, v_lng
  from public.zip_centroids z
  where z.zip5 = v_zip5;

  if not found then
    raise exception 'zip_not_found' using errcode = 'P0001';
  end if;

  v_point := extensions.st_setsrid(
    extensions.st_makepoint(v_lng, v_lat),
    4326
  )::extensions.geography;

  return query
  select
    b.id,
    b.display_name,
    b.state,
    b.cluster_id,
    extensions.st_distance(b.centroid, v_point)::double precision,
    coalesce(c.signup_count, 0)::int
  from public.blocks b
  left join public.block_waitlist_counts c on c.block_id = b.id
  where b.cluster_id = p_cluster_id
    and b.centroid is not null
  order by extensions.st_distance(b.centroid, v_point)
  limit greatest(1, least(coalesce(p_limit, 10), 25));
end;
$$;

drop function if exists public.assign_home_block(text, double precision, double precision);

create or replace function public.assign_home_block(
  p_block_id text default null,
  p_lat double precision default null,
  p_lng double precision default null,
  p_home_zip text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_uid uuid := auth.uid();
  v_block_id text;
  v_block record;
  v_zip5 text;
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_zip5 := public.normalize_zip5(p_home_zip);

  if p_block_id is not null then
    select id, display_name, state, cluster_id
    into v_block
    from public.blocks
    where id = p_block_id;

    if not found then
      raise exception 'invalid_block' using errcode = 'P0001';
    end if;

    v_block_id := p_block_id;
  elsif p_lat is not null and p_lng is not null then
    v_block_id := public.resolve_nearest_block_id(p_lat, p_lng);

    if v_block_id is null then
      raise exception 'no_block_nearby' using errcode = 'P0001';
    end if;

    select id, display_name, state, cluster_id
    into v_block
    from public.blocks
    where id = v_block_id;
  else
    raise exception 'block_or_coords_required' using errcode = 'P0001';
  end if;

  insert into public.users (id, home_block_id, home_zip)
  values (v_uid, v_block_id, v_zip5)
  on conflict (id) do update
    set home_block_id = excluded.home_block_id,
        home_zip = coalesce(excluded.home_zip, public.users.home_zip),
        updated_at = now();

  return jsonb_build_object(
    'user_id', v_uid,
    'home_block_id', v_block_id,
    'home_zip', v_zip5,
    'block_display_name', v_block.display_name,
    'block_state', v_block.state,
    'cluster_id', v_block.cluster_id
  );
end;
$$;

create or replace function public.get_my_profile()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'id', u.id,
    'phone', u.phone,
    'nickname', u.nickname,
    'home_block_id', u.home_block_id,
    'home_zip', u.home_zip,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'home_location_visibility', u.home_location_visibility::text,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$$;

revoke execute on function public.get_blocks_near_zip(text, text, int) from public, anon;
grant execute on function public.get_blocks_near_zip(text, text, int) to authenticated;

revoke execute on function public.assign_home_block(text, double precision, double precision, text) from public, anon;
grant execute on function public.assign_home_block(text, double precision, double precision, text) to authenticated;
