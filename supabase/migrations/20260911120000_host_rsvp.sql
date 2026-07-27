-- Host RSVP (C-18 · Meet container): the host had no RSVP path at all.
--
-- The 20260817 model stores RSVP on event_requests, which are guest-only:
-- request_to_join_event rejects the host (host_cannot_request_own_event), so
-- set_event_rsvp always raised request_not_found for them — the PWA mapped
-- that to a "coming soon" toast. QA reads it as "host rsvp is broken".
--
-- Fix: host intent lives on the event itself, not on a synthetic request row
-- (a request row would leak into capacity math, the chat-membership trigger,
-- and the host_cannot_request_own_event invariant).
--   • events.host_rsvp_status ('going' | 'maybe' | 'not_going', default
--     'going') — the host is implicitly going from publish.
--   • set_event_rsvp gains a host branch: flips the column, logs a
--     'host_update' thread event (payload kind='rsvp'; the check constraint
--     from 20260529 has no rsvp-specific type and doesn't need one).
--   • Host RSVP never touches capacity, participant_count, or group-chat
--     membership — the host owns the event and stays in the thread; flipping
--     to 'not_going' is a visible intent, not a cancellation.
--   • get_event_preview_authed / get_nearby_activities_authed report the
--     host's own intent as my_rsvp_status when the caller hosts the event,
--     so the existing FE control lights up with no client change. The authed
--     preview also exposes host_rsvp_status for future guest-facing badges.
--   • Co-hosts are unchanged (still no request row → request_not_found);
--     follow-up if product wants co-host RSVP.
--
-- Rewritten-on-top-of: set_event_rsvp (20260817), get_event_preview_authed
-- (20260831), get_nearby_activities_authed (20260829).

-- ---------------------------------------------------------------------------
-- 1. Column
-- ---------------------------------------------------------------------------

alter table public.events
  add column if not exists host_rsvp_status text not null default 'going'
    check (host_rsvp_status in ('going', 'maybe', 'not_going'));

-- ---------------------------------------------------------------------------
-- 2. set_event_rsvp: host branch first, guest path verbatim from 20260817.
-- ---------------------------------------------------------------------------

create or replace function public.set_event_rsvp(
  p_event_id uuid,
  p_rsvp text
)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid uuid := auth.uid();
  v_event record;
  v_req record;
  v_open boolean;
  v_max integer;
  v_going_count integer;
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_rsvp not in ('going', 'maybe', 'not_going') then
    raise exception 'invalid_rsvp' using errcode = 'P0001';
  end if;

  -- Host branch: intent lives on the event row. No capacity check (the host
  -- never consumes a spot) and no chat-membership change (the host owns the
  -- thread). A missing event falls through to the guest path's
  -- request_not_found, preserving the pre-existing error surface.
  select e.host_id, e.status, e.host_rsvp_status
  into v_event
  from public.events e
  where e.id = p_event_id;

  if found and v_event.host_id = v_uid then
    if v_event.status <> 'open' then
      raise exception 'event_not_open' using errcode = 'P0001';
    end if;
    if v_event.host_rsvp_status = p_rsvp then
      return p_rsvp;
    end if;

    update public.events
    set host_rsvp_status = p_rsvp
    where id = p_event_id;

    insert into public.thread_events (event_id, actor_id, event_type, payload)
    values (
      p_event_id, v_uid, 'host_update',
      jsonb_build_object('kind', 'rsvp', 'rsvp_from', v_event.host_rsvp_status, 'rsvp_to', p_rsvp)
    );

    return p_rsvp;
  end if;

  select er.id, er.status, er.rsvp_status
  into v_req
  from public.event_requests er
  where er.event_id = p_event_id and er.requester_id = v_uid;

  if not found then
    raise exception 'request_not_found' using errcode = 'P0001';
  end if;
  -- A cancelled/declined row is closed — re-request instead of flipping intent.
  if v_req.status in ('cancelled', 'declined') then
    raise exception 'request_closed' using errcode = 'P0001';
  end if;

  if v_req.rsvp_status = p_rsvp then
    return p_rsvp;
  end if;

  -- Reclaiming a spot: an approved guest flipping back to 'going' must still fit.
  -- (maybe/not_going never hold a spot; pending rows are gated at approval time.)
  if p_rsvp = 'going' and v_req.status in ('approved', 'attended') then
    select e.status = 'open', e.max_attendees
    into v_open, v_max
    from public.events e
    where e.id = p_event_id;
    if not coalesce(v_open, false) then
      raise exception 'event_not_open' using errcode = 'P0001';
    end if;
    select count(*) into v_going_count
    from public.event_requests er
    where er.event_id = p_event_id
      and er.status in ('approved', 'attended')
      and er.rsvp_status = 'going'
      and er.requester_id <> v_uid;
    if v_max is not null and v_going_count >= v_max then
      raise exception 'event_full' using errcode = 'P0001';
    end if;
  end if;

  update public.event_requests
  set rsvp_status = p_rsvp
  where id = v_req.id;

  -- The status-change logger (20260529) ignores rsvp-only updates; log the flip
  -- here so the host's thread timeline shows "changed to maybe / backed out".
  insert into public.thread_events (event_id, actor_id, event_type, payload)
  values (
    p_event_id, v_uid, 'request_changed',
    jsonb_build_object('requester_id', v_uid, 'rsvp_from', v_req.rsvp_status, 'rsvp_to', p_rsvp)
  );

  return p_rsvp;
end;
$$;

revoke execute on function public.set_event_rsvp(uuid, text) from public, anon;
grant execute on function public.set_event_rsvp(uuid, text) to authenticated;

-- ---------------------------------------------------------------------------
-- 3. get_event_preview_authed (body from 20260831): host caller's
--    my_rsvp_status comes from host_rsvp_status; payload also carries
--    host_rsvp_status for everyone.
-- ---------------------------------------------------------------------------

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
    e.id, e.host_id, e.host_rsvp_status,
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

  if v_caller = v_event.host_id then
    v_my_status := null;
    v_my_rsvp := v_event.host_rsvp_status;
  else
    select er.status, er.rsvp_status
    into v_my_status, v_my_rsvp
    from public.event_requests er
    where er.event_id = p_event_id
      and er.requester_id = v_caller;
  end if;

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
    'host_rsvp_status', v_event.host_rsvp_status,
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
-- 4. get_nearby_activities_authed (body from 20260829): my_rsvp_status is the
--    host's own intent on events they host. Same return type, so replace in
--    place — no drop/re-grant dance needed (grants restated for convention).
-- ---------------------------------------------------------------------------

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
    case
      when e.host_id = v_caller then e.host_rsvp_status
      else (
        select er.rsvp_status
        from public.event_requests er
        where er.event_id = e.id
          and er.requester_id = v_caller
      )
    end as my_rsvp_status,
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

revoke all on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) from public, anon;
grant execute on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) to authenticated;
