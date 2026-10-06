-- ============================================================================
-- §29 PRIVATE MEETS (issues #95, LANA-48)
--
-- The hosting carousel's Privacy card promises "it won't show anywhere in the app;
-- only neighbours you share the invite with can see it." Until now nothing stored
-- or enforced that. This migration:
--
--   (b) adds events.is_private (not null, default false), written by create_event
--       from p_fields.is_private and echoed by get_event_preview / _authed.
--   (c) keeps a private meet out of every discovery read:
--         - RLS: the world-readable "open rows" policy now excludes private rows.
--           Host / co-host / approved-attendee policies are untouched, so those
--           people still read the row directly.
--         - SECURITY DEFINER readers (RLS does not apply to them) get an explicit
--           `not e.is_private`: get_nearby_activities, get_nearby_activities_authed,
--           get_activities_near_point, search_events_semantic, get_similar_events,
--           get_cluster_events, get_lana_block_context_for_user and
--           get_peer_profile.upcoming_shared_events.
--
-- Deliberately NOT changed (own lists / invite path):
--   get_event_preview(_authed) -- the invite link; only the echo is added.
--   get_my_contributions, get_my_group_threads, get_my_event_requests,
--   get_my_cohost_invites, get_my_profile_dashboard -- the host's / joiner's own lists.
--   roll_recurring_events / skip_event_occurrence -- a recurring meet is ONE rolling
--   row, so is_private rides along on every occurrence with no extra work.
--
-- is_private is NOT allow_attendee_share: a host may want a private meet whose
-- attendees can still forward the invite. The two columns are independent.
--
-- Every redefined body below was extracted programmatically from its latest
-- definition on origin/main and had only the §29 line(s) substituted, asserted at
-- build time. Signatures, return types, volatility, security mode and grants are
-- unchanged (create or replace keeps the existing ACL), so no grant is restated.
-- ============================================================================

alter table public.events
  add column if not exists is_private boolean not null default false;

comment on column public.events.is_private is
  'Invite-only meet (§29). Excluded from every discovery read; reachable by its '
  '/meet/{id} link (get_event_preview) and in the host''s and attendees'' own lists. '
  'Independent of allow_attendee_share.';

drop policy if exists "events_select_open_anyone" on public.events;
create policy "events_select_open_anyone"
  on public.events for select
  using (status = 'open' and not is_private);

-- ---------------------------------------------------------------------------
-- create_event -- body verbatim from 20261009120000_recurring_events.sql; only the §29 line(s) differ.
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
  v_title text;
  v_starts timestamptz;
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

  -- Resolved once so the insert and the conflict lookup agree exactly.
  v_title := p_fields->>'title';
  v_starts := coalesce((p_fields->>'starts_at')::timestamptz, now() + interval '7 days');

  begin
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
      cover_emoji,
      recurrence,
      recurrence_until,
      is_private
    )
    values (
      auth.uid(),
      v_cohost,
      coalesce(p_fields->>'cluster_id', 'lake-nona'),
      p_fields->>'block_id',
      v_title,
      p_fields->>'description',
      v_starts,
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
      nullif(left(btrim(coalesce(p_fields->>'cover_emoji', '')), 16), ''),
      nullif(btrim(coalesce(p_fields->>'recurrence', '')), ''),
      nullif(btrim(coalesce(p_fields->>'recurrence_until', '')), '')::date,
      coalesce((p_fields->>'is_private')::boolean, false)
    )
    returning id into new_id;
  exception
    when unique_violation then
      -- The first tap already created this exact event. Hand back ITS id and
      -- exit: re-running the cohost update below would be a no-op at best, and
      -- the caller's contract is "you get the event id", not "you inserted".
      select e.id into new_id
      from public.events e
      where e.host_id = auth.uid()
        and lower(btrim(e.title)) = lower(btrim(v_title))
        and e.starts_at = v_starts
        and e.status = 'open'
      limit 1;

      if new_id is null then
        -- A different unique index fired, or the row vanished between the
        -- insert and this read. Do not invent a success.
        raise;
      end if;
      return new_id;
  end;

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
-- get_event_preview -- body verbatim from 20270110120000_meet_fit_and_preview_fixes.sql; only the §29 line(s) differ.
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
  begin
    perform public.roll_recurring_events(p_event_id);
  exception when others then null;
  end;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.has_time, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url, e.cover_emoji,
    e.recurrence, e.recurrence_until, e.circle_place_ref, e.is_private
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
    'recurrence', v_event.recurrence,
    'recurrence_until', v_event.recurrence_until,
    'is_private', v_event.is_private,
    'community', public.event_community(v_event.circle_place_ref, v_event.host_id),
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_total,
    'affinity_match_label', case when v_total > 0 then concat(v_total, ' affinities') else null end,
    -- A signed-out viewer has no claims to intersect: no threads, no score — never the
    -- event's own tags dressed up as a match (20260901120000).
    'affinity_matched_tags', '[]'::jsonb,
    'fit_score', null,
    'is_authenticated', false,
    'participant_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'going'), 0),
    'maybe_count', coalesce((select count(*) from public.event_requests er where er.event_id = p_event_id and er.status in ('approved', 'attended') and er.rsvp_status = 'maybe'), 0),
    'participants', v_participants
  );
