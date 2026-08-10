-- Recurring meets ("do this every Friday").
--
-- ONE events row per series that ROLLS FORWARD — never N materialized rows. That
-- choice is what keeps this migration small: the group chat is one-per-event-row
-- (20260621120000), the RSVP is one event_requests row per person, browse/feed/
-- similar_events/nudges all filter `starts_at > now()`, and the no-dupe index is
-- (host_id, title, starts_at). A rolling row leaves every one of those untouched.
-- A weekly meet is ONE meet whose starts_at moves, with a standing roster.
--
--   recurrence        'weekly' | 'biweekly' | 'monthly' | null (null = one-off, unchanged)
--   recurrence_until  last date the series may land on; null = 180 days from creation,
--                     so an abandoned weekly meet stops instead of haunting browse
--   recurrence_skips  occurrence dates the host called off ("skip this Friday")
--
-- Rollover is READ-REPAIR, no cron and no scheduler (same pattern as
-- 20260906120000_circles_places_phase_a.sql): opening a meet rolls that meet, and
-- the worker's browse fetch rolls every due row.
--
-- Ending a series is `cancel_event` (unchanged, host-only, already notifies).
-- Skipping ONE occurrence is skip_event_occurrence() below. Those are the only two.

alter table public.events
  add column if not exists recurrence text,
  add column if not exists recurrence_until date,
  add column if not exists recurrence_skips date[] not null default '{}';

alter table public.events drop constraint if exists events_recurrence_valid;
alter table public.events add constraint events_recurrence_valid
  check (recurrence is null or recurrence in ('weekly', 'biweekly', 'monthly'));

comment on column public.events.recurrence is
  'weekly|biweekly|monthly, or null for a one-off meet. The row''s starts_at is the '
  'NEXT occurrence and is rolled forward by roll_recurring_events().';
comment on column public.events.recurrence_until is
  'Last date the series may land on. Null means 180 days from created_at.';
comment on column public.events.recurrence_skips is
  'Occurrence dates (UTC) the host called off via skip_event_occurrence().';

-- The due-scan's index: recurring rows whose next occurrence is already behind us.
create index if not exists events_recurring_due_idx
  on public.events (starts_at)
  where recurrence is not null and status = 'open';

-- ---------------------------------------------------------------------------
-- next_occurrence — the one place the recurrence math lives
-- ---------------------------------------------------------------------------
create or replace function public.next_occurrence(
  p_anchor timestamptz,
  p_recurrence text,
  p_skips date[] default '{}',
  p_from timestamptz default now()
)
returns timestamptz
language plpgsql
stable
set search_path = pg_catalog, public
as $$
declare
  v_step interval;
  v_next timestamptz;
  n int := 0;
begin
  v_step := case p_recurrence
              when 'weekly' then interval '7 days'
              when 'biweekly' then interval '14 days'
              when 'monthly' then interval '1 month'
            end;
  if v_step is null or p_anchor is null then
    return null;  -- not a recurring event, or nothing to anchor on
  end if;

  v_next := p_anchor;
  -- Steps are measured FROM THE ANCHOR (anchor + step*n), never accumulated onto the
  -- previous value: Jan 31 + 1 month + 1 month drifts to Mar 28, while
  -- Jan 31 + (2 * 1 month) is Mar 31 — the date the host actually meant.
  -- Dates are compared in UTC because skips are WRITTEN in UTC (see
  -- skip_event_occurrence); both sides agreeing is what matters, not which zone.
  while v_next < p_from
     or (v_next at time zone 'UTC')::date = any(coalesce(p_skips, '{}'::date[])) loop
    n := n + 1;
    if n > 400 then
      return null;  -- stale past any plausible series; the caller closes it
    end if;
    v_next := p_anchor + (v_step * n);
  end loop;
  return v_next;
end;
$$;

