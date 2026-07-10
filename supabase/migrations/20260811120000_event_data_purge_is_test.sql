-- Data hygiene: purge the junk prod events QA found in the 32827 feed (2026-07-08 pass:
-- dirty rows appeared in ~20/24 result lists), then fence the feed so it can't happen
-- again: events.is_test (QA-created rows never reach feeds/previews) + a dedupe pass +
-- a partial unique index so the same meet can't be posted twice.
--
-- ── SELECT-FIRST: the exact junk rows this migration targets ─────────────────────────
-- Run these before applying to eyeball what will be removed. Archetypes from the QA
-- pass (production, 2026-07-08):
--
--   (a) "Playdate at the park" with a New York, NY venue (Google place_id
--       ChIJOwg_06VPwokRYv534QaPC8g — the "New+York,+NY" Maps query), starts_at
--       2026-07-14T00:30Z. A default title chip published against an unresolved venue
--       that geocoded to Manhattan.
--       select id, title, venue_name, place_id, starts_at from public.events
--       where lower(btrim(title)) = 'playdate at the park'
--         and (place_id = 'ChIJOwg_06VPwokRYv534QaPC8g'
--              or venue_name ilike '%new york%'
--              or starts_at = timestamptz '2026-07-14 00:30:00+00');
--
--   (b) "Brazilian Coffee Morning" listed 3x with venue-string variants
--       ("Foxtail Coffee Co." / "at Foxtail" / "Foxtail Coffee Co") — same meet posted
--       repeatedly. Removed by the generic dedupe pass below (keep earliest per
--       host + lower(title) + day), not by a bespoke delete.
--       select id, host_id, title, venue_name, starts_at, created_at from public.events
--       where lower(title) like 'brazilian coffee morning%' order by created_at;
--
--   (c) "Morning meetup" at venue "My place" (Sat Jul 11) — a default title chip with a
--       placeholder venue that can't be resolved to a pin.
--       select id, title, venue_name, starts_at from public.events
--       where lower(btrim(title)) = 'morning meetup'
--         and lower(btrim(coalesce(venue_name, ''))) = 'my place';
--
--   (d) "6th Birthday Moms & Kids Party" at 2026-08-12T04:00Z — a midnight-local
--       starts_at (timezone artifact; no real party starts at 00:00).
--       select id, title, starts_at from public.events
--       where title = '6th Birthday Moms & Kids Party'
--         and starts_at = timestamptz '2026-08-12 04:00:00+00';
--
--   (e) QA-created "Pre-K Playground Meetup" (Fri 2026-07-10 9:30 AM, Laureate Park
--       Zipline Playground), created 2026-07-08 by t+lanaqa1@phygtl.com. Handled by the
--       host-email fence (any '+…qa…' plus-tag account), not a title match — so every
--       past and future QA row is caught, not just this one.
--       select e.id, e.title, e.starts_at, u.email from public.events e
--       join public.users u on u.id = e.host_id
--       where u.email ~* '^[^@+]*\+[^@]*qa[^@]*@';
--
-- All FKs onto events are `on delete cascade` (event_requests, thread_events, chat,
-- irl_promotion) or `on delete set null` (tpr metrics), so hard deletes are safe.
-- Every statement below is idempotent: re-running on a clean DB is a no-op.

-- ── 1. is_test fence column ──────────────────────────────────────────────────────────
alter table public.events
  add column if not exists is_test boolean not null default false;

comment on column public.events.is_test is
  'True for QA/test-created events. Fenced out of every feed/browse/preview surface '
  '(get_nearby_activities[_authed], get_event_preview[_authed], worker block previews) '
  'and exempt from the live-events unique index. Set by create_event when the worker '
  'flags a QA account (email plus-tag containing "qa", e.g. t+lanaqa1@phygtl.com).';

create index if not exists events_is_test_idx on public.events (is_test) where is_test;

-- ── 2. Quarantine QA-account events (archetype e) ────────────────────────────────────
-- Mark rather than delete: QA may still need to inspect their own rows; the fence keeps
-- them out of every member-facing list.
update public.events e
set is_test = true
from public.users u
where u.id = e.host_id
  and u.email ~* '^[^@+]*\+[^@]*qa[^@]*@'
  and not e.is_test;

