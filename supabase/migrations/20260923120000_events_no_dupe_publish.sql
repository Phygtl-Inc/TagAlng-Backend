-- ============================================================================
-- DOUBLE-PUBLISH: one tap, one event
--
-- PROBLEM (verified on prod kmetmatfxdkrialwrnzj, 2026-08-03)
--   Two identical rows, same host, same title, same starts_at, same venue:
--     83d5b8e1-e73a-4590-b396-7d288519dc17  created 14:23:07.979
--     a700dd06-3557-44aa-b9bc-8f1b0ba8234f  created 14:23:16.886
--   Nine seconds apart. Nothing stops it at any layer:
--     * public.events has NO unique constraint or unique index (only plain
--       indexes on cluster_id/starts_at, host_id, block_id, location);
--     * create_event validates auth/location/title/cohort/cohost then runs a
--       bare INSERT -- no on-conflict, no duplicate lookup;
--     * publish_event (worker) has no idempotency key.
--   The /complete handler's `session_already_completed` check guards one path,
--   not _auto_publish_event, and cannot help across two requests anyway.
--
-- THE KEY is (host_id, lower(title), starts_at) WHERE status = 'open'.
--   * lower(title) so "Pizza Playdate" and "pizza playdate" collide;
--   * starts_at included, so a weekly meet is a different row every week --
--     recurrence (whenever it ships as per-occurrence rows) is unaffected;
--   * partial on 'open', so cancelling an event frees the slot to re-create the
--     same thing later, and cancelled history never blocks a host.
--   What it DOES block: one host, one title, the same exact instant, twice.
--   That is a double-submit. The only honest false positive is two genuinely
--   distinct same-named sessions at the identical timestamp.
--
-- Non-CONCURRENTLY on purpose: supabase db push runs each migration in a
-- transaction, where CONCURRENTLY is not allowed. public.events is small (5
-- rows on prod) so the brief lock is a non-event.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. Free the slot on existing duplicates, without losing anything.
--
-- Keeps the FIRST row of each duplicate group and marks the later ones
-- 'cancelled'. Deliberately NOT a delete: event_requests, messages and bring
-- claims may already reference them, and a row is evidence of what happened.
-- A plain UPDATE also skips the cancel_event RPC, so nobody is notified about
-- a duplicate they never knew existed.
-- ----------------------------------------------------------------------------
with ranked as (
  select
    id,
    row_number() over (
      partition by host_id, lower(btrim(title)), starts_at
      order by created_at, id
    ) as rn
  from public.events
  where status = 'open'
)
update public.events e
set status = 'cancelled',
    updated_at = now()
from ranked r
where r.id = e.id
  and r.rn > 1;


-- ----------------------------------------------------------------------------
-- 2. The constraint itself.
-- ----------------------------------------------------------------------------
create unique index if not exists events_no_dupe_open_publish
  on public.events (host_id, lower(btrim(title)), starts_at)
  where status = 'open';

comment on index public.events_no_dupe_open_publish is
  'One open event per (host, title, start). Blocks the double-tap that put two '
  'identical "Under 8s Pizza Playdate" rows in prod 9 seconds apart. Partial on '
  'status=open so cancelling frees the slot; starts_at is in the key so weekly '
  'recurrence is unaffected.';


-- ----------------------------------------------------------------------------
-- 3. Make create_event idempotent.
--
-- A constraint alone turns the second tap into a 23505 -> HTTP 500 and a scary
-- error in the user's face for something that already succeeded. Instead:
-- catch the violation and return the event the FIRST tap created. The caller
-- gets the same uuid either way and cannot tell the difference, which is what
-- idempotent means.
--
-- Body is otherwise byte-identical to 20260829120000_event_has_time.sql.
-- ----------------------------------------------------------------------------
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
      cover_emoji
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
      nullif(left(btrim(coalesce(p_fields->>'cover_emoji', '')), 16), '')
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
  'returns the first event''s id instead of creating a duplicate or erroring.';


-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
--   drop index if exists public.events_no_dupe_open_publish;
--   -- then re-apply create_event from 20260829120000_event_has_time.sql
--
-- Step 1's cancellations are NOT reversed by the rollback: re-opening them
-- would recreate the duplicates the index exists to prevent. To restore one
-- deliberately, set status='open' after dropping the index.
-- ============================================================================
