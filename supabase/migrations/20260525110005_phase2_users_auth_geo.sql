-- TagAlng Phase 2 (dev): Supabase Auth profile + block assignment from GPS or pick

-- Block centroids for nearest-block resolution (never store user street address)
alter table public.blocks
  add column if not exists centroid extensions.geography(point, 4326);

comment on column public.blocks.centroid is
  'Approx block center for dev nearest-block lookup. Production: H3 from geocode worker.';

-- Lake Nona placeholder centers (replace with dossier H3 centroids)
update public.blocks
set centroid = extensions.st_setsrid(extensions.st_makepoint(-81.2568, 28.3647), 4326)::extensions.geography
where id = '8a2a1072b59ffff';

update public.blocks
set centroid = extensions.st_setsrid(extensions.st_makepoint(-81.2621, 28.3689), 4326)::extensions.geography
where id = '8a2a1072b5affff';

create table public.users (
  id uuid primary key references auth.users (id) on delete cascade,
  phone text,
  nickname text,
  home_block_id text references public.blocks (id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.users is
  'App profile keyed to auth.users. No street address; home_block_id is H3 block.';

create index users_home_block_id_idx on public.users (home_block_id);

create trigger users_updated_at
before update on public.users
for each row execute function public.set_updated_at();

-- Create profile row when phone auth completes
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.users (id, phone)
  values (new.id, new.phone)
  on conflict (id) do update
    set phone = excluded.phone,
        updated_at = now();
  return new;
end;
$$;

create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();

-- Nearest block by GPS (lng, lat). Does not persist raw coordinates.
create or replace function public.resolve_nearest_block_id(
  p_lat double precision,
  p_lng double precision,
  p_cluster_id text default 'lake-nona'
)
returns text
language sql
stable
security definer
set search_path = public, extensions
as $$
  select b.id
  from public.blocks b
  where b.centroid is not null
    and b.cluster_id = p_cluster_id
  order by extensions.st_distance(
    b.centroid,
    extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography
  )
  limit 1;
$$;

-- Authenticated user sets home block (manual pick or GPS)
create or replace function public.assign_home_block(
  p_block_id text default null,
  p_lat double precision default null,
  p_lng double precision default null
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  v_uid uuid := auth.uid();
  v_block_id text;
  v_block record;
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

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

  insert into public.users (id, home_block_id)
  values (v_uid, v_block_id)
  on conflict (id) do update
    set home_block_id = excluded.home_block_id,
        updated_at = now();

  return jsonb_build_object(
    'user_id', v_uid,
    'home_block_id', v_block_id,
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
set search_path = public
as $$
  select jsonb_build_object(
    'id', u.id,
    'phone', u.phone,
    'nickname', u.nickname,
    'home_block_id', u.home_block_id,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$$;

grant execute on function public.resolve_nearest_block_id to authenticated;
grant execute on function public.assign_home_block to authenticated;
grant execute on function public.get_my_profile to authenticated;
