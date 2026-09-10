-- ============================================================================
-- Fix: standing/timeless gatherings invisible in discovery
-- ----------------------------------------------------------------------------
-- BUG (found in user interview 2026-07-28, confirmed in prod):
--   A user searched for local gatherings and got "nothing's popped up in your
--   area just yet" even though gatherings exist. Root cause is the time filter
--   in get_nearby_activities / get_nearby_activities_authed:
--
--       and e.starts_at between now() and now() + p_window
--
--   This BETWEEN drops two legitimate classes of open events:
--     1. TIMELESS / STANDING gatherings (has_time = false) — Lana's "recurring
--        community" events (e.g., "Sunday picnic"). Their starts_at is only a
--        placeholder; once it drifts into the past the event vanishes from
--        discovery forever. (Prod: 4 of 5 timeless open events currently hidden.)
--     2. ONGOING timed events whose starts_at just passed but that haven't ended.
--
-- FIX: don't time-filter timeless gatherings at all; for timed events, show
--   those upcoming within the window OR still ongoing (not yet ended).
--   Everything else in both functions is unchanged. Non-destructive
--   (CREATE OR REPLACE); no schema change; instantly reversible.
-- VERIFIED: run in a rolled-back tx against prod — both functions compile and
--   the previously-hidden timeless gatherings now return (4 -> 8 rows).
-- REAL SCHEMA (verified 2026-07-28): events(status, location, starts_at NOT NULL,
--   has_time NOT NULL default true, ends_at nullable).
-- ROLLBACK: re-apply the previous definitions (both fns) with the original
--   `e.starts_at between now() and now() + p_window` predicate.
-- ============================================================================

begin;

-- ─── 1) ANON: get_nearby_activities ─────────────────────────────────────────
create or replace function public.get_nearby_activities(
  p_lat double precision default null::double precision,
  p_lng double precision default null::double precision,
  p_zip text default null::text,
  p_window interval default '14 days'::interval,
  p_locale text default 'en'::text,
  p_limit integer default 20)
returns table(id uuid, host_id uuid, title text, description text,
  starts_at timestamp with time zone, has_time boolean, ends_at timestamp with time zone,
  duration_minutes integer, venue_name text, cohort_tags text[], max_attendees integer,
  status text, cover_image_url text, cover_emoji text, distance_meters double precision,
  distance_text text, affinity_match_count integer, affinity_match_label text,
  participant_count integer, maybe_count integer, participant_preview jsonb)
language plpgsql security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_lat double precision := p_lat;
  v_lng double precision := p_lng;
  v_zip5 text;
  v_point extensions.geography;
begin
  if v_lat is null or v_lng is null then
    v_zip5 := public.normalize_zip5(p_zip);
    if v_zip5 is null then
      raise exception 'location_required' using errcode = 'P0001';
    end if;
    select z.lat, z.lng into v_lat, v_lng
    from public.zip_centroids z where z.zip5 = v_zip5;
    if not found then
      raise exception 'zip_not_found' using errcode = 'P0001';
    end if;
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography;

  return query
  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.has_time, e.ends_at,
    case when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1) end as duration_minutes,
    e.venue_name, e.cohort_tags, e.max_attendees, e.status, e.cover_image_url, e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    concat(greatest(1, round(extensions.st_distance(e.location, v_point) / 80)::int), ' min walk') as distance_text,
    null::int as affinity_match_count,
    null::text as affinity_match_label,
    coalesce((select count(*)::int from public.event_requests er
      where er.event_id = e.id and er.status in ('approved','attended') and er.rsvp_status = 'going'), 0) as participant_count,
    coalesce((select count(*)::int from public.event_requests er
      where er.event_id = e.id and er.status in ('approved','attended') and er.rsvp_status = 'maybe'), 0) as maybe_count,
    coalesce((
      select jsonb_agg(jsonb_build_object(
        'user_id', null, 'nickname', null, 'avatar_url', null, 'is_blurred', true,
        'event_count', coalesce(p.event_count, 0), 'weeks_here', p.weeks_here,
        'about_tags', p.about_tags, 'shared_claim_count', 0)
        order by p.event_count desc, p.weeks_here desc)
      from (
        select u.id,
          (select count(*) from public.event_requests er2
            where er2.requester_id = u.id and er2.status in ('approved','attended')) as event_count,
          floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
          coalesce((select jsonb_agg(sub.label order by sub.confidence desc)
            from (select distinct c.label, c.confidence from public.user_identity_claims c
              where c.user_id = u.id and c.dismissed_at is null and c.disclosure = 'public'
              order by c.confidence desc limit 5) sub), '[]'::jsonb) as about_tags
        from public.users u
        join public.event_requests er on er.requester_id = u.id
        where er.event_id = e.id and er.status in ('approved','attended') and er.rsvp_status = 'going'
        order by event_count desc, u.created_at asc limit 6) p
    ), '[]'::jsonb) as participant_preview
  from public.events e
  where e.status = 'open'
    and e.location is not null
    -- FIX: timeless standing gatherings are always eligible; timed events show
    -- if upcoming within the window OR still ongoing (not yet ended).
    and (
      e.has_time = false
      or (e.starts_at <= now() + p_window and coalesce(e.ends_at, e.starts_at) >= now())
    )
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

-- ─── 2) AUTHED: get_nearby_activities_authed ────────────────────────────────
create or replace function public.get_nearby_activities_authed(
  p_lat double precision default null::double precision,
  p_lng double precision default null::double precision,
  p_zip text default null::text,
  p_window interval default '14 days'::interval,
  p_locale text default 'en'::text,
  p_limit integer default 20)
