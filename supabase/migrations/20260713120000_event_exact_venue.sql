-- Exact venue for hosted events: store the precise address + Google place_id (alongside
-- the existing coarse `location` point), so neighbors can navigate to the right pin.
-- The lat/lng passed to create_event is now the EXACT picked place (not re-geocoded).

alter table public.events
  add column if not exists venue_address text,
  add column if not exists place_id text;

create or replace function public.create_event(p_fields jsonb)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public, extensions
as $$
declare
  new_id uuid;
  v_lat double precision;
  v_lng double precision;
  v_tags text[];
  v_cohost uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_lat := (p_fields->>'lat')::double precision;
  v_lng := (p_fields->>'lng')::double precision;

  if v_lat is null or v_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;

  if p_fields->>'title' is null or char_length(p_fields->>'title') < 1 then
    raise exception 'title_required' using errcode = 'P0001';
  end if;

  select coalesce(array_agg(t), '{}')
  into v_tags
  from jsonb_array_elements_text(coalesce(p_fields->'cohort_tags', '[]'::jsonb)) as t;

  if not public.validate_event_cohort_tags(v_tags) then
    raise exception 'invalid_cohort' using errcode = 'P0001';
  end if;

  v_cohost := nullif(p_fields->>'cohost_id', '')::uuid;
  if v_cohost is not null then
    if not exists (
      select 1
      from public.event_cohost_invites i
      where i.host_id = auth.uid()
        and i.candidate_id = v_cohost
        and i.status = 'accepted'
    ) then
      raise exception 'cohost_not_accepted' using errcode = 'P0001';
    end if;
  end if;

  insert into public.events (
    host_id,
    cohost_id,
    cluster_id,
    block_id,
    title,
    description,
    starts_at,
    ends_at,
    location,
    venue_name,
    venue_address,
    place_id,
    cohort_tags,
    max_attendees,
    cover_image_url
  )
  values (
    auth.uid(),
    v_cohost,
    coalesce(p_fields->>'cluster_id', 'lake-nona'),
    p_fields->>'block_id',
    p_fields->>'title',
    p_fields->>'description',
    coalesce((p_fields->>'starts_at')::timestamptz, now() + interval '7 days'),
    (p_fields->>'ends_at')::timestamptz,
    extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography,
    p_fields->>'venue_name',
    p_fields->>'venue_address',
    p_fields->>'place_id',
    v_tags,
    (p_fields->>'max_attendees')::integer,
    p_fields->>'cover_image_url'
  )
  returning id into new_id;

  if v_cohost is not null then
    update public.event_cohost_invites i
    set event_id = new_id
    where i.host_id = auth.uid()
      and i.candidate_id = v_cohost
      and i.status = 'accepted'
      and i.event_id is null;
  end if;

  return new_id;
end;
$$;
