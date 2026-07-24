-- Cancelled-meet affordances (walkthrough C-18-CANCELLED / C-18-ATTENDEE-CANCELLED)
-- ---------------------------------------------------------------------------
-- 1. dismiss_event(p_event_id): per-user "remove from my list". A dismissal is
--    a private event_dismissals row for the CALLER only — no global state
--    change. Works for the host of a cancelled meet ("Remove from my list")
--    and for an attendee self-removing (issue #53). Idempotent.
-- 2. get_my_contributions: events I host/co-host and meets I asked to join now
--    exclude events the caller has dismissed. (Signals are unaffected —
--    dismissals are event-keyed.)
-- 3. cancel_event: was a bare `update events set status='cancelled'` — no one
--    was told anything, so the FE's "I let everyone know" claim was false.
--    Now host-only (matching the 20260802 policy comment "Cancel/delete +
--    revoke stay host-only"), idempotent, and posts a system message into the
--    event group chat (the in-app signal every approved attendee sees). Push +
--    email fan-out to the going roster happens in the worker via
--    POST /hooks/event-cancel, mirroring /hooks/event-join.
-- ---------------------------------------------------------------------------

-- 1. Per-user dismissals -----------------------------------------------------

create table if not exists public.event_dismissals (
  user_id      uuid not null references public.users(id) on delete cascade,
  event_id     uuid not null references public.events(id) on delete cascade,
  dismissed_at timestamptz not null default now(),
  primary key (user_id, event_id)
);

comment on table public.event_dismissals is
  'Per-user "remove from my list" for meets (e.g. a cancelled meet card). '
  'Hides the event from the dismissing user''s own feeds only.';

alter table public.event_dismissals enable row level security;

drop policy if exists "event_dismissals_select_own" on public.event_dismissals;
create policy "event_dismissals_select_own"
  on public.event_dismissals for select
  to authenticated
  using (user_id = auth.uid());
-- No insert/update/delete policies: writes go through dismiss_event() only.

create or replace function public.dismiss_event(p_event_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if not exists (select 1 from public.events e where e.id = p_event_id) then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;
  -- security definer, but the row is always keyed on auth.uid(): a caller can
  -- only ever dismiss for themselves. No-op when already dismissed.
  insert into public.event_dismissals (user_id, event_id)
  values (auth.uid(), p_event_id)
  on conflict (user_id, event_id) do nothing;
end;
$$;

revoke all on function public.dismiss_event(uuid) from public, anon;
grant execute on function public.dismiss_event(uuid) to authenticated;

-- 2. Filter dismissed events out of the caller's contributions feed ----------
-- Body identical to 20260818 (host OR co-host arm, rsvp-going counts) plus a
-- not-exists dismissal filter on both event-backed arms.

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

    -- Meets I host or co-host (published events) — title, when, N-of-capacity going.
    -- cohost_meet lets the Radar card badge "CO-HOSTING" instead of "HOSTING".
    select
      'event'::text        as kind,
      e.id                 as id,
      case when e.cohost_id = auth.uid() and e.host_id <> auth.uid()
           then 'cohost_meet' else 'host_meet' end::text as intent,
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
    where (e.host_id = auth.uid() or e.cohost_id = auth.uid())
      and (p_since is null or e.created_at >= p_since)
      and not exists (
        select 1 from public.event_dismissals d
        where d.event_id = e.id and d.user_id = auth.uid()
      )

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
      and not exists (
        select 1 from public.event_dismissals d
        where d.event_id = e.id and d.user_id = auth.uid()
      )
  )
  select coalesce(jsonb_agg(to_jsonb(m) order by m.created_at desc), '[]'::jsonb)
  from mine m;
$$;

revoke execute on function public.get_my_contributions(timestamptz) from public, anon;
grant execute on function public.get_my_contributions(timestamptz) to authenticated;

-- 3. cancel_event: host-only, idempotent, in-app group-chat notice -----------

create or replace function public.cancel_event(p_event_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_host uuid;
  v_status text;
  v_thread uuid;
begin
  select e.host_id, e.status into v_host, v_status
  from public.events e where e.id = p_event_id;
  if not found then
    raise exception 'event_not_found' using errcode = 'P0001';
  end if;
  -- Host-only by design (the events update policy also admits the co-host,
  -- but cancel stays with the host — see 20260802 cohost_full_flow).
  if v_host is distinct from auth.uid() then
    raise exception 'not_event_host' using errcode = 'P0001';
  end if;
  if v_status = 'cancelled' then
    return;  -- idempotent: no second system message
  end if;

  update public.events e
  set status = 'cancelled'
  where e.id = p_event_id;

  -- In-app notice: everyone in the meet's group chat sees the cancellation.
  select t.id into v_thread
  from public.chat_threads t
  where t.event_id = p_event_id and t.kind = 'group_event'
  limit 1;
  if v_thread is not null then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_thread, null, 'system', 'This meet was cancelled by the host.');
    update public.chat_threads set last_message_at = now() where id = v_thread;
  end if;
end;
$$;

revoke all on function public.cancel_event(uuid) from public, anon;
grant execute on function public.cancel_event(uuid) to authenticated;
