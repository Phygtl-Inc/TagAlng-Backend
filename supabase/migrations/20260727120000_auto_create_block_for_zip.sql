-- Auto-create a waitlist block when a user arrives from a ZIP we don't cover yet, so
-- signup is NEVER blocked on "I couldn't find blocks for that ZIP". The app geocodes the
-- ZIP via Google (lat/lng + city) and passes them in; we record the ZIP centroid and
-- create ONE waitlist block per ZIP (centroid = the geocoded point). Distance-based
-- get_blocks_near_zip then returns it for that ZIP on subsequent lookups. The block stays
-- state='waitlist' until neighbors join; it is fully usable (signals can be posted) — there
-- are simply no matches until others arrive.

create or replace function public.create_block_for_zip(
  p_zip text,
  p_lat double precision,
  p_lng double precision,
  p_city text default null,
  p_display_name text default null,
  p_cluster_id text default 'lake-nona'
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_zip5 text;
  v_point extensions.geography;
  v_block_id text;
  v_name text;
  v_block record;
begin
  v_zip5 := public.normalize_zip5(p_zip);
  if v_zip5 is null then
    raise exception 'invalid_zip' using errcode = 'P0001';
  end if;
  if p_lat is null or p_lng is null then
    raise exception 'missing_coords' using errcode = 'P0001';
  end if;

  v_point := extensions.st_setsrid(
    extensions.st_makepoint(p_lng, p_lat), 4326
  )::extensions.geography;

  -- Record the ZIP centroid so get_blocks_near_zip can resolve this ZIP next time.
  insert into public.zip_centroids (zip5, lat, lng, city, cluster_id)
  values (
    v_zip5, p_lat, p_lng,
    nullif(btrim(coalesce(p_city, '')), ''),
    coalesce(nullif(btrim(coalesce(p_cluster_id, '')), ''), 'lake-nona')
  )
  on conflict (zip5) do update
    set lat = excluded.lat,
        lng = excluded.lng,
        city = coalesce(public.zip_centroids.city, excluded.city);

  -- One waitlist block per ZIP, deterministic id so repeat signups don't duplicate it.
  v_block_id := 'zip-' || v_zip5;
  v_name := coalesce(
    nullif(btrim(coalesce(p_display_name, '')), ''),
    nullif(btrim(coalesce(p_city, '')), '') || ' (' || v_zip5 || ')',
    'ZIP ' || v_zip5
  );

  insert into public.blocks (id, cluster_id, state, display_name, centroid)
  values (
    v_block_id,
    coalesce(nullif(btrim(coalesce(p_cluster_id, '')), ''), 'lake-nona'),
    'waitlist',
    v_name,
    v_point
  )
  on conflict (id) do update
    set centroid = coalesce(public.blocks.centroid, excluded.centroid),
        display_name = coalesce(public.blocks.display_name, excluded.display_name),
        updated_at = now();

  insert into public.block_waitlist_counts (block_id, signup_count)
  values (v_block_id, 0)
  on conflict (block_id) do nothing;

  select id, display_name, state, cluster_id
  into v_block
  from public.blocks
  where id = v_block_id;

  return jsonb_build_object(
    'block_id', v_block.id,
    'display_name', v_block.display_name,
    'block_state', v_block.state,
    'cluster_id', v_block.cluster_id
  );
end;
$$;

revoke execute on function public.create_block_for_zip(text, double precision, double precision, text, text, text) from public, anon;
grant  execute on function public.create_block_for_zip(text, double precision, double precision, text, text, text) to authenticated;