end;
$$;

-- ---------------------------------------------------------------------------
-- get_event_preview_authed -- body verbatim from 20270110120000_meet_fit_and_preview_fixes.sql; only the §29 line(s) differ.
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
  v_matched_tags text[];
  v_fit numeric;
  v_my_status text;
  v_my_rsvp text;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  begin
    perform public.roll_recurring_events(p_event_id);
  exception when others then null;
  end;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.has_time, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url, e.cover_emoji,
    e.recurrence, e.recurrence_until, e.circle_place_ref, e.is_private
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

  -- One intersection for the count, the chips and the score — the same helper the map's
  -- get_nearby_activities_authed and /lana/circles/profile read, so the card, the marker
  -- and the community-scoped marker can never disagree about the same meet.
  select f.matched_count, f.matched_tags, f.fit_score
  into v_matched, v_matched_tags, v_fit
  from public.event_viewer_fit(v_event.cohort_tags, v_caller) f;

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
    'recurrence', v_event.recurrence,
    'recurrence_until', v_event.recurrence_until,
    'is_private', v_event.is_private,
    'community', public.event_community(v_event.circle_place_ref, v_event.host_id),
    'distance_meters', v_distance,
    'distance_text', v_distance_text,
    'affinity_match_count', v_matched,
    'affinity_total_count', v_total,
    'affinity_match_label', case when v_total = 0 then null else concat(v_matched, '/', v_total, ' affinities match') end,
    -- The intersection itself, label-mapped like cohort_tags, in the event's tag order.
    'affinity_matched_tags', to_jsonb(public.cohort_tag_labels(coalesce(v_matched_tags, '{}'))),
    'fit_score', v_fit,
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
-- get_nearby_activities -- body verbatim from 20260926120000_honest_distance_labels.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
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
    and e.starts_at between now() and now() + p_window
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

-- ---------------------------------------------------------------------------
-- get_nearby_activities_authed -- body verbatim from 20270110120000_meet_fit_and_preview_fixes.sql; only the §29 line(s) differ.
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
    and e.starts_at between now() and now() + p_window
  order by distance_meters asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$$;

-- ---------------------------------------------------------------------------
-- get_activities_near_point -- body verbatim from 20260920120000_geolocation_aware_search.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
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
    and e.starts_at between now() and now() + p_window
    -- THE CAP. st_dwithin is index-assisted (GiST on geography).
    and extensions.st_dwithin(e.location, v_point, v_radius)
  order by extensions.st_distance(e.location, v_point) asc, e.starts_at asc
  limit greatest(1, least(coalesce(p_limit, 20), 50));
end;
$function$;

-- ---------------------------------------------------------------------------
-- search_events_semantic -- body verbatim from 20261205120000_search_events_exclude_host.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- get_similar_events -- body verbatim from 20261015120000_event_community_surface.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
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
  has_time boolean,
  location extensions.geography,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text,
  shared_tags integer,
  meters_away double precision,
  community jsonb
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
    st_distance(e.location, src.location) as meters_away,
    public.event_community(e.circle_place_ref, e.host_id) as community
  from public.events e
  cross join src
  where e.cluster_id = src.cluster_id
    and e.id <> p_event_id
    and e.status = 'open'
    and not e.is_private  -- §29: a private meet travels by invite link only
    and e.starts_at > now()
  order by
    shared_tags desc,
    meters_away asc nulls last,
    abs(extract(epoch from (e.starts_at - src.starts_at))) asc
  limit greatest(1, least(coalesce(p_limit, 3), 10));