-- ── 3. Guarded deletes of the junk archetypes ────────────────────────────────────────
-- (a) The New-York "Playdate at the park". Title alone is NOT enough ("Playdate at the
-- park" is a suggestion chip real hosts use) — require the NY marker or the exact
-- QA-documented instant.
delete from public.events
where lower(btrim(title)) = 'playdate at the park'
  and (
    place_id = 'ChIJOwg_06VPwokRYv534QaPC8g'
    or venue_name ilike '%new york%'
    or starts_at = timestamptz '2026-07-14 00:30:00+00'
  );

-- (c) "Morning meetup" at the placeholder venue "My place".
delete from public.events
where lower(btrim(title)) = 'morning meetup'
  and lower(btrim(coalesce(venue_name, ''))) = 'my place';

-- (d) The midnight-local birthday party. Exact title + exact QA-documented instant.
delete from public.events
where title = '6th Birthday Moms & Kids Party'
  and starts_at = timestamptz '2026-08-12 04:00:00+00';

-- ── 4. Dedupe (archetype b + any other repeats) ──────────────────────────────────────
-- Keep the EARLIEST row per (creator, lower(title), day of starts_at); delete the later
-- duplicates. Day bucket in UTC — the venue-string variants QA saw were same-day repeats.
with ranked as (
  select
    id,
    row_number() over (
      partition by host_id, lower(title), ((starts_at at time zone 'utc')::date)
      order by created_at asc, id asc
    ) as rn
  from public.events
)
delete from public.events e
using ranked r
where e.id = r.id
  and r.rn > 1;

-- ── 5. Dedupe guard: live events are unique per (creator, lower(title), starts_at) ───
-- Partial (`where not is_test`) so QA can re-post the same fixture freely. create_event
-- (below) maps the violation to 'duplicate_event' for a friendly in-chat reply.
create unique index if not exists events_host_title_starts_live_uniq
  on public.events (host_id, lower(title), starts_at)
  where not is_test;

-- ── 6. RLS: the anonymous open-events read must not see test rows either ─────────────
-- (Feeds go through the security-definer RPCs below, and the worker uses service_role —
-- this closes the remaining direct-PostgREST read. Hosts still see their own test rows
-- via events_select_host_all_status.)
drop policy if exists "events_select_open_anyone" on public.events;
create policy "events_select_open_anyone"
  on public.events for select
  using (status = 'open' and not is_test);

-- ── 7. Feed RPCs: fence is_test out ──────────────────────────────────────────────────
-- Applied migrations are immutable, so (repo convention, see 20260612000000) each
-- function is create-or-replaced with its authoritative latest body + the one-line
-- `and not e.is_test` fence:
--   * get_nearby_activities         — body from 20260612000000
--   * get_nearby_activities_authed  — body from 20260612000000
--   * get_event_preview[_authed]    — bodies from 20260804120000
--   * create_event                  — body from 20260804120000 (+ is_test insert
--                                     + unique_violation → 'duplicate_event')

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
      select count(*)::int
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
    and not e.is_test
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
      select count(*)::int
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
        and public.cohort_tag_matches_claim(tag, c.concept, c.synonyms)
    )
  ) am on true
  where e.status = 'open'
    and not e.is_test
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
    e.starts_at, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status = 'open'
    and not e.is_test;

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
    'venue_address', v_event.venue_address,
    'place_id', v_event.place_id,
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
    'bring_items', coalesce(v_event.bring_items, '{}'),
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
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select
    e.id, e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at, e.ends_at, e.location, e.venue_name, e.venue_address, e.place_id, e.cohort_tags,
    e.max_attendees, e.bring_items, e.status, e.cover_image_url
  into v_event
  from public.events e
  where e.id = p_event_id
    and e.status = 'open'
    and not e.is_test;

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
    'venue_address', v_event.venue_address,
    'place_id', v_event.place_id,
    'cohort_tags', v_event.cohort_tags,
    'max_attendees', v_event.max_attendees,
    'bring_items', coalesce(v_event.bring_items, '{}'),
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

-- ── 8. create_event: persist is_test + friendly duplicate conflict ───────────────────
-- Body from 20260804120000 plus: (1) inserts is_test from p_fields (the worker sets it
-- for QA accounts; a caller can only ever HIDE their own event with it, never expose),
-- (2) maps the unique-index violation to 'duplicate_event' so the worker can reply
-- "you already have that meet — want to edit it instead?" instead of a 502.
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

  begin
    insert into public.events (
      host_id,
      cohost_id,
      cluster_id,
      block_id,
      title,
      description,
      starts_at,
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
      is_test
    )
    values (
      auth.uid(),
      v_cohost,
      coalesce(p_fields->>'cluster_id', 'lake-nona'),
      p_fields->>'block_id',
      p_fields->>'title',
      p_fields->>'description',
      coalesce((p_fields->>'starts_at')::timestamptz, now() + interval '7 days'),
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
      coalesce((p_fields->>'is_test')::boolean, false)
    )
    returning id into new_id;
  exception
    when unique_violation then
      -- events_host_title_starts_live_uniq: same host, same title, same instant.
      raise exception 'duplicate_event' using errcode = 'P0001';
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

-- Re-assert grants (create-or-replace preserves them, but make intent explicit).
grant execute on function public.get_nearby_activities(double precision, double precision, text, interval, text, int) to anon, authenticated;
revoke all on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) from public, anon;
grant execute on function public.get_nearby_activities_authed(double precision, double precision, text, interval, text, int) to authenticated;
grant execute on function public.get_event_preview(uuid, double precision, double precision, text) to anon, authenticated;
revoke all on function public.get_event_preview_authed(uuid, double precision, double precision, text) from public, anon;
grant execute on function public.get_event_preview_authed(uuid, double precision, double precision, text) to authenticated;
grant execute on function public.create_event(jsonb) to authenticated;
