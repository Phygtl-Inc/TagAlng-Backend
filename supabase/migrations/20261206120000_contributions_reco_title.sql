-- Radar "Sharing a tip" rows showed the whole tip as their title:
--
--   The furniture land · furniture store · great · used for: Looks good · where to buy:
--   Main St · price: Budget · setup effort: Some assembly · durability: it has not been
--   long since I have had the furniture so I can't say how well it held up · best for: …
--
-- detail_text is the " · "-joined capture blob (embedding input + dedupe key), never
-- display copy — 20261120120000 stored the card head as its own columns for exactly this
-- reason and backfilled reco_name on legacy tip rows. get_my_contributions predates that
-- migration and was never moved over, so it is the last reader still rendering the blob.
--
-- Body identical to 20260904120000 apart from the signal arm's projection:
--   * title  = reco_name when the row has one, detail_text otherwise. The fallback is not
--     dead code — swap offers, tip seeks and casual host asks have no reco_name, and for
--     them detail_text IS the title ("good pizza").
--   * reco_type / reco_place / reco_description ride along so the card can grow a subtitle
--     line without a second migration. Null on both event-backed arms.

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
      coalesce(nullif(btrim(s.reco_name), ''), s.detail_text) as title,
      s.category           as category,
      s.status             as status,
      s.created_at         as created_at,
      s.photo_url          as photo_url,
      s.reco_type          as reco_type,
      nullif(btrim(s.reco_place), '')       as reco_place,
      nullif(btrim(s.reco_description), '') as reco_description,
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
      null::text           as reco_type,
      null::text           as reco_place,
      null::text           as reco_description,
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
      null::text           as reco_type,
      null::text           as reco_place,
      null::text           as reco_description,
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
