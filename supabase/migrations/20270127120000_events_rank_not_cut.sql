-- ============================================================================
-- search_events_semantic ORDERS by meaning-less-distance instead of being CUT by a floor.
--
-- Prod 2026-10-08: browse passed p_min_similarity ~0.495 (the admission floor at distance
-- zero). A one-word ask scores low against EVERY meet — a word against a paragraph — so the
-- cut removed exact matches before the topic matcher could judge them: "music" vs a Latin
-- Jazz Concert scored 0.41, "jazz" vs a guitar jam 12 km away 0.42. On 38 real prod meets
-- and 20 short asks the floor let through 13% of the right meets; ranking let through 58-63%.
--
-- New optional p_distance_k / p_distance_base_m: when p_distance_k is set, rows are ordered
-- by  similarity - k * max(0, ln(distance / base))  — the same log-distance curve the
-- admission floor used, applied as an ORDER, not a gate — and that value is returned as
-- rank_score. With p_distance_k NULL (the default) the function behaves exactly as before,
-- so a worker deployed before this migration keeps working, and the new worker passes
-- p_min_similarity 0 and lets the matcher decide.
--
-- Signature change (two args, one output column), so drop + create; grants re-stated.
-- ROLLBACK: drop this signature and re-run the body from 20270122120000.
-- ============================================================================

drop function if exists public.search_events_semantic(
  extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer, uuid
);

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
  p_exclude_host_id uuid default null,
  p_distance_k      real default null,
  p_distance_base_m double precision default 8000
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
  similarity real,
  rank_score real
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
    end as similarity,
    -- Meaning, less a distance penalty: the curve the admission floor used to CUT with,
    -- now used to ORDER. A farther meet has to be more on-topic to outrank a nearer one.
    -- NULL when not embedded (ranked last, never hidden) or when no penalty was asked for.
    case
      when e.embedding is null or p_distance_k is null then null
      else (
        (1 - (e.embedding <=> p_query_embedding))
        - p_distance_k * greatest(
            0,
            ln(greatest(extensions.st_distance(e.location, v_point), 1)
               / greatest(coalesce(p_distance_base_m, 8000), 1))
          )
      )::real
    end as rank_score
  from public.events e
  where e.status = 'open'
    and not e.is_private  -- §29: a private meet travels by invite link only
    and e.location is not null
    and e.starts_at < now() + p_window
    -- In progress counts (20261006195408): not finished yet, and starts before the window
    -- closes. Date-only meets run the day, not two hours past midnight.
    and coalesce(e.ends_at,
          e.starts_at + case when e.has_time then interval '2 hours'
                             else interval '1 day' end) > now()
    and extensions.st_dwithin(e.location, v_point, v_radius)
    and (p_circle_place_id is null or e.circle_place_ref = p_circle_place_id)
    -- Your own meets, dropped before the limit rather than after it. Browse has never shown
    -- them; doing it here is what makes the truncation flag trustworthy.
    and (p_exclude_host_id is null or e.host_id <> p_exclude_host_id)
    and (
      e.embedding is null
      or (1 - (e.embedding <=> p_query_embedding)) >= coalesce(p_min_similarity, 0)
    )
  order by
    -- With a penalty: best combined rank first. Without one: the old order, unchanged.
    case when p_distance_k is null then null else
      (1 - (e.embedding <=> p_query_embedding))
      - p_distance_k * greatest(
          0,
          ln(greatest(extensions.st_distance(e.location, v_point), 1)
             / greatest(coalesce(p_distance_base_m, 8000), 1))
        )
    end desc nulls last,
    e.embedding <=> p_query_embedding asc,
    e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 50), 200));
end;
$function$;

comment on function public.search_events_semantic(extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer, uuid, real, double precision) is
  'Meets inside a radius, ordered by MEANING less a log-distance penalty when p_distance_k is '
  'given (rank_score), else by meaning alone. Returns similarity=NULL for meets not yet '
  'embedded, ranked last, never hidden. Row count = p_limit means truncated.';

grant execute on function public.search_events_semantic(extensions.vector, double precision, double precision, double precision, interval, text, uuid, real, integer, uuid, real, double precision)
  to anon, authenticated, service_role;
