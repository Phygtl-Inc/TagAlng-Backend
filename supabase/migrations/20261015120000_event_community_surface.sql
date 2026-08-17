-- ── the community a meet was created for, on every surface that shows the meet ──
-- 20261012120000 added events.circle_place_ref and the host-setup picker that writes it,
-- but nothing ever read it back: the invite link, the meet container, the "similar meets"
-- sheet and the co-host invite all showed a meet with no sign of the community it was
-- created for. This migration is that read path.
--
-- One helper, public.event_community(place_ref, host_id) -> jsonb, so every RPC returns
-- the SAME shape and the FE renders one component. Null when the meet is a plain
-- neighborhood meet, which stays the default.
--
-- Anon viewers see it too: a community IS a place, and the invite card already shows the
-- venue name to signed-out viewers. What anon never gets is the roster.

-- ---------------------------------------------------------------------------
-- event_community — the label, resolved once, in one place
-- ---------------------------------------------------------------------------
create or replace function public.event_community(
  p_place_ref uuid,
  p_host_id uuid default null
)
returns jsonb
language sql
security definer
stable
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'place_ref', p.id,
    'name', p.name,
    -- The glyph is per-membership (20261008120000), not per-place: prefer the host's own
    -- (they picked this community for this meet), else any confirmed member's, else the
    -- FE's circle_type map.
    'emoji', coalesce(host.emoji, member.emoji),
    'circle_type', coalesce(host.circle_type, member.circle_type),
    -- The HOST's own words for it ("Zumba") — never another member's, which would put a
    -- stranger's phrasing on the host's meet.
    'detail', nullif(btrim(coalesce(host.detail, '')), '')
  )
  from public.places p
  left join lateral (
    select a.emoji, a.circle_type, a.detail
    from public.circle_affiliations a
    where a.place_ref = p.id
      and a.user_id = p_host_id
      and a.dismissed_at is null
    limit 1
  ) host on true
  left join lateral (
    select a.emoji, a.circle_type
    from public.circle_affiliations a
    where a.place_ref = p.id
      and a.dismissed_at is null
      and a.status = 'confirmed'
    order by (a.emoji is null), a.created_at
    limit 1
  ) member on true
  where p.id = p_place_ref;
$$;

comment on function public.event_community(uuid, uuid) is
  'The community a meet was created for, as {place_ref,name,emoji,circle_type,detail} — '
  'or null for a plain neighborhood meet. One shape for every event-reading RPC.';

revoke all on function public.event_community(uuid, uuid) from public, anon;
grant execute on function public.event_community(uuid, uuid) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- get_event_preview / _authed — bodies verbatim from 20261009120000 apart from
-- circle_place_ref in the select and the new 'community' key.
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
    e.recurrence, e.recurrence_until, e.circle_place_ref
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
    'community', public.event_community(v_event.circle_place_ref, v_event.host_id),
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
    e.recurrence, e.recurrence_until, e.circle_place_ref
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
    'community', public.event_community(v_event.circle_place_ref, v_event.host_id),
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

grant execute on function public.get_event_preview(uuid, double precision, double precision, text) to anon, authenticated;
revoke all on function public.get_event_preview_authed(uuid, double precision, double precision, text) from public, anon;
grant execute on function public.get_event_preview_authed(uuid, double precision, double precision, text) to authenticated;

-- ---------------------------------------------------------------------------
-- get_similar_events — the "similar meets nearby" rows. Return type changes, so
-- drop + recreate + re-grant (same as 20260829120000 had to).
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
    and e.starts_at > now()
  order by
    shared_tags desc,
    meters_away asc nulls last,
    abs(extract(epoch from (e.starts_at - src.starts_at))) asc
  limit greatest(1, least(coalesce(p_limit, 3), 10));
$$;

revoke execute on function public.get_similar_events(uuid, integer, text) from public;
grant execute on function public.get_similar_events(uuid, integer, text) to anon, authenticated;

-- ---------------------------------------------------------------------------
-- get_my_cohost_invites — the co-host invite card shows the meet, so it shows the
-- community too. Body verbatim from 20260802120000 plus the one column.
-- ---------------------------------------------------------------------------
drop function if exists public.get_my_cohost_invites(text);

create function public.get_my_cohost_invites(p_direction text default 'all')
returns table (
  invite_id      uuid,
  direction      text,
  event_id       uuid,
  event_title    text,
  starts_at      timestamptz,
  venue_name     text,
  venue_address  text,
  host_id        uuid,
  host_name      text,
  host_avatar    text,
  candidate_id   uuid,
  candidate_name text,
  candidate_avatar text,
  overlap_reason text,
  status         text,
  thread_id      uuid,
  created_at     timestamptz,
  responded_at   timestamptz,
  community      jsonb
)
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  select
    i.id,
    case when i.host_id = auth.uid() then 'sent' else 'received' end,
    i.event_id,
    e.title,
    e.starts_at,
    e.venue_name,
    e.venue_address,
    i.host_id,
    coalesce(nullif(trim(hu.nickname), ''), 'A neighbor'),
    hu.profile_photo_url,
    i.candidate_id,
    coalesce(nullif(trim(cu.nickname), ''), 'A neighbor'),
    cu.profile_photo_url,
    i.overlap_reason,
    i.status,
    i.thread_id,
    i.created_at,
    i.responded_at,
    public.event_community(e.circle_place_ref, e.host_id)
  from public.event_cohost_invites i
  left join public.events e on e.id = i.event_id
  left join public.users hu on hu.id = i.host_id
  left join public.users cu on cu.id = i.candidate_id
  where (
    (p_direction in ('all', 'received') and i.candidate_id = auth.uid())
    or (p_direction in ('all', 'sent') and i.host_id = auth.uid())
  )
  order by (i.status = 'proposed') desc, i.created_at desc;
$$;

revoke all on function public.get_my_cohost_invites(text) from public, anon;
grant execute on function public.get_my_cohost_invites(text) to authenticated;