comment on function public.next_occurrence(timestamptz, text, date[], timestamptz) is
  'First occurrence at or after p_from, skipping called-off dates. Null when the '
  'recurrence is unknown or the row is implausibly stale.';

revoke all on function public.next_occurrence(timestamptz, text, date[], timestamptz) from public, anon;
grant execute on function public.next_occurrence(timestamptz, text, date[], timestamptz) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- roll_recurring_events — read-repair: advance past-due series, close finished ones
-- ---------------------------------------------------------------------------
create or replace function public.roll_recurring_events(p_event_id uuid default null)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  d record;
  v_ok boolean;
  v_rolled integer := 0;
begin
  for d in
    select
      e.id,
      public.next_occurrence(e.starts_at, e.recurrence, e.recurrence_skips, now()) as nxt,
      case when e.ends_at is null then null else e.ends_at - e.starts_at end as dur,
      -- No end date given = 180 days from creation. A host who wants more just hosts again.
      coalesce(e.recurrence_until, (e.created_at + interval '180 days')::date) as until
    from public.events e
    where e.recurrence is not null
      and e.status = 'open'
      and e.starts_at < now()
      and (p_event_id is null or e.id = p_event_id)
  loop
    -- `until + 1` day of grace: an evening meet's UTC date is already tomorrow, and
    -- cutting a series one occurrence SHORT is the worse error of the two.
    v_ok := d.nxt is not null and (d.nxt at time zone 'UTC')::date <= d.until + 1;
    -- Row by row, each in its own subtransaction: the rolled starts_at can trip the
    -- no-dupe index (20260923120000) if the host also has a one-off with the same title
    -- at exactly that slot, and ONE such host must not freeze every other series in the
    -- table. The stuck row simply stays put and is retried on the next read.
    begin
      if v_ok then
        update public.events
        set starts_at = d.nxt,
            ends_at = case when d.dur is null then ends_at else d.nxt + d.dur end,
            updated_at = now()
        where id = d.id;
      else
        update public.events
        set status = 'completed',
            updated_at = now()
        where id = d.id;
      end if;
      v_rolled := v_rolled + 1;
    exception when unique_violation then
      null;  -- collided with one of this host's own meets; leave it for the next pass
    end;
  end loop;
  return v_rolled;
end;
$$;

comment on function public.roll_recurring_events(uuid) is
  'Read-repair for recurring meets: moves any past-due series to its next occurrence '
  'and marks a series completed once it passes recurrence_until. Idempotent — a second '
  'call touches nothing. Pass an event id to roll just that one.';

revoke all on function public.roll_recurring_events(uuid) from public, anon;
grant execute on function public.roll_recurring_events(uuid) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- skip_event_occurrence — "skip this Friday" (NOT cancel; the series lives on)
-- ---------------------------------------------------------------------------
create or replace function public.skip_event_occurrence(p_event_id uuid)
returns timestamptz
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_ev record;
  v_skip date;
  v_until date;
  v_next timestamptz;
  v_thread uuid;
