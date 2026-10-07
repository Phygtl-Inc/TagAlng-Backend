-- Restore three changes applied to PROD by hand on 2026-10-06, on top of today's functions
--
-- WHY: four migrations were run directly on prod on 2026-10-06 19:52–19:55 UTC
-- (20261006195247, …195331, …195408, …195527) and never committed. They are committed now
-- as history (see those files). Two of their effects did not survive on prod:
--
--   * "events include in progress" (195408 + 195527). "What's happening right now? I have
--     an hour between classes" returned zero while the SJSU job fair (11:00–15:00) was
--     running 300 m away, because every event surface used
--         e.starts_at between now() and now() + p_window
--     so anything already under way was dropped. Prod LOST the fix the same night:
--     20270117120000_private_meets re-emitted the same functions with the old window.
--     Restored here on the CURRENT bodies (private meets stay hidden), and extended to
--     get_nearby_activities_authed, which the original missed.
--
--   * discover_communities (from 195331): prod runs that body (no chapter exclusion);
--     re-emitted verbatim so a fresh database matches prod. discover_communities_near from
--     195331 was superseded on prod by 20270119120000 and is left to it.
--
-- New window rule: an event is in the window if it has NOT finished and starts before the
-- window closes. With no ends_at, a timed meet runs 2 hours and a date-only meet runs the
-- day.

-- ── discover_communities · verbatim from prod (20261006195331) ─────────────────────

create or replace function public.discover_communities(
  p_user_id uuid default null::uuid,
  p_query_embedding extensions.vector default null::extensions.vector,
  p_min_similarity real default 0.55,
  p_limit integer default 10,
  p_creator_only boolean default false,
  p_include_mine boolean default false
)
returns table(place_id uuid, name text, place_type text, hq_city text,
              hq_lat double precision, hq_lng double precision, members integer,
              is_mine boolean, match_label text, match_kind text,
              similarity real, is_new boolean)
language sql
stable security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
  with me as (
    select coalesce(auth.uid(), p_user_id) as uid
  ),
  visible as (
    select vm.place_ref, vm.user_id
    from me, public.visible_place_members(me.uid) vm
  ),
  counted as (
    select v.place_ref as pid,
           count(distinct v.user_id)::int as members,
           bool_or(v.user_id = (select uid from me)) as mine
    from visible v group by v.place_ref
  ),
  by_claim as (
    select distinct on (s.pid) s.pid, s.lbl, s.kind, s.sim
    from (
      select v.place_ref as pid, c.label as lbl, 'member_claim'::text as kind,
             (1 - (c.embedding <=> p_query_embedding))::real as sim
      from visible v
      join public.user_identity_claims c
        on c.user_id = v.user_id
       and c.dismissed_at is null and c.transient = false
       and c.disclosure = 'public' and c.subject_kind = 'self'
       and c.embedding is not null
    ) s
    where s.sim >= p_min_similarity
    order by s.pid, s.sim desc
  ),
  by_blurb as (
    select p.id as pid, p.blurb as lbl, 'blurb'::text as kind,
           (1 - (p.blurb_embedding <=> p_query_embedding))::real as sim
    from public.places p
    where p.blurb_embedding is not null
      and p.governance_state in ('community_started','operator_verified')
  ),
  merged as (
    select * from by_claim
    union all
    select * from by_blurb b
    where b.sim >= p_min_similarity
      and not exists (select 1 from by_claim c where c.pid = b.pid)
  )
  select
    m.pid, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
    coalesce(c.members, 0), coalesce(c.mine, false),
    m.lbl, m.kind, m.sim,
    coalesce(c.members, 0) <= 1 as is_new
  from merged m
  join public.places p on p.id = m.pid
  left join counted c  on c.pid = m.pid
  where p_query_embedding is not null
    and (not p_creator_only or p.place_type = 'creator')
    and (p_include_mine or not coalesce(c.mine, false))
    and p.governance_state <> 'suspended'
    and not p.is_test
  order by (m.kind = 'member_claim') desc, m.sim desc, coalesce(c.members,0) desc, p.name
  limit greatest(1, least(coalesce(p_limit, 10), 50));
