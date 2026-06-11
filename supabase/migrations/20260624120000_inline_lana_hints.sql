-- Inline Lana hints (#6, DB foundation): the "only you can see this" tooltips that
-- appear inside a chat thread with 1-3 CTA chips. Lana DECIDES when to drop one
-- (event-driven) — they are never user-summonable. Drafting (Vertex AI) lives in
-- the lana-worker, which calls create_lana_hint (service_role) to persist + push.
-- Users can only fetch / dismiss / act on their OWN hints. Cap: 10 per user / day.

-- ---------------------------------------------------------------------------
-- Table
-- ---------------------------------------------------------------------------

create table if not exists public.lana_inline_hints (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,   -- the only viewer
  thread_id uuid not null references public.chat_threads (id) on delete cascade,
  text text not null check (char_length(text) between 1 and 500),
  cta_chips jsonb not null default '[]'::jsonb,    -- [{label, action, payload?}]
  context jsonb not null default '{}'::jsonb,      -- {trigger, focused_entity, recent_action}
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '5 minutes'),
  dismissed_at timestamptz,
  acted_at timestamptz,
  constraint lana_inline_hints_chips_is_array check (jsonb_typeof(cta_chips) = 'array'),
  constraint lana_inline_hints_context_is_object check (jsonb_typeof(context) = 'object')
);

comment on table public.lana_inline_hints is
  'Private per-user tooltips inside chat threads. Created only by Lana (service_role); read/dismiss by the recipient.';

create index if not exists lana_inline_hints_user_active_idx
  on public.lana_inline_hints (user_id, created_at desc)
  where dismissed_at is null and acted_at is null;

create index if not exists lana_inline_hints_thread_idx
  on public.lana_inline_hints (thread_id);

alter table public.lana_inline_hints enable row level security;

-- Recipient reads only their own; no client writes (all mutations via RPCs).
create policy "lana_inline_hints_select_own"
  on public.lana_inline_hints for select
  to authenticated
  using (user_id = auth.uid());

create policy "lana_inline_hints_no_client_write"
  on public.lana_inline_hints for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- create_lana_hint (INTERNAL — called by the lana-worker after drafting).
-- Enforces the 10/day per-user cap; returns the hint id, or null if capped.
-- ---------------------------------------------------------------------------

create or replace function public.create_lana_hint(
  p_user_id uuid,
  p_thread_id uuid,
  p_text text,
  p_cta_chips jsonb default '[]'::jsonb,
  p_context jsonb default '{}'::jsonb
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_today_count int;
  v_id uuid;
begin
  if p_user_id is null or p_thread_id is null then
    raise exception 'user_and_thread_required' using errcode = 'P0001';
  end if;
  if p_text is null or char_length(trim(p_text)) < 1 then
    raise exception 'hint_text_required' using errcode = 'P0001';
  end if;
  -- Defensive: only drop a hint for someone actually in the thread.
  if not public.lana_in_thread(p_thread_id, p_user_id) then
    raise exception 'recipient_not_in_thread' using errcode = 'P0001';
  end if;

  -- Cap: 10 hints per recipient per rolling 24h ("don't over-presence").
  select count(*) into v_today_count
  from public.lana_inline_hints
  where user_id = p_user_id and created_at > now() - interval '1 day';

  if v_today_count >= 10 then
    return null;  -- capped; caller should treat as "no hint dropped"
  end if;

  insert into public.lana_inline_hints (user_id, thread_id, text, cta_chips, context)
  values (
    p_user_id, p_thread_id, trim(p_text),
    coalesce(p_cta_chips, '[]'::jsonb), coalesce(p_context, '{}'::jsonb)
  )
  returning id into v_id;

  return v_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- get_my_active_hints — recipient fetches live (non-dismissed/acted/expired) hints
-- ---------------------------------------------------------------------------

create or replace function public.get_my_active_hints(p_thread_id uuid default null)
returns table (
  hint_id uuid,
  thread_id uuid,
  text text,
  cta_chips jsonb,
  context jsonb,
  created_at timestamptz,
  expires_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select h.id, h.thread_id, h.text, h.cta_chips, h.context, h.created_at, h.expires_at
  from public.lana_inline_hints h
  where h.user_id = auth.uid()
    and h.dismissed_at is null
    and h.acted_at is null
    and h.expires_at > now()
    and (p_thread_id is null or h.thread_id = p_thread_id)
  order by h.created_at desc;
$$;

-- ---------------------------------------------------------------------------
-- dismiss_lana_hint / act_on_lana_hint — recipient lifecycle actions
-- ---------------------------------------------------------------------------

create or replace function public.dismiss_lana_hint(p_hint_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  update public.lana_inline_hints
  set dismissed_at = now()
  where id = p_hint_id and user_id = auth.uid() and dismissed_at is null;
  if not found then
    raise exception 'hint_not_found_or_already_dismissed' using errcode = 'P0001';
  end if;
end;
$$;

create or replace function public.act_on_lana_hint(p_hint_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  update public.lana_inline_hints
  set acted_at = now()
  where id = p_hint_id and user_id = auth.uid() and acted_at is null;
  if not found then
    raise exception 'hint_not_found_or_already_acted' using errcode = 'P0001';
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- expire_lana_hints — housekeeping cron (service_role): GC old rows
-- ---------------------------------------------------------------------------

create or replace function public.expire_lana_hints()
returns int
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_count int;
begin
  delete from public.lana_inline_hints
  where created_at < now() - interval '1 day';
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

comment on function public.expire_lana_hints() is
  'Garbage-collects hints older than 1 day. Active filtering is done by get_my_active_hints (5-min expiry). Schedule hourly (service_role).';

-- ---------------------------------------------------------------------------
-- Realtime: FE receives new hints live on the lana_inline_hints stream
-- (RLS limits each client to their own rows). Guarded + idempotent.
-- ---------------------------------------------------------------------------

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
     and not exists (
       select 1 from pg_publication_tables
       where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'lana_inline_hints'
     ) then
    execute 'alter publication supabase_realtime add table public.lana_inline_hints';
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Grants: creation is Lana-only (service_role); recipients fetch/dismiss/act.
-- ---------------------------------------------------------------------------

revoke all on function public.create_lana_hint(uuid, uuid, text, jsonb, jsonb) from public, anon, authenticated;
grant execute on function public.create_lana_hint(uuid, uuid, text, jsonb, jsonb) to service_role;

revoke all on function public.get_my_active_hints(uuid) from public, anon;
grant execute on function public.get_my_active_hints(uuid) to authenticated;

revoke all on function public.dismiss_lana_hint(uuid) from public, anon;
grant execute on function public.dismiss_lana_hint(uuid) to authenticated;

revoke all on function public.act_on_lana_hint(uuid) from public, anon;
grant execute on function public.act_on_lana_hint(uuid) to authenticated;

revoke all on function public.expire_lana_hints() from public, anon, authenticated;
grant execute on function public.expire_lana_hints() to service_role;