begin
  select e.host_id, e.starts_at, e.ends_at, e.recurrence, e.recurrence_skips,
         e.recurrence_until, e.created_at, e.status
  into v_ev
  from public.events e
  where e.id = p_event_id;

  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;
  -- Host-only, like cancel_event: the co-host can edit a meet but not call one off.
  if v_ev.host_id is distinct from auth.uid() then
    raise exception 'not_event_host' using errcode = 'P0001';
  end if;
  if v_ev.status <> 'open' then
    raise exception 'event_not_open' using errcode = 'P0001';
  end if;
  if v_ev.recurrence is null then
    raise exception 'not_recurring' using errcode = 'P0001';
  end if;

  v_skip := (v_ev.starts_at at time zone 'UTC')::date;
  v_until := coalesce(v_ev.recurrence_until, (v_ev.created_at + interval '180 days')::date);
  -- Skipping happens BEFORE the occurrence, so starts_at is still in the future —
  -- search from just past it to force at least one step forward.
  v_next := public.next_occurrence(
    v_ev.starts_at,
    v_ev.recurrence,
    v_ev.recurrence_skips || v_skip,
    v_ev.starts_at + interval '1 second'
  );

  if v_next is null or (v_next at time zone 'UTC')::date > v_until + 1 then
    -- Skipping the last one ends the series rather than leaving a meet that never happens.
    update public.events
    set recurrence_skips = recurrence_skips || v_skip,
        status = 'completed',
        updated_at = now()
    where id = p_event_id;
    v_next := null;
  else
    begin
      update public.events
      set recurrence_skips = recurrence_skips || v_skip,
          starts_at = v_next,
          ends_at = case when ends_at is null then null else v_next + (ends_at - starts_at) end,
          updated_at = now()
      where id = p_event_id;
    exception when unique_violation then
      -- The next slot is already taken by another of this host's own open meets with the
      -- same title (no-dupe index, 20260923120000). Nothing was skipped — say so, rather
      -- than half-applying and posting a notice about a move that didn't happen.
      raise exception 'occurrence_conflict' using errcode = 'P0001';
    end;
  end if;

  -- In-app notice, so nobody shows up. Date-free on purpose: this function only knows
  -- UTC, and an evening meet's UTC date is the wrong day. The worker's push carries the
  -- real local date, in each attendee's own language.
  select t.id into v_thread
  from public.chat_threads t
  where t.event_id = p_event_id and t.kind = 'group_event'
  limit 1;
  if v_thread is not null then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (
      v_thread, null, 'system',
      case when v_next is null
        then 'The host called this one off — that was the last one in the series.'
        else 'The host skipped this one. The meet page has the next date.'
      end
    );
    update public.chat_threads set last_message_at = now() where id = v_thread;
  end if;

  return v_next;  -- next real occurrence, or null when that was the last one
end;
$$;

comment on function public.skip_event_occurrence(uuid) is
  'Call off ONE occurrence of a recurring meet and move it to the next date. Returns '
  'the new start, or null when the series ended. Use cancel_event to end the series.';

revoke all on function public.skip_event_occurrence(uuid) from public, anon;
grant execute on function public.skip_event_occurrence(uuid) to authenticated;

-- ---------------------------------------------------------------------------
-- create_event: carry recurrence through publish.
-- Body verbatim from 20260923120000_events_no_dupe_publish.sql except the two
-- recurrence columns in the insert.
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
      recurrence_until
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
      nullif(btrim(coalesce(p_fields->>'recurrence_until', '')), '')::date
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

comment on function public.create_event(jsonb) is
  'Publish an event. Idempotent since 20260923120000: a second identical publish '
  'returns the first event''s id. Carries recurrence since 20261009120000.';

revoke execute on function public.create_event(jsonb) from public, anon;
grant execute on function public.create_event(jsonb) to authenticated;

-- ---------------------------------------------------------------------------
-- get_event_preview / _authed: roll this meet first (so a recurring meet never
-- shows a date that already passed), and return the recurrence itself so the page
-- can say "Every Friday" instead of implying a one-off.
-- Bodies verbatim from 20260831120000_event_preview_cancelled.sql apart from the
-- roll call and the two new keys.
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
  -- Read-repair before the read: a weekly meet's row carries its NEXT occurrence, and
  -- nothing else advances it. Best-effort — a stale date beats a broken page.
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
    e.recurrence, e.recurrence_until
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
    e.recurrence, e.recurrence_until
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

-- Grants restated (create-or-replace preserves them; make intent explicit).
grant execute on function public.get_event_preview(uuid, double precision, double precision, text) to anon, authenticated;
revoke all on function public.get_event_preview_authed(uuid, double precision, double precision, text) from public, anon;
grant execute on function public.get_event_preview_authed(uuid, double precision, double precision, text) to authenticated;