returns table(id uuid, host_id uuid, title text, description text,
  starts_at timestamp with time zone, has_time boolean, ends_at timestamp with time zone,
  duration_minutes integer, venue_name text, cohort_tags text[], max_attendees integer,
  status text, cover_image_url text, cover_emoji text, distance_meters double precision,
  distance_text text, affinity_match_count integer, affinity_total_count integer,
  affinity_match_label text, participant_count integer, maybe_count integer,
  my_request_status text, my_rsvp_status text, participant_preview jsonb)
language plpgsql security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_caller uuid := auth.uid();
  v_lat double precision := p_lat;
  v_lng double precision := p_lng;
  v_zip5 text;
  v_point extensions.geography;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if v_lat is null or v_lng is null then
    v_zip5 := public.normalize_zip5(p_zip);
    if v_zip5 is null then
      raise exception 'location_required' using errcode = 'P0001';
    end if;
    select z.lat, z.lng into v_lat, v_lng
    from public.zip_centroids z where z.zip5 = v_zip5;
    if not found then
      raise exception 'zip_not_found' using errcode = 'P0001';
    end if;
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography;

  return query
  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.has_time, e.ends_at,
    case when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1) end as duration_minutes,
    e.venue_name, e.cohort_tags, e.max_attendees, e.status, e.cover_image_url, e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    concat(greatest(1, round(extensions.st_distance(e.location, v_point) / 80)::int), ' min walk') as distance_text,
    am.matched as affinity_match_count,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_total_count,
    case when cardinality(coalesce(e.cohort_tags, '{}')) = 0 then null
      else concat(am.matched, '/', cardinality(coalesce(e.cohort_tags, '{}')), ' affinities match') end as affinity_match_label,
    coalesce((select count(*)::int from public.event_requests er
      where er.event_id = e.id and er.status in ('approved','attended') and er.rsvp_status = 'going'), 0) as participant_count,
    coalesce((select count(*)::int from public.event_requests er
      where er.event_id = e.id and er.status in ('approved','attended') and er.rsvp_status = 'maybe'), 0) as maybe_count,
    (select er.status from public.event_requests er
      where er.event_id = e.id and er.requester_id = v_caller) as my_request_status,
    case when e.host_id = v_caller then e.host_rsvp_status
      else (select er.rsvp_status from public.event_requests er
        where er.event_id = e.id and er.requester_id = v_caller) end as my_rsvp_status,
    coalesce((
      select jsonb_agg(jsonb_build_object(
        'user_id', p.id, 'nickname', p.nickname, 'avatar_url', p.avatar_url, 'is_blurred', false,
        'event_count', coalesce(p.event_count, 0), 'weeks_here', p.weeks_here,
        'about_tags', p.about_tags, 'shared_claim_count', p.shared_claim_count)
        order by p.shared_claim_count desc, p.event_count desc, p.weeks_here desc)
      from (
        select u.id, u.nickname, u.profile_photo_url as avatar_url,
          (select count(*) from public.event_requests er2
            where er2.requester_id = u.id and er2.status in ('approved','attended')) as event_count,
          floor(extract(epoch from now() - u.created_at) / 604800)::int as weeks_here,
          coalesce((select jsonb_agg(sub.label order by sub.confidence desc)
            from (select distinct c.label, c.confidence from public.user_identity_claims c
              where c.user_id = u.id and c.dismissed_at is null and c.disclosure = 'public'
              order by c.confidence desc limit 5) sub), '[]'::jsonb) as about_tags,
          coalesce((select count(*)::int
            from public.user_identity_claims c1
            join public.user_identity_claims c2 on c1.concept = c2.concept
            where c1.user_id = v_caller and c2.user_id = u.id
              and c1.dismissed_at is null and c2.dismissed_at is null
              and c1.disclosure = 'public' and c2.disclosure = 'public'), 0) as shared_claim_count
        from public.users u
        join public.event_requests er on er.requester_id = u.id
        where er.event_id = e.id and er.status in ('approved','attended') and er.rsvp_status = 'going'
        order by event_count desc, u.created_at asc limit 6) p
    ), '[]'::jsonb) as participant_preview
  from public.events e
  left join lateral (
    select count(distinct tag)::int as matched
    from unnest(coalesce(e.cohort_tags, '{}')) as tag
    where exists (
      select 1 from public.user_identity_claims c
      where c.user_id = v_caller and c.dismissed_at is null and c.disclosure = 'public'
        and public.cohort_tag_matches_claim(tag, c.concept, c.synonyms))
  ) am on true
  where e.status = 'open'
    and e.location is not null
    -- FIX: timeless standing gatherings are always eligible; timed events show
    -- if upcoming within the window OR still ongoing (not yet ended).
    and (
      e.has_time = false
      or (e.starts_at <= now() + p_window and coalesce(e.ends_at, e.starts_at) >= now())
    )
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

commit;

-- ============================================================================
-- TEST PLAN (run after apply):
--   -- timeless standing gatherings now appear (were hidden when starts_at drifted past):
--   select id, title, has_time, starts_at
--   from get_nearby_activities(28.40, -81.27, null, interval '14 days', 'es', 20)
--   where has_time = false;
--   -- ongoing timed events (started, not yet ended) now appear:
--   select id, title from get_nearby_activities_authed(28.40, -81.27) where starts_at < now();
-- NOTE: separate follow-ups from the same interview (NOT in this PR):
--   • Lana replied in English during a Spanish session — worker/locale plumbing
--     (users.locale exists, default 'en'; confirm it's set from the conversation).
--   • Robotic TTS voice; frontend search-scroll + "nueva petición" reset bugs.
-- ROLLBACK: re-CREATE OR REPLACE both functions with the original predicate
--   `and e.starts_at between now() and now() + p_window`.
-- ============================================================================
