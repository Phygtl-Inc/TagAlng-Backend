-- get_my_contributions: one feed of everything the caller has posted — their
-- local_signals (offers / seeks / tips / casual host asks) PLUS the meets they host
-- (events). Powers the Radar "Your contributions" list (C-CONTRIB-LIST).
--
-- The data already existed (local_signals + events) but there was no single reader.
-- Each row is normalized so the FE renders one list and routes a tap:
--   • kind='event' → open /meet/{event_id}
--   • kind='signal' → (no detail page yet) the FE opens chat to manage it
--
-- SECURITY DEFINER: every row is scoped to auth.uid() (own signals / hosted events),
-- so definer rights are safe and let us read the matched peer's public nickname for the
-- "matched · <name>" label without tripping cross-table RLS.

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
        where er.event_id = e.id and er.status in ('approved', 'attended')) as yes_count,
      e.max_attendees      as capacity,
      null::text           as peer_label
    from public.events e
    where e.host_id = auth.uid()
      and (p_since is null or e.created_at >= p_since)
  )
  select coalesce(jsonb_agg(to_jsonb(m) order by m.created_at desc), '[]'::jsonb)
  from mine m;
$$;

revoke execute on function public.get_my_contributions(timestamptz) from public, anon;
grant execute on function public.get_my_contributions(timestamptz) to authenticated;
