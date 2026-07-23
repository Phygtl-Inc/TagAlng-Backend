-- The meet card's "Common Affinities" chips showed raw taxonomy ids
-- (kids_led_activity, lifestyle_social) because the read RPCs return
-- events.cohort_tags verbatim and the PWA renders them as-is. The human labels
-- already live in public.cohorts (seeded from cohorts.yaml), so map id → label
-- at read time. Storage, create_event validation, and affinity matching all
-- keep operating on canonical ids — only the display payload changes.
--   1. cohort_tag_labels(text[]): id → cohorts.label; unknown slug-shaped tags
--      get prettified (underscores → spaces, capitalized); free text passes
--      through untouched.
--   2. get_event_preview / get_event_preview_authed (based on 20260804) return
--      labelled cohort_tags.
--   3. get_similar_events (based on 20260811) returns labelled cohort_tags;
--      its shared_tags ranking still intersects raw ids.

create or replace function public.cohort_tag_labels(p_tags text[])
returns text[]
language sql
stable
set search_path = pg_catalog, public
as $$
  select coalesce(
    array_agg(
      coalesce(
        co.label,
        case
          when tag.t ~ '^[a-z0-9]+(_[a-z0-9]+)+$'
            then upper(left(replace(tag.t, '_', ' '), 1)) || substr(replace(tag.t, '_', ' '), 2)
          else tag.t
        end
      )
      order by tag.ord
    ),
    '{}'
  )
  from unnest(coalesce(p_tags, '{}')) with ordinality as tag(t, ord)
  left join public.cohorts co on co.id = tag.t;
$$;

revoke execute on function public.cohort_tag_labels(text[]) from public;
grant execute on function public.cohort_tag_labels(text[]) to anon, authenticated;

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
    'cohort_tags', public.cohort_tag_labels(v_event.cohort_tags),
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
    'cohort_tags', public.cohort_tag_labels(v_event.cohort_tags),
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

create or replace function public.get_similar_events(
  p_event_id uuid,
  p_limit integer default 3,
  p_locale text default 'en'
)
returns table (
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamptz,
  ends_at timestamptz,
  location extensions.geography,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  shared_tags integer,
  meters_away double precision
)
language sql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
  with src as (
    select e.cluster_id, e.location, e.cohort_tags, e.starts_at
    from public.events e
    where e.id = p_event_id
  )
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.ends_at,
    e.location,
    e.venue_name,
    public.cohort_tag_labels(e.cohort_tags) as cohort_tags,
    e.max_attendees,
    e.status,
    cardinality(array(
      select unnest(e.cohort_tags) intersect select unnest(src.cohort_tags)
    )) as shared_tags,
    st_distance(e.location, src.location) as meters_away
  from public.events e
  cross join src
  where e.cluster_id = src.cluster_id
    and e.id <> p_event_id
    and e.status = 'open'
    and e.starts_at > now()
  order by
    shared_tags desc,
    meters_away asc nulls last,
    abs(extract(epoch from (e.starts_at - src.starts_at))) asc
  limit greatest(1, least(coalesce(p_limit, 3), 10));
$$;