$$;

-- ---------------------------------------------------------------------------
-- get_cluster_events -- body verbatim from 20260529000000_phase3_events_rtj_nudges.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
create or replace function public.get_cluster_events(
  p_cluster_id text,
  p_window interval default '14 days',
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
  status text
)
language sql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.ends_at,
    e.location,
    e.venue_name,
    e.cohort_tags,
    e.max_attendees,
    e.status
  from public.events e
  where e.cluster_id = p_cluster_id
    and e.status = 'open'
    and not e.is_private  -- §29: a private meet travels by invite link only
    and e.starts_at between now() and now() + p_window
  order by e.starts_at asc;
$$;

-- ---------------------------------------------------------------------------
-- get_lana_block_context_for_user -- body verbatim from 20260608120000_lana_block_context_rpc.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
create or replace function public.get_lana_block_context_for_user(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_block_id text;
  v_cluster_id text;
  v_block_name text;
  v_member_count int;
  v_events jsonb;
  v_neighbors jsonb;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  select u.home_block_id, b.cluster_id, b.display_name
  into v_block_id, v_cluster_id, v_block_name
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if v_block_id is null then
    return jsonb_build_object(
      'has_block', false,
      'block_id', null,
      'cluster_id', null,
      'block_display_name', null,
      'member_count', 0,
      'open_events_count', 0,
      'upcoming_events', '[]'::jsonb,
      'neighbor_hints', '[]'::jsonb
    );
  end if;

  select count(*)::int
  into v_member_count
  from public.users u
  where u.home_block_id = v_block_id
    and u.id <> p_user_id;

  select coalesce(jsonb_agg(jsonb_build_object(
      'title', e.title,
      'starts_at', e.starts_at,
      'venue_name', e.venue_name,
      'cohort_tags', e.cohort_tags
    ) order by e.starts_at asc), '[]'::jsonb)
  into v_events
  from (
    select e.title, e.starts_at, e.venue_name, e.cohort_tags
    from public.events e
    where e.status = 'open'
      and not e.is_private  -- §29
      and e.cluster_id = coalesce(v_cluster_id, 'lake-nona')
      and e.starts_at between now() and now() + interval '14 days'
    order by e.starts_at asc
    limit 5
  ) e;

  select coalesce(jsonb_agg(jsonb_build_object(
      'nickname', n.nickname,
      'shared_public_claim_count', n.shared_count,
      'public_labels', n.public_labels
    ) order by n.shared_count desc, n.nickname asc nulls last), '[]'::jsonb)
  into v_neighbors
  from (
    select
      u.nickname,
      coalesce((
        select count(*)::int
        from public.user_identity_claims c1
        join public.user_identity_claims c2 on c1.concept = c2.concept
        where c1.user_id = p_user_id
          and c2.user_id = u.id
          and c1.dismissed_at is null
          and c2.dismissed_at is null
          and c1.disclosure = 'public'
          and c2.disclosure = 'public'
      ), 0) as shared_count,
      coalesce((
        select jsonb_agg(sub.label order by sub.confidence desc)
        from (
          select distinct c.label, c.confidence
          from public.user_identity_claims c
          where c.user_id = u.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc
          limit 3
        ) sub
      ), '[]'::jsonb) as public_labels
    from public.users u
    where u.home_block_id = v_block_id
      and u.id <> p_user_id
    order by shared_count desc, u.created_at asc
    limit 5
  ) n;

  return jsonb_build_object(
    'has_block', true,
    'block_id', v_block_id,
    'cluster_id', v_cluster_id,
    'block_display_name', v_block_name,
    'member_count', v_member_count,
    'open_events_count', jsonb_array_length(v_events),
    'upcoming_events', v_events,
    'neighbor_hints', v_neighbors
  );
end;
$$;

-- ---------------------------------------------------------------------------
-- get_peer_profile -- body verbatim from 20270110120000_meet_fit_and_preview_fixes.sql; only the §29 line(s) differ.
-- ---------------------------------------------------------------------------
create or replace function public.get_peer_profile(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  caller uuid := auth.uid();
  peer record;
  is_matched boolean;
  location_label text;
  location_precision text;
  result jsonb;
begin
  -- Fetch peer profile
  select u.id, u.nickname, u.profile_photo_url, u.home_block_id, u.home_location_visibility,
         u.public_portrait,
         b.display_name, b.cluster_id
  into peer
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  -- Anonymous visitor: blurred profile (no sensitive data)
  if caller is null then
    return jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'avatar_url', null,
      'is_matched', false,
      'portrait', null,
      'public_claims', '[]'::jsonb,
      'mutual_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'location_label', null,
      'location_precision', null,
      'block_name', null,
      'upcoming_shared_events', '[]'::jsonb
    );
  end if;

  -- Choose label based on peer preference
  location_label := case peer.home_location_visibility
    when 'block' then peer.display_name
    when 'cluster' then peer.cluster_id
  end;
  location_precision := peer.home_location_visibility::text;

  -- Check if caller and peer are matched (same event or shared public claims)
  is_matched := public.are_users_matched(caller, p_user_id);

  -- Build authenticated response
  result := jsonb_build_object(
    'user_id', peer.id,
    'nickname', peer.nickname,
    'avatar_url', peer.profile_photo_url,
    'is_matched', is_matched,
    -- Written from the public claims listed directly below, and from nothing else.
    'portrait', peer.public_portrait,
    -- Always show public claims
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence,
        -- NEW: what kind of thread it is, and when it was learned.
        'bucket', c.bucket,
        'subject_kind', c.subject_kind,
        'created_at', c.created_at
      ) order by c.confidence desc)
      from public.user_identity_claims c
      where c.user_id = peer.id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ), '[]'::jsonb),
    -- Show mutual claims only if matched (silent omission otherwise)
    'mutual_claims', case
      when is_matched then
        coalesce((
          select jsonb_agg(jsonb_build_object(
            'concept', c.concept,
            'label', c.label,
            'tone', c.tone,
            'confidence', c.confidence,
            'bucket', c.bucket,
        'subject_kind', c.subject_kind,
            'created_at', c.created_at
          ) order by c.confidence desc)
          from public.user_identity_claims c
          where c.user_id = peer.id
            and c.dismissed_at is null
            and c.disclosure = 'mutual'
        ), '[]'::jsonb)
      else '[]'::jsonb
    end,
    'shared_claim_count', (
      select count(*)::int
      from public.user_identity_claims c1
      join public.user_identity_claims c2
        on c1.concept = c2.concept
       and c1.subject_kind = c2.subject_kind
      where c1.user_id = caller
        and c2.user_id = peer.id
        and c1.dismissed_at is null
        and c2.dismissed_at is null
        and c1.disclosure = 'public'
        and c2.disclosure = 'public'
    ),
    'location_label', location_label,
    'location_precision', location_precision,
    'block_name', case when peer.home_location_visibility = 'block' then peer.display_name else null end,
    -- home_block_id is HIDDEN from peer views (only owner sees it via get_my_profile)
    'upcoming_shared_events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', e.id,
        'title', e.title,
        'starts_at', e.starts_at,
        -- The community the meet is FOR, same object as get_event_preview's; null on a
        -- plain neighbourhood meet. Both people are going, so the place is no news.
        'community', public.event_community(e.circle_place_ref, e.host_id)
      ) order by e.starts_at asc)
      from public.events e
      where e.status = 'open'
        and not e.is_private  -- §29
        and e.starts_at > now()
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = caller
            and r.status in ('approved', 'attended')
        )
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = peer.id
            and r.status in ('approved', 'attended')
        )
    ), '[]'::jsonb)
  );

  return result;
end;
$$;

-- ============================================================================
-- ROLLBACK
--   Re-apply each function body from the source migration named above it, restore
--   the policy as `using (status = 'open')`, then
--   `alter table public.events drop column is_private;`.
-- ============================================================================
