-- Enforce the host's join settings at JOIN time.
--
-- The host flow captures auto_approve (anyone can join vs host approves each) and
-- max_attendees (capacity), and create_event persists both — but request_to_join_event
-- ignored them: every join landed as 'pending' regardless. So "anyone can join" never
-- actually let anyone in, and capacity was never enforced.
--
-- This rewrites request_to_join_event to honor the settings:
--   • auto_approve = false (host approves each)      -> 'pending'  (host queue; unchanged)
--   • auto_approve = true  + capacity available      -> 'approved' (instant join)
--   • auto_approve = true  + at capacity (full)      -> 'pending'  (falls back to host)
-- Approved rows auto-join the event group chat via the existing
-- event_requests_group_membership trigger (20260621120000) — no extra wiring.
--
-- SECURITY DEFINER: the function must read the TRUE approved-attendee count, but the
-- er_select_self_or_host RLS policy hides other people's rows from a joiner, so an
-- invoker-rights count would undercount and break the capacity check. Definer rights let
-- it count accurately; we re-enforce every check the RLS insert policy made (verified
-- account, open event, not the host, self only) by hand so nothing is weakened.

create or replace function public.request_to_join_event(
  p_event_id uuid,
  p_message text default null
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
  v_approved_count integer;
  v_status text := 'pending';
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
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
  if v_auto then
    select count(*) into v_approved_count
    from public.event_requests er
    where er.event_id = p_event_id
      and er.status in ('approved', 'attended');
    if v_max is null or v_approved_count < v_max then
      v_status := 'approved';
    end if;
  end if;

  -- decided_at is normally stamped by the BEFORE UPDATE trigger; an auto-approved row
  -- never updates, so stamp it here so the approval has a timestamp.
  insert into public.event_requests (event_id, requester_id, message, status, decided_at)
  values (
    p_event_id, v_uid, p_message, v_status,
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

revoke execute on function public.request_to_join_event(uuid, text) from public, anon;
grant execute on function public.request_to_join_event(uuid, text) to authenticated;

-- allow_attendee_share: whether anyone but the host may surface the share/invite
-- affordance. The host always can; everyone else only when the host chose "let them
-- share". A focused read (vs. re-pasting the large get_event_preview bodies) the FE
-- calls to gate the Share button. Open events only; null/closed → false.
create or replace function public.event_allows_attendee_share(p_event_id uuid)
returns boolean
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  select coalesce(allow_attendee_share, false)
  from public.events
  where id = p_event_id and status = 'open';
$$;

revoke execute on function public.event_allows_attendee_share(uuid) from public;
grant execute on function public.event_allows_attendee_share(uuid) to anon, authenticated;
