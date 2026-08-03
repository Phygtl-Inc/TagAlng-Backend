-- ============================================================================
-- DISTANCE LABELS: honest at any range
--
-- PROBLEM (reproduced live on prod 2026-08-03)
--   Four RPCs render distance with an uncapped walking formula:
--       concat(greatest(1, round(st_distance(...) / 80)::int), ' min walk')
--   80 m/min with no upper bound, so a faraway event is still described as a
--   walk. Calling get_event_preview for the (real, open) "Under 8s Pizza
--   Playdate" -- venue "University of Johannesburg", 13,125 km from Lake Nona --
--   with a Lake Nona viewer returns:
--       distance_text: '164069 min walk'
--   That is 114 days of continuous walking, on the meet page a shared link opens.
--
--   This is NOT the bad-geocode bug. Cancelling the duplicate row in
--   20260923120000 removed one of the two Johannesburg events; the surviving one
--   still renders the string above. Deleting data would have hidden the defect
--   without fixing it -- the formatter is wrong for any distance over ~1.6 km,
--   including a perfectly legitimate event two towns over.
--
-- FIX
--   Swap the inline formula for public.humanize_distance_text(meters, locale)
--   (20260920120000): walking only under 1600 m, then miles for EN and km for
--   ES/PT. All four RPCs already take p_locale, so the label is now localized
--   too -- previously "min walk" was English for every user.
--
--   Bodies are otherwise byte-identical to their current definitions; they were
--   extracted programmatically and had exactly one formatter expression each
--   substituted, asserted at build time. Signatures, volatility, security mode
--   and grants are unchanged, so no grant/RLS restatement is needed.
--
-- Sources: get_nearby_activities + _authed  <- 20260829120000_event_has_time
--          get_event_preview                <- 20260831120000_event_preview_cancelled
--          get_event_preview_authed         <- 20260817120000_event_rsvp_cover_emoji
-- ============================================================================


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
    and e.location is not null
    and e.starts_at between now() and now() + p_window
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
    am.matched as affinity_match_count,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_total_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) = 0 then null
      else concat(am.matched, '/', cardinality(coalesce(e.cohort_tags, '{}')), ' affinities match')
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
  left join lateral (
    select count(distinct tag)::int as matched
    from unnest(coalesce(e.cohort_tags, '{}')) as tag
    where exists (
      select 1
      from public.user_identity_claims c
      where c.user_id = v_caller
        and c.dismissed_at is null
        and c.disclosure = 'public'
        and public.cohort_tag_matches_claim(tag, c.concept, c.synonyms)
    )
  ) am on true
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
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
    e.starts_at, e.has_time, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url, e.cover_emoji
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status in ('open', 'cancelled');

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null then
    v_point := extensions.st_setsrid(extensions.st_makepoint(p_lng, p_lat), 4326)::extensions.geography;
    v_distance := extensions.st_distance(v_event.location, v_point)::double precision;
    v_distance_text := public.humanize_distance_text(v_distance, p_locale);
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
      and er.rsvp_status = 'going'
    order by event_count desc, u.created_at asc
    limit 20
  ) p;

  return jsonb_build_object(
    'event_id', v_event.id,
    'host_id', v_event.host_id,
    'title', v_event.title,
    'description', v_event.description,
    'starts_at', v_event.starts_at,
    'has_time', v_event.has_time,
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
    'cover_emoji', v_event.cover_emoji,
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_total,
    'affinity_match_label', case when v_total > 0 then concat(v_total, ' affinities') else null end,
    'is_authenticated', false,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'going'), 0),
    'maybe_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'maybe'), 0),
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
  v_my_rsvp text;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url, e.cover_emoji
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
    v_distance_text := public.humanize_distance_text(v_distance, p_locale);
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

  select er.status, er.rsvp_status
  into v_my_status, v_my_rsvp
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
      and er.rsvp_status = 'going'
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
    'cover_emoji', v_event.cover_emoji,
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_matched,
    'affinity_total_count', v_total,
    'affinity_match_label', case when v_total = 0 then null else concat(v_matched, '/', v_total, ' affinities match') end,
    'is_authenticated', true,
    'my_request_status', v_my_status,
    'my_rsvp_status', v_my_rsvp,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'going'), 0),
    'maybe_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'maybe'), 0),
    'participants', v_participants
  );
end;
$$;


-- ============================================================================
-- ROLLBACK
--   Re-apply the four function bodies from the source migrations listed in the
--   header. Nothing else here changes -- no signature, no grant, no table.
-- ============================================================================