$function$;

-- ── the four event surfaces · current bodies (20270117120000) with the new window ────

create or replace function public.get_nearby_activities(
  p_lat double precision default null,
  p_lng double precision default null,
  p_zip text default null,
  p_window interval default '14 days',
  p_locale text default 'en',
  p_limit int default 20
)
returns table (
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamptz,
  has_time boolean,
  ends_at timestamptz,
  duration_minutes int,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  cover_emoji text,
  distance_meters double precision,
  distance_text text,
  affinity_match_count int,
  affinity_match_label text,
  participant_count int,
  maybe_count int,
  participant_preview jsonb
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
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

    select z.lat, z.lng
    into v_lat, v_lng
    from public.zip_centroids z
    where z.zip5 = v_zip5;

    if not found then
      raise exception 'zip_not_found' using errcode = 'P0001';
    end if;
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.has_time,
    e.ends_at,
    case
      when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1)
    end as duration_minutes,
    e.venue_name,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    e.cover_image_url,
    e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    public.humanize_distance_text(
      extensions.st_distance(e.location, v_point)::double precision, p_locale
    ) as distance_text,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_match_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) > 0 then
        concat(cardinality(coalesce(e.cohort_tags, '{}')), ' affinities')
      else null
    end as affinity_match_label,
    coalesce((
      select count(*)::int
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going'
    ), 0) as participant_count,
    coalesce((
      select count(*)::int
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'maybe'
    ), 0) as maybe_count,
    coalesce((
      select jsonb_agg(jsonb_build_object(
        'user_id', null,
        'nickname', null,
        'avatar_url', null,
        'is_blurred', true,
        'event_count', coalesce(p.event_count, 0),
        'weeks_here', p.weeks_here,
        'about_tags', p.about_tags,
        'shared_claim_count', 0
      ) order by p.event_count desc, p.weeks_here desc)
      from (
        select
          u.id,
          (select count(*) from public.event_requests er2
            where er2.requester_id = u.id
              and er2.status in ('approved','attended')) as event_count,
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
        where er.event_id = e.id
          and er.status in ('approved', 'attended')
          and er.rsvp_status = 'going'
        order by event_count desc, u.created_at asc
        limit 6
      ) p
    ), '[]'::jsonb) as participant_preview
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
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

