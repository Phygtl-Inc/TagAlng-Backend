-- search_events_semantic gains p_exclude_host_id — drop your own meets in SQL, not after.
--
-- PR #151 drops the caller's own meets in Python once the page has come back. They occupy
-- slots in that page first, so the truncation flag ("the set was cut off, do not conclude
-- nothing further out matched") fires on a page that was never actually full of candidates.
-- That flag is the one thing standing between an honest empty result and a silent lie, so a
-- false positive on it is worse than the wasted slots.
--
-- One predicate fixes both. Host exclusion happens before the limit, so the page is p_limit
-- real candidates and truncation means what it says.
--
-- The 9-argument version is DROPPED rather than left alongside: a new trailing parameter with
-- a default would make every existing 9-argument call ambiguous (PostgreSQL cannot choose
-- between the two candidates and raises 42725 at call time, not at migration time). Both
-- signatures are ours and the only caller is the worker, so replacing outright is safe.

drop function if exists public.search_events_semantic(
  extensions.vector, double precision, double precision, double precision,
  interval, text, uuid, real, integer);

create or replace function public.search_events_semantic(
  p_query_embedding extensions.vector(768),
  p_lat             double precision,
  p_lng             double precision,
  p_radius_meters   double precision default 40000,
  p_window          interval default '14 days'::interval,
  p_locale          text default 'en',
  p_circle_place_id uuid default null,
  p_min_similarity  real default 0,
  p_limit           integer default 50,
  p_exclude_host_id uuid default null
)
returns table(
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamp with time zone,
  has_time boolean,
  ends_at timestamp with time zone,
  duration_minutes integer,
  venue_name text,
  block_id text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  cover_emoji text,
  distance_meters double precision,
  distance_text text,
  participant_count integer,
  maybe_count integer,
  similarity real
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_point extensions.geography;
  v_radius double precision := greatest(100, least(coalesce(p_radius_meters, 40000), 200000));
  v_lang text := lower(coalesce(nullif(split_part(coalesce(p_locale, 'en'), '-', 1), ''), 'en'));
begin
  if p_lat is null or p_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;
  if p_lat not between -90 and 90 or p_lng not between -180 and 180 then
    raise exception 'invalid_location' using errcode = 'P0001';
  end if;
  if p_query_embedding is null then
    raise exception 'query_embedding_required' using errcode = 'P0001';
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title_translations->>v_lang, e.title) as title,
    coalesce(
      e.description_translations->>p_locale,
      e.description_translations->>v_lang,
      e.description
    ) as description,
    e.starts_at,
    e.has_time,
    e.ends_at,
    case
      when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1)
    end as duration_minutes,
    e.venue_name,
    e.block_id,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    e.cover_image_url,
    e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    public.humanize_distance_text(
      extensions.st_distance(e.location, v_point)::double precision, p_locale
    ) as distance_text,
    coalesce((
      select count(*)::int from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going'
    ), 0) as participant_count,
    coalesce((
      select count(*)::int from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'maybe'
    ), 0) as maybe_count,
    case
      when e.embedding is null then null
      else (1 - (e.embedding <=> p_query_embedding))::real
    end as similarity
  from public.events e
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
    and extensions.st_dwithin(e.location, v_point, v_radius)
    and (p_circle_place_id is null or e.circle_place_ref = p_circle_place_id)
    -- Your own meets, dropped before the limit rather than after it. Browse has never shown
    -- them; doing it here is what makes the truncation flag trustworthy.
    and (p_exclude_host_id is null or e.host_id <> p_exclude_host_id)
    and (
      e.embedding is null
      or (1 - (e.embedding <=> p_query_embedding)) >= coalesce(p_min_similarity, 0)
    )
  order by e.embedding <=> p_query_embedding asc, e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 50), 200));
end;
$function$;

comment on function public.search_events_semantic(extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer, uuid) is
  'Meets ranked by MEANING inside a radius, with distance returned as data rather than used '
  'as the ranking. The candidate source for C3 distance admission — get_activities_near_point '
  'orders by distance and caps at 20, which discards the far-but-relevant row C3 exists to '
  'admit. Returns similarity=NULL for meets not yet embedded, ranked last, never hidden. '
  'p_exclude_host_id drops the caller''s own meets before the limit, so a full page means the '
  'topical set genuinely exceeded it (contract D9-A9). Row count = p_limit means truncated; '
  'say so rather than concluding nothing further out matched.';

grant execute on function public.search_events_semantic(extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer, uuid)
  to anon, authenticated, service_role;

-- ============================================================================
-- ROLLBACK — restore the 9-argument version from 20261202120000, then:
--   drop function if exists public.search_events_semantic(
--     extensions.vector, double precision, double precision, double precision,
--     interval, text, uuid, real, integer, uuid);
-- Callers that never pass p_exclude_host_id behave identically either way.
-- ============================================================================
