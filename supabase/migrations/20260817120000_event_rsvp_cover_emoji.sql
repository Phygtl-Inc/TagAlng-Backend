-- RSVP (going / maybe / not_going) + emoji cover for events.
--
-- RSVP model: the guest's intent lives BESIDE the host's approval, not instead of it.
--   • event_requests.rsvp_status ('going' | 'maybe' | 'not_going', default 'going') is
--     guest-owned; event_requests.status (pending/approved/…) stays host-owned. A row
--     reads like "approved + maybe" or "pending + going".
--   • Joining picks the intent: request_to_join_event gains p_rsvp ('going'|'maybe').
--     'not_going' is not a way to join — that's just "don't request".
--   • set_event_rsvp flips the caller's intent afterwards. Flipping to 'not_going'
--     frees the spot and leaves the group chat but KEEPS the row, so flipping back to
--     'going' needs no re-request/re-approval (only a capacity re-check).
--   • Only approved/attended + 'going' consumes max_attendees and counts as the
--     headline participant_count; 'maybe' is a separate soft count (maybe_count).
--   • Group-chat membership (20260621 trigger) now keys on BOTH: member iff
--     status in (approved, attended) AND rsvp_status <> 'not_going'.
--
-- Emoji cover: events.cover_emoji — an AI-picked emoji the FE renders as the card
-- cover when cover_image_url is null (Lana's publish flow authors it alongside the
-- title). create_event persists it; every card-shaped read RPC returns it.
--
-- Rewritten-on-top-of: create_event + previews (20260804/20260816), join enforcement
-- (20260718), feed (20260612), contributions (20260721), chat membership (20260621).

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------

alter table public.events
  add column if not exists cover_emoji text
    check (cover_emoji is null or char_length(cover_emoji) between 1 and 16);

alter table public.event_requests
  add column if not exists rsvp_status text not null default 'going'
    check (rsvp_status in ('going', 'maybe', 'not_going'));

-- Counts filter on (event_id, status, rsvp_status); extend the existing shape.
create index if not exists event_requests_event_status_rsvp_idx
  on public.event_requests (event_id, status, rsvp_status);

-- ---------------------------------------------------------------------------
-- 2. create_event: persist cover_emoji (body from 20260804 + one field)
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
-- 3. request_to_join_event: join AS going or maybe (body from 20260718 + p_rsvp).
--    Dropped (not replaced): the added defaulted param changes the signature, and
--    leaving the 2-arg version behind would make PostgREST calls ambiguous.
--    Capacity only gates 'going' joins — a 'maybe' doesn't hold a spot, so on an
--    auto-approve event it's approved even when the event is full.
-- ---------------------------------------------------------------------------

drop function if exists public.request_to_join_event(uuid, text);