create or replace function public.get_nearby_activities_authed(
  p_lat double precision default null,
  p_lng double precision default null,
  p_zip text default null,
  p_window interval default '14 days',
  p_locale text default 'en',
  p_limit int default 20
)
returns table (
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamptz,
  has_time boolean,
  ends_at timestamptz,
  duration_minutes int,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  cover_emoji text,
  distance_meters double precision,
  distance_text text,
  affinity_match_count int,
  affinity_total_count int,
  affinity_match_label text,
  fit_score numeric,
  participant_count int,
  maybe_count int,
  my_request_status text,
  my_rsvp_status text,
  participant_preview jsonb
)
language plpgsql
security definer
set search_path = pg_catalog, public, extensions
as $$
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

    select z.lat, z.lng
    into v_lat, v_lng
    from public.zip_centroids z
    where z.zip5 = v_zip5;

    if not found then
      raise exception 'zip_not_found' using errcode = 'P0001';
    end if;
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(v_lng, v_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.has_time,
    e.ends_at,
    case
      when e.ends_at is null then null
      else greatest(round(extract(epoch from e.ends_at - e.starts_at) / 60)::int, 1)
    end as duration_minutes,
    e.venue_name,
    e.cohort_tags,
    e.max_attendees,
    e.status,
    e.cover_image_url,
    e.cover_emoji,
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    public.humanize_distance_text(
      extensions.st_distance(e.location, v_point)::double precision, p_locale
    ) as distance_text,
    am.matched_count as affinity_match_count,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_total_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) = 0 then null
      else concat(am.matched_count, '/', cardinality(coalesce(e.cohort_tags, '{}')), ' affinities match')
    end as affinity_match_label,
    -- 0-1 over the same proven intersection as affinity_match_count; null = unscored
    -- (no public claims, or an untagged meet). See public.event_viewer_fit.
    am.fit_score as fit_score,
    coalesce((
      select count(*)::int
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going'
    ), 0) as participant_count,
    coalesce((
      select count(*)::int
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'maybe'
    ), 0) as maybe_count,
    (
      select er.status
      from public.event_requests er
      where er.event_id = e.id
        and er.requester_id = v_caller
    ) as my_request_status,
    (
      select er.rsvp_status
      from public.event_requests er
      where er.event_id = e.id
        and er.requester_id = v_caller
    ) as my_rsvp_status,
    coalesce((
      select jsonb_agg(jsonb_build_object(
        'user_id', p.id,
        'nickname', p.nickname,
        'avatar_url', p.avatar_url,
        'is_blurred', false,
        'event_count', coalesce(p.event_count, 0),
        'weeks_here', p.weeks_here,
        'about_tags', p.about_tags,
        'shared_claim_count', p.shared_claim_count
      ) order by p.shared_claim_count desc, p.event_count desc, p.weeks_here desc)
      from (
        select
          u.id,
          u.nickname,
          u.profile_photo_url as avatar_url,
          (select count(*) from public.event_requests er2
            where er2.requester_id = u.id
              and er2.status in ('approved','attended')) as event_count,
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
        where er.event_id = e.id
          and er.status in ('approved', 'attended')
          and er.rsvp_status = 'going'
        order by event_count desc, u.created_at asc
        limit 6
      ) p
    ), '[]'::jsonb) as participant_preview
  from public.events e
  left join lateral public.event_viewer_fit(e.cohort_tags, v_caller) am on true
  where e.status = 'open'
    and not e.is_private  -- §29: a private meet travels by invite link only
    and e.location is not null
    and e.starts_at < now() + p_window
    -- In progress counts (20261006195408): not finished yet, and starts before the window
    -- closes. Date-only meets run the day, not two hours past midnight.
    and coalesce(e.ends_at,
          e.starts_at + case when e.has_time then interval '2 hours'
                             else interval '1 day' end) > now()
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

create or replace function public.get_activities_near_point(
  p_lat double precision,
  p_lng double precision,
  p_radius_meters double precision default 40000,
  p_window interval default '14 days'::interval,
  p_locale text default 'en',
  p_limit integer default 20
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
  maybe_count integer
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_point extensions.geography;
  v_radius double precision := greatest(100, least(coalesce(p_radius_meters, 40000), 200000));
  -- Region-stripped locale. humanize_distance_text() normalizes internally, so
  -- without this the same p_locale means two different things in one call:
  -- 'pt-BR' would pick the Portuguese distance label while missing every
  -- pt translation and silently falling back to the English title.
  v_lang text := lower(coalesce(nullif(split_part(coalesce(p_locale, 'en'), '-', 1), ''), 'en'));
begin
  if p_lat is null or p_lng is null then
    raise exception 'location_required' using errcode = 'P0001';
  end if;
  if p_lat not between -90 and 90 or p_lng not between -180 and 180 then
    raise exception 'invalid_location' using errcode = 'P0001';
  end if;

  v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;

  return query
  select
    e.id,
    e.host_id,
    -- Exact tag first ('pt-BR' if it was ever stored that way), then the base
    -- language, then the English-canonical column.
    coalesce(
      e.title_translations->>p_locale,
      e.title_translations->>v_lang,
      e.title
    ) as title,
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
    ), 0) as maybe_count
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
    -- THE CAP. st_dwithin is index-assisted (GiST on geography).
    and extensions.st_dwithin(e.location, v_point, v_radius)
  order by extensions.st_distance(e.location, v_point) asc, e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

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
  order by e.embedding <=> p_query_embedding asc, e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 50), 200));
end;
$function$;

-- ============================================================================
-- ROLLBACK: re-run the function sections of 20270117120000_private_meets (events) and
-- 20270104120000 (discover_communities).
-- ============================================================================
