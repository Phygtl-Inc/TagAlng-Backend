-- Queued contributions: swap items and tips captured in chat BEFORE the swap/tip
-- surfaces are live on the user's block.
--
-- QA (2026-07-08): the pass-along capture worked beautifully ("3T rain boots · free")
-- and then dead-ended — swaps are "Coming soon" in the UI, so the listing went nowhere
-- and Lana's "it's listed on your block" was a lie. Instead of dead-ending, Lana now
-- closes the capture with an honest promise ("I'll hold your listing — swaps open on
-- your block soon and yours will be first up") and parks the contribution here, so it
-- can be promoted into local_signals the day the surface opens, first in line.
--
-- One row per queued contribution (append log). `notify` is true only when the user has
-- a verified contact to ping when the surface opens; anonymous/unverified users queue
-- with notify=false. `payload` carries the full capture (title/condition/intent_type/
-- detail_text/photo/zip…) so promotion needs no re-ask.
--
-- Service-role only for writes: the worker inserts; no anon/authenticated access.
-- Users see their own queued rows through get_my_contributions (definer RPC below).
create table if not exists public.queued_contributions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  block_id text references public.blocks(id) on delete set null,
  kind text not null check (kind in ('swap_item', 'tip')),
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'queued',
  notify boolean not null default true,
  created_at timestamptz not null default now()
);

create index if not exists queued_contributions_user_idx
  on public.queued_contributions (user_id);
create index if not exists queued_contributions_status_idx
  on public.queued_contributions (status, created_at desc);

alter table public.queued_contributions enable row level security;
-- No policies: RLS denies anon/authenticated by default; service_role bypasses RLS.

-- Extend get_my_contributions with a fourth source: contributions queued for launch,
-- so the Radar "Your contributions" list shows the held items ("Queued — opens with
-- swaps on your block") instead of silently dropping them.
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

    union all

    -- Contributions queued for launch — swaps/tips Lana is holding until the surface
    -- opens on the block. intent mirrors the local_signals vocabulary so the FE can
    -- reuse its labels; status='queued' distinguishes the held state.
    select
      'queued'::text       as kind,
      q.id                 as id,
      coalesce(
        nullif(q.payload->>'intent', ''),
        case q.kind when 'swap_item' then 'swap_offer' else 'tip_share' end
      )                    as intent,
      coalesce(
        nullif(q.payload->>'detail_text', ''),
        nullif(q.payload->>'title', ''),
        case q.kind when 'swap_item' then 'Queued swap item' else 'Queued tip' end
      )                    as title,
      nullif(q.payload->>'category', '') as category,
      q.status             as status,
      q.created_at         as created_at,
      nullif(q.payload->>'photo_url', '') as photo_url,
      null::uuid           as event_id,
      null::timestamptz    as starts_at,
      null::int            as yes_count,
      null::int            as capacity,
      null::text           as peer_label
    from public.queued_contributions q
    where q.user_id = auth.uid()
      and q.status = 'queued'
      and (p_since is null or q.created_at >= p_since)
  )
  select coalesce(jsonb_agg(to_jsonb(m) order by m.created_at desc), '[]'::jsonb)
  from mine m;
$$;

revoke execute on function public.get_my_contributions(timestamptz) from public, anon;
grant execute on function public.get_my_contributions(timestamptz) to authenticated;
