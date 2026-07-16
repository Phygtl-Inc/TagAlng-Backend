-- Co-host visibility in Radar contributions
-- ---------------------------------------------------------------------------
-- An accepted co-host (events.cohost_id, see 20260802 cohost_full_flow) could
-- edit the event and manage its join queue, but the event never appeared in
-- their Radar / "hosting" feed: get_my_contributions' events arm matched
-- host_id only, and the co-host has no event_requests row either, so no arm
-- of the union returned it. Broaden the events arm to host OR co-host.
-- Body otherwise identical to 20260817 (rsvp-going counts, jsonb return).
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