create function public.request_to_join_event(
  p_event_id uuid,
  p_message text default null,
  p_rsvp text default 'going'
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  req_id uuid;
  v_uid uuid := auth.uid();
  v_host uuid;
  v_auto boolean;
  v_max integer;
  v_going_count integer;
  v_status text := 'pending';
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_rsvp not in ('going', 'maybe') then
    raise exception 'invalid_rsvp' using errcode = 'P0001';
  end if;

  -- Verified gate (phone OR email — 20260717 stamps phone_verified_at on email confirm).
  -- Mirrors the er_insert_self RLS policy we bypass under definer rights.
  if not exists (
    select 1 from public.users u
    where u.id = v_uid and u.phone_verified_at is not null
  ) then
    raise exception 'not_verified' using errcode = 'P0001';
  end if;

  select e.host_id, coalesce(e.auto_approve, false), e.max_attendees
  into v_host, v_auto, v_max
  from public.events e
  where e.id = p_event_id and e.status = 'open';

  if v_host is null then
    raise exception 'event_not_open' using errcode = 'P0001';
  end if;
  if v_host = v_uid then
    raise exception 'host_cannot_request_own_event' using errcode = 'P0001';
  end if;

  -- "Anyone can join" → approve instantly while there's room; once full, fall back to
  -- the host's approval queue (host can still admit over capacity at their discretion).
  -- Only confirmed 'going' rows occupy capacity.
  if v_auto then
    if p_rsvp = 'maybe' then
      v_status := 'approved';
    else
      select count(*) into v_going_count
      from public.event_requests er
      where er.event_id = p_event_id
        and er.status in ('approved', 'attended')
        and er.rsvp_status = 'going';
      if v_max is null or v_going_count < v_max then
        v_status := 'approved';
      end if;
    end if;
  end if;

  -- decided_at is normally stamped by the BEFORE UPDATE trigger; an auto-approved row
  -- never updates, so stamp it here so the approval has a timestamp.
  insert into public.event_requests (event_id, requester_id, message, status, rsvp_status, decided_at)
  values (
    p_event_id, v_uid, p_message, v_status, p_rsvp,
    case when v_status = 'approved' then now() else null end
  )
  on conflict (event_id, requester_id) do nothing
  returning id into req_id;

  if req_id is null then
    raise exception 'request_already_exists' using errcode = 'P0001';
  end if;

  return req_id;
end;
$$;

revoke execute on function public.request_to_join_event(uuid, text, text) from public, anon;
grant execute on function public.request_to_join_event(uuid, text, text) to authenticated;

-- ---------------------------------------------------------------------------
-- 4. set_event_rsvp: flip the caller's own intent on an existing request.
--    Host approval is untouched; only the capacity re-check guards flip-to-going.
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
-- 5. Group-chat membership follows BOTH knobs (body from 20260621, rsvp-aware):
--    member iff status in (approved, attended) AND rsvp_status <> 'not_going'.
-- ---------------------------------------------------------------------------

create or replace function public._sync_event_group_membership()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread uuid;
  v_was_member boolean;
  v_is_member boolean;
begin
  select id into v_thread
  from public.chat_threads
  where event_id = new.event_id and kind = 'group_event';

  if v_thread is null then
    return new;
  end if;

  v_is_member := new.status in ('approved', 'attended')
                 and coalesce(new.rsvp_status, 'going') <> 'not_going';
  v_was_member := tg_op = 'UPDATE'
                  and old.status in ('approved', 'attended')
                  and coalesce(old.rsvp_status, 'going') <> 'not_going';

  if v_is_member and not v_was_member then
    insert into public.chat_thread_members (thread_id, user_id)
    values (v_thread, new.requester_id)
    on conflict (thread_id, user_id) do update set left_at = null;  -- rejoin clears left_at

  elsif v_was_member and not v_is_member then
    update public.chat_thread_members
    set left_at = now()
    where thread_id = v_thread and user_id = new.requester_id and left_at is null;
  end if;

  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Event previews (bodies from 20260816): + cover_emoji, going-only
--    participant_count/roster, maybe_count, and (authed) my_rsvp_status.
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
-- 7. Feed (bodies from 20260612): + cover_emoji + maybe_count (+ my_rsvp_status
--    on the authed feed), going-only participant_count/preview. Adding OUT
--    columns changes the return type, so drop + recreate + re-grant.
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
-- 8. Radar contributions (body from 20260721): the host's "N going" counts
--    confirmed going only (jsonb return — no signature change).
-- ---------------------------------------------------------------------------

create or replace function public.get_my_contributions(p_since timestamptz default null)
returns jsonb
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  with mine as (
    -- My local signals: offers, seeks, tips, casual host asks.
    select
      'signal'::text       as kind,
      s.id                 as id,
      s.intent             as intent,
      s.detail_text        as title,
      s.category           as category,
      s.status             as status,
      s.created_at         as created_at,
      s.photo_url          as photo_url,
      null::uuid           as event_id,
      null::timestamptz    as starts_at,
      null::int            as yes_count,
      null::int            as capacity,
      (
        select u.nickname
        from public.block_log_entries b
        join public.users u on u.id = b.peer_user_id
        where b.my_signal_id = s.id and b.peer_user_id is not null
        order by b.created_at desc
        limit 1
      )                    as peer_label
    from public.local_signals s
    where s.user_id = auth.uid()
      and (p_since is null or s.created_at >= p_since)

    union all

    -- Meets I host (published events) — title, when, and N-of-capacity going.
    select
      'event'::text        as kind,
      e.id                 as id,
      'host_meet'::text    as intent,
      e.title              as title,
      null::text           as category,
      e.status             as status,
      e.created_at         as created_at,
      null::text           as photo_url,
      e.id                 as event_id,
      e.starts_at          as starts_at,
      (select count(*)::int from public.event_requests er
        where er.event_id = e.id
          and er.status in ('approved', 'attended')
          and er.rsvp_status = 'going') as yes_count,
      e.max_attendees      as capacity,
      null::text           as peer_label
    from public.events e
    where e.host_id = auth.uid()
      and (p_since is null or e.created_at >= p_since)

    union all

    -- Meets I asked to join — status is the REQUEST's status (pending/approved/attended).
    select
      'request'::text      as kind,
      e.id                 as id,
      'joined_meet'::text  as intent,
      e.title              as title,
      null::text           as category,
      er.status            as status,
      er.created_at        as created_at,
      null::text           as photo_url,
      e.id                 as event_id,
      e.starts_at          as starts_at,
      null::int            as yes_count,
      e.max_attendees      as capacity,
      null::text           as peer_label
    from public.event_requests er
    join public.events e on e.id = er.event_id
    where er.requester_id = auth.uid()
      and er.status in ('pending', 'approved', 'attended')
      and (p_since is null or er.created_at >= p_since)
  )
  select coalesce(jsonb_agg(to_jsonb(m) order by m.created_at desc), '[]'::jsonb)
  from mine m;
$$;

revoke execute on function public.get_my_contributions(timestamptz) from public, anon;
grant execute on function public.get_my_contributions(timestamptz) to authenticated;
