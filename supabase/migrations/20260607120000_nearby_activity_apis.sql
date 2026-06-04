-- TagAlng Phase 3c: Nearby activities + event preview + profile summary.
--
-- Two explicit variants per screen (frontend picks based on login state):
--   * Anonymous  (get_*)        -> always blurred: no names/photos, affinity = event tag count.
--   * Signed-in  (get_*_authed) -> full names/photos, personalized affinity ("5/7 match"),
--                                  shared-claim counts, caller's RSVP status. Requires a login token.
--
-- Drop any previously-shipped variants (renames / earlier auth-aware versions) for a clean apply.
drop function if exists public.get_nearby_activities(double precision, double precision, text, interval, text, int);
drop function if exists public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int);
drop function if exists public.get_event_preview(uuid, double precision, double precision, text);
drop function if exists public.get_event_preview_authed(uuid, double precision, double precision, text);
drop function if exists public.get_event_preview_anon(uuid, double precision, double precision, text);
drop function if exists public.get_profile_summary(uuid);
drop function if exists public.get_profile_summary_authed(uuid);
drop function if exists public.get_anon_profile_summary(uuid);
-- =====================================================================
-- ANONYMOUS VARIANTS (always blurred)
-- =====================================================================

-- 1a. Nearby activities for anonymous visitors (by GPS or ZIP).
create function public.get_nearby_activities(
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
  ends_at timestamptz,
  duration_minutes int,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  distance_meters double precision,
  distance_text text,
  affinity_match_count int,
  affinity_match_label text,
  participant_count int,
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
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    concat(
      greatest(1, round(extensions.st_distance(e.location, v_point) / 80)::int),
      ' min walk'
    ) as distance_text,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_match_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) > 0 then
        concat(cardinality(coalesce(e.cohort_tags, '{}')), ' affinities')
      else null
    end as affinity_match_label,
    coalesce((
      select count(*)
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
    ), 0) as participant_count,
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
-- 2a. Event preview for anonymous visitors (blurred participants).
create function public.get_event_preview(
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
    e.starts_at, e.ends_at, e.location, e.venue_name, e.cohort_tags,
    e.max_attendees, e.status, e.cover_image_url
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
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
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
-- 3a. Profile summary for anonymous visitors (blurred).
create function public.get_profile_summary(
  p_user_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_user record;
  v_public_claims jsonb;
begin
  select id, created_at
  into v_user
  from public.users
  where id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  select coalesce(jsonb_agg(sub.label order by sub.confidence desc), '[]'::jsonb)
  into v_public_claims
  from (
    select distinct c.label, c.confidence
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    order by c.confidence desc
  ) sub;

  return jsonb_build_object(
    'user_id', null,
    'nickname', null,
    'avatar_url', null,
    'is_blurred', true,
    'is_authenticated', false,
    'is_matched', false,
    'event_count', coalesce((select count(*) from public.event_requests er where er.requester_id = p_user_id and er.status in ('approved', 'attended')), 0),
    'weeks_here', floor(extract(epoch from now() - v_user.created_at) / 604800)::int,
    'affinity_match_count', jsonb_array_length(v_public_claims),
    'about_tags', v_public_claims,
    'common_interest_tags', '[]'::jsonb,
    'shared_event_count', 0
  );
end;
$$;
-- =====================================================================
-- SIGNED-IN VARIANTS (full data + personalized match; require a login token)
-- =====================================================================

-- 1b. Nearby activities for signed-in users.
create function public.get_nearby_activities_authed(
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
  ends_at timestamptz,
  duration_minutes int,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  cover_image_url text,
  distance_meters double precision,
  distance_text text,
  affinity_match_count int,
  affinity_total_count int,
  affinity_match_label text,
  participant_count int,
  my_request_status text,
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
    extensions.st_distance(e.location, v_point)::double precision as distance_meters,
    concat(
      greatest(1, round(extensions.st_distance(e.location, v_point) / 80)::int),
      ' min walk'
    ) as distance_text,
    am.matched as affinity_match_count,
    cardinality(coalesce(e.cohort_tags, '{}')) as affinity_total_count,
    case
      when cardinality(coalesce(e.cohort_tags, '{}')) = 0 then null
      else concat(am.matched, '/', cardinality(coalesce(e.cohort_tags, '{}')), ' affinities match')
    end as affinity_match_label,
    coalesce((
      select count(*)
      from public.event_requests er
      where er.event_id = e.id
        and er.status in ('approved', 'attended')
    ), 0) as participant_count,
    (
      select er.status
      from public.event_requests er
      where er.event_id = e.id
        and er.requester_id = v_caller
    ) as my_request_status,
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
        and (c.concept = tag or tag = any(c.synonyms))
    )
  ) am on true
  where e.status = 'open'
    and e.location is not null
    and e.starts_at between now() and now() + p_window
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;
-- 2b. Event preview for signed-in users.
create function public.get_event_preview_authed(
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
    e.starts_at, e.ends_at, e.location, e.venue_name, e.cohort_tags,
    e.max_attendees, e.status, e.cover_image_url
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
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
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
-- 3b. Profile summary for signed-in users.
create function public.get_profile_summary_authed(
  p_user_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_caller uuid := auth.uid();
  v_user record;
  v_public_claims jsonb;
  v_common_tags jsonb;
  v_shared_claim_count int;
  v_shared_event_count int;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select id, nickname, profile_photo_url, created_at
  into v_user
  from public.users
  where id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  select coalesce(jsonb_agg(sub.label order by sub.confidence desc), '[]'::jsonb)
  into v_public_claims
  from (
    select distinct c.label, c.confidence
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    order by c.confidence desc
  ) sub;

  select
    coalesce(jsonb_agg(t.label order by t.label), '[]'::jsonb),
    count(*)::int
  into v_common_tags, v_shared_claim_count
  from (
    select distinct c2.label
    from public.user_identity_claims c1
    join public.user_identity_claims c2 on c1.concept = c2.concept
    where c1.user_id = v_caller
      and c2.user_id = p_user_id
      and c1.dismissed_at is null
      and c2.dismissed_at is null
      and c1.disclosure = 'public'
      and c2.disclosure = 'public'
  ) t;

  select count(distinct er1.event_id)::int
  into v_shared_event_count
  from public.event_requests er1
  join public.event_requests er2 on er2.event_id = er1.event_id
  where er1.requester_id = v_caller
    and er2.requester_id = p_user_id
    and er1.status in ('approved', 'attended')
    and er2.status in ('approved', 'attended');

  return jsonb_build_object(
    'user_id', v_user.id,
    'nickname', v_user.nickname,
    'avatar_url', v_user.profile_photo_url,
    'is_blurred', false,
    'is_authenticated', true,
    'is_matched', public.are_users_matched(v_caller, p_user_id),
    'event_count', coalesce((select count(*) from public.event_requests er where er.requester_id = p_user_id and er.status in ('approved', 'attended')), 0),
    'weeks_here', floor(extract(epoch from now() - v_user.created_at) / 604800)::int,
    'affinity_match_count', v_shared_claim_count,
    'about_tags', v_public_claims,
    'common_interest_tags', v_common_tags,
    'shared_event_count', v_shared_event_count
  );
end;
$$;
-- =====================================================================
-- GRANTS: anon variants open to everyone; _authed variants signed-in only.
-- =====================================================================
grant execute on function public.get_nearby_activities(double precision, double precision, text, interval, text, int) to anon, authenticated;
grant execute on function public.get_event_preview(uuid, double precision, double precision, text) to anon, authenticated;
grant execute on function public.get_profile_summary(uuid) to anon, authenticated;
revoke all on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) from public, anon;
grant execute on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) to authenticated;
revoke all on function public.get_event_preview_authed(uuid, double precision, double precision, text) from public, anon;
grant execute on function public.get_event_preview_authed(uuid, double precision, double precision, text) to authenticated;
revoke all on function public.get_profile_summary_authed(uuid) from public, anon;
grant execute on function public.get_profile_summary_authed(uuid) to authenticated;
