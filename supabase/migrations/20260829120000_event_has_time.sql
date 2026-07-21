-- events.has_time: whether the host actually gave a clock time (#56).
--
-- The host flow builds starts_at as "<date>T<time or 00:00>:00" while the time is
-- still unanswered, and the extractor LLM must emit a full ISO timestamp even when
-- the host only said a date — so date-only events land at midnight and every card
-- renders "12 AM" as if it were fact. The truthful contract is an explicit flag:
-- starts_at stays date-anchored (sorting/windows keep working), has_time says
-- whether the clock component is real. The worker sets it from the when-resolver
-- (true only when the TIME came from the host's words/chips); card readers return
-- it so the FE renders date-only when it is false.
--
-- Backfill: existing rows whose starts_at is exactly midnight EVENT-LOCAL are
-- marked has_time = false. This is the isMidnightLocal heuristic applied ONCE to
-- legacy data (neighborhood meets at 12:00 AM sharp do not exist in practice);
-- from this migration on the flag is written truthfully at create time and the
-- heuristic is never consulted again.
--
-- Rewritten-on-top-of: create_event + previews + feed (20260817), similar meets
-- (20260811).

-- ---------------------------------------------------------------------------
-- 1. Column + one-time legacy backfill
-- ---------------------------------------------------------------------------

alter table public.events
  add column if not exists has_time boolean not null default true;

comment on column public.events.has_time is
  'Whether the host gave a real clock time. false = starts_at is date-only (time component is a midnight placeholder); render the date without a time.';

update public.events
set has_time = false
where (starts_at at time zone 'America/New_York')::time = time '00:00';

-- ---------------------------------------------------------------------------
-- 2. create_event: persist has_time (body from 20260817 + one field)
-- ---------------------------------------------------------------------------

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
    has_time,
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
    cover_image_url,
    cover_emoji
  )
  values (
    auth.uid(),
    v_cohost,
    coalesce(p_fields->>'cluster_id', 'lake-nona'),
    p_fields->>'block_id',
    p_fields->>'title',
    p_fields->>'description',
    coalesce((p_fields->>'starts_at')::timestamptz, now() + interval '7 days'),
    coalesce((p_fields->>'has_time')::boolean, true),
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
    p_fields->>'cover_image_url',
    nullif(left(btrim(coalesce(p_fields->>'cover_emoji', '')), 16), '')
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

-- ---------------------------------------------------------------------------
-- 3. Event previews (bodies from 20260817): + has_time. jsonb return — no
--    signature change, create-or-replace suffices.
-- ---------------------------------------------------------------------------

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
    e.starts_at, e.has_time, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
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

-- ---------------------------------------------------------------------------
-- 4. Feed (bodies from 20260817): + has_time. Adding an OUT column changes the
--    return type, so drop + recreate + re-grant.
-- ---------------------------------------------------------------------------

drop function if exists public.get_nearby_activities(double precision, double precision, text, interval, text, int);

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

drop function if exists public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int);

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

grant execute on function public.get_nearby_activities(double precision, double precision, text, interval, text, int) to anon, authenticated;
revoke all on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) from public, anon;
grant execute on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) to authenticated;

-- ---------------------------------------------------------------------------
-- 5. Similar meets (body from 20260811): + has_time. Return-type change, so
--    drop + recreate + re-grant.
-- ---------------------------------------------------------------------------

drop function if exists public.get_similar_events(uuid, integer, text);

create function public.get_similar_events(
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
  has_time boolean,
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
    e.has_time,
    e.location,
    e.venue_name,
    e.cohort_tags,
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

revoke execute on function public.get_similar_events(uuid, integer, text) from public;
grant execute on function public.get_similar_events(uuid, integer, text) to anon, authenticated;
