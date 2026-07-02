-- Host "bring" list: items attendees should bring (stroller, coffee mug, blanket…),
-- captured in the 4/4 card of the host quick-setup carousel and surfaced as the meet's
-- pinned list. Stored as a text[]. This migration:
--   1. adds events.bring_items,
--   2. recreates create_event (based on 20260714) to persist it, and
--   3. recreates get_event_preview / get_event_preview_authed (based on 20260723) to
--      return it, so the shared /meet/[id] card can show the list.
-- Placed last (after 20260803) so it applies cleanly on top of the existing chain.

alter table public.events
  add column if not exists bring_items text[] not null default '{}';

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
  v_bring text[];
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

  -- Bring list: free-text items, trimmed of blanks and capped to 12. Order preserved.
  select coalesce(array_agg(t order by ord), '{}')
  into v_bring
  from jsonb_array_elements_text(coalesce(p_fields->'bring_items', '[]'::jsonb))
       with ordinality as e(t, ord)
  where char_length(btrim(t)) > 0 and ord <= 12;

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
    auto_approve,
    allow_attendee_share,
    bring_items,
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
    coalesce((p_fields->>'auto_approve')::boolean, false),
    coalesce((p_fields->>'allow_attendee_share')::boolean, true),
    coalesce(v_bring, '{}'),
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

create or replace function public.get_event_preview(
  p_event_id uuid,
  p_lat double precision default null,
  p_lng double precision default null,
  p_locale text default 'en'
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_event record;
  v_point extensions.geography;
  v_distance double precision;
  v_distance_text text;
  v_participants jsonb;
  v_total int;
begin
  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status = 'open';

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null then
    v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;
    v_distance := extensions.st_distance(v_event.location, v_point)::double precision;
    v_distance_text := concat(greatest(1, round(v_distance / 80)::int), ' min walk');
  else
    v_distance := null;
    v_distance_text := null;
  end if;

  v_total := cardinality(coalesce(v_event.cohort_tags, '{}'));

  select coalesce(jsonb_agg(jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_blurred', true,
      'event_count', p.event_count,
      'weeks_here', p.weeks_here,
      'about_tags', p.about_tags,
      'shared_claim_count', 0
    ) order by p.event_count desc, p.weeks_here desc), '[]'::jsonb)
  into v_participants
  from (
    select
      u.id,
      (select count(*) from public.event_requests er2
        where er2.requester_id = u.id
          and er2.status in ('approved', 'attended')) as event_count,
      floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
      coalesce((
        select jsonb_agg(sub.label order by sub.confidence desc)
        from (
          select distinct c.label, c.confidence
          from public.user_identity_claims c
          where c.user_id = u.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc
          limit 5
        ) sub
      ), '[]'::jsonb) as about_tags
    from public.users u
    join public.event_requests er on er.requester_id = u.id
    where er.event_id = p_event_id
      and er.status in ('approved', 'attended')
    order by event_count desc, u.created_at asc
    limit 20
  ) p;

  return jsonb_build_object(
    'event_id', v_event.id,
    'host_id', v_event.host_id,
    'title', v_event.title,
    'description', v_event.description,
    'starts_at', v_event.starts_at,
    'ends_at', v_event.ends_at,
    'duration_minutes', case when v_event.ends_at is null then null else greatest(round(extract(epoch from v_event.ends_at - v_event.starts_at) / 60)::int, 1) end,
    'venue_name', v_event.venue_name,
    'venue_address', v_event.venue_address,
    'place_id', v_event.place_id,
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
    'bring_items', coalesce(v_event.bring_items, '{}'),
    'status', v_event.status,
    'cover_image_url', v_event.cover_image_url,
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_total,
    'affinity_match_label', case when v_total > 0 then concat(v_total, ' affinities') else null end,
    'is_authenticated', false,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended')), 0),
    'participants', v_participants
  );
end;
$$;

create or replace function public.get_event_preview_authed(
  p_event_id uuid,
  p_lat double precision default null,
  p_lng double precision default null,
  p_locale text default 'en'
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_caller uuid := auth.uid();
  v_event record;
  v_point extensions.geography;
  v_distance double precision;
  v_distance_text text;
  v_participants jsonb;
  v_total int;
  v_matched int;
  v_my_status text;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status = 'open';

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null then
    v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;
    v_distance := extensions.st_distance(v_event.location, v_point)::double precision;
    v_distance_text := concat(greatest(1, round(v_distance / 80)::int), ' min walk');
  else
    v_distance := null;
    v_distance_text := null;
  end if;

  v_total := cardinality(coalesce(v_event.cohort_tags, '{}'));

  select count(distinct tag)::int
  into v_matched
  from unnest(coalesce(v_event.cohort_tags, '{}')) as tag
  where exists (
    select 1
    from public.user_identity_claims c
    where c.user_id = v_caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and (c.concept = tag or tag = any(c.synonyms))
  );

  select er.status
  into v_my_status
  from public.event_requests er
  where er.event_id = p_event_id
    and er.requester_id = v_caller;

  select coalesce(jsonb_agg(jsonb_build_object(
      'user_id', p.id,
      'nickname', p.nickname,
      'avatar_url', p.avatar_url,
      'is_blurred', false,
      'event_count', p.event_count,
      'weeks_here', p.weeks_here,
      'about_tags', p.about_tags,
      'shared_claim_count', p.shared_claim_count
    ) order by p.shared_claim_count desc, p.event_count desc, p.weeks_here desc), '[]'::jsonb)
  into v_participants
  from (
    select
      u.id,
      u.nickname,
      u.profile_photo_url as avatar_url,
      (select count(*) from public.event_requests er2
        where er2.requester_id = u.id
          and er2.status in ('approved', 'attended')) as event_count,
      floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
      coalesce((
        select jsonb_agg(sub.label order by sub.confidence desc)
        from (
          select distinct c.label, c.confidence
          from public.user_identity_claims c
          where c.user_id = u.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc
          limit 5
        ) sub
      ), '[]'::jsonb) as about_tags,
      coalesce((
        select count(*)::int
        from public.user_identity_claims c1
        join public.user_identity_claims c2 on c1.concept = c2.concept
        where c1.user_id = v_caller
          and c2.user_id = u.id
          and c1.dismissed_at is null
          and c2.dismissed_at is null
          and c1.disclosure = 'public'
          and c2.disclosure = 'public'
      ), 0) as shared_claim_count
    from public.users u
    join public.event_requests er on er.requester_id = u.id
    where er.event_id = p_event_id
      and er.status in ('approved', 'attended')
    order by event_count desc, u.created_at asc
    limit 20
  ) p;

  return jsonb_build_object(
    'event_id', v_event.id,
    'host_id', v_event.host_id,
    'title', v_event.title,
    'description', v_event.description,
    'starts_at', v_event.starts_at,
    'ends_at', v_event.ends_at,
    'duration_minutes', case when v_event.ends_at is null then null else greatest(round(extract(epoch from v_event.ends_at - v_event.starts_at) / 60)::int, 1) end,
    'venue_name', v_event.venue_name,
    'venue_address', v_event.venue_address,
    'place_id', v_event.place_id,
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
    'bring_items', coalesce(v_event.bring_items, '{}'),
    'status', v_event.status,
    'cover_image_url', v_event.cover_image_url,
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_matched,
    'affinity_total_count', v_total,
    'affinity_match_label', case when v_total = 0 then null else concat(v_matched, '/', v_total, ' affinities match') end,
    'is_authenticated', true,
    'my_request_status', v_my_status,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended')), 0),
    'participants', v_participants
  );
end;
$$;
