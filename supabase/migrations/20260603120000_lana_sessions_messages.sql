-- TagAlng: Lana conversational sessions (signup profile intake + future event draft)

create type public.lana_session_purpose as enum (
  'profile_intake',
  'event_draft'
);
create type public.lana_session_status as enum (
  'active',
  'completed',
  'abandoned'
);
create table public.lana_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  purpose public.lana_session_purpose not null default 'profile_intake',
  status public.lana_session_status not null default 'active',
  context jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint lana_sessions_context_is_object check (jsonb_typeof(context) = 'object')
);
comment on table public.lana_sessions is
  'Lana agent sessions. Chat turns in lana_messages; profile truth in user_identity_claims after complete.';
create index lana_sessions_user_id_active_idx
  on public.lana_sessions (user_id, created_at desc)
  where status = 'active';
create trigger lana_sessions_updated_at
before update on public.lana_sessions
for each row execute function public.set_updated_at();
create table public.lana_messages (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references public.lana_sessions (id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null check (char_length(content) >= 1 and char_length(content) <= 8000),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint lana_messages_metadata_is_object check (jsonb_typeof(metadata) = 'object')
);
comment on table public.lana_messages is
  'Lana chat transcript. Not used for matching — claims table is source of truth.';
create index lana_messages_session_id_created_idx
  on public.lana_messages (session_id, created_at asc);
alter table public.lana_sessions enable row level security;
alter table public.lana_messages enable row level security;
create policy "lana_sessions_select_own"
  on public.lana_sessions for select
  to authenticated
  using (user_id = auth.uid());
create policy "lana_sessions_no_client_write"
  on public.lana_sessions for all
  to authenticated
  using (false)
  with check (false);
create policy "lana_messages_select_own_session"
  on public.lana_messages for select
  to authenticated
  using (
    exists (
      select 1
      from public.lana_sessions s
      where s.id = session_id
        and s.user_id = auth.uid()
    )
  );
create policy "lana_messages_no_client_write"
  on public.lana_messages for all
  to authenticated
  using (false)
  with check (false);
-- Resume active profile intake session (optional frontend helper)
create or replace function public.get_active_lana_session(
  p_purpose public.lana_session_purpose default 'profile_intake'
)
returns table (
  session_id uuid,
  purpose public.lana_session_purpose,
  status public.lana_session_status,
  context jsonb,
  created_at timestamptz,
  message_count bigint
)
language sql
stable
security definer
set search_path = public
as $$
  select
    s.id,
    s.purpose,
    s.status,
    s.context,
    s.created_at,
    (
      select count(*)::bigint
      from public.lana_messages m
      where m.session_id = s.id
    )
  from public.lana_sessions s
  where s.user_id = auth.uid()
    and s.purpose = p_purpose
    and s.status = 'active'
  order by s.created_at desc
  limit 1;
$$;
comment on function public.get_active_lana_session(public.lana_session_purpose) is
  'Returns latest active Lana session for the caller, if any.';
grant execute on function public.get_active_lana_session(public.lana_session_purpose) to authenticated;
create or replace function public.get_lana_session_messages(
  p_session_id uuid
)
returns table (
  id uuid,
  role text,
  content text,
  metadata jsonb,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = public
as $$
  select m.id, m.role, m.content, m.metadata, m.created_at
  from public.lana_messages m
  join public.lana_sessions s on s.id = m.session_id
  where m.session_id = p_session_id
    and s.user_id = auth.uid()
  order by m.created_at asc;
$$;
grant execute on function public.get_lana_session_messages(uuid) to authenticated;
