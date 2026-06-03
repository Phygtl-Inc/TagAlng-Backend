-- TagAlng: internal admin inbox for Lana sessions (WhatsApp-style ops UI)
-- Admins sign in via Supabase Auth (email/password); uid must be on allowlist.

create table if not exists public.admin_allowlist (
  user_id uuid primary key references auth.users (id) on delete cascade,
  label text,
  created_at timestamptz not null default now()
);
comment on table public.admin_allowlist is
  'Supabase Auth user ids allowed to call admin_* RPCs. Add ops emails after creating Auth users.';
alter table public.admin_allowlist enable row level security;
create policy "admin_allowlist_no_client"
  on public.admin_allowlist for all
  to authenticated
  using (false)
  with check (false);
create or replace function public.is_tagalng_admin()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1
    from public.admin_allowlist a
    where a.user_id = auth.uid()
  );
$$;
revoke all on function public.is_tagalng_admin() from public;
grant execute on function public.is_tagalng_admin() to authenticated;
create or replace function public.admin_list_lana_sessions(
  p_limit int default 50,
  p_offset int default 0,
  p_status public.lana_session_status default null
)
returns table (
  session_id uuid,
  user_id uuid,
  phone text,
  nickname text,
  purpose public.lana_session_purpose,
  status public.lana_session_status,
  message_count bigint,
  last_message_at timestamptz,
  last_message_preview text,
  last_role text,
  created_at timestamptz,
  updated_at timestamptz,
  completed_at timestamptz
)
language plpgsql
stable
security definer
set search_path = public
as $$
begin
  if not public.is_tagalng_admin() then
    raise exception 'admin_forbidden' using errcode = '42501';
  end if;

  return query
  select
    s.id,
    s.user_id,
    u.phone,
    u.nickname,
    s.purpose,
    s.status,
    (
      select count(*)::bigint
      from public.lana_messages m
      where m.session_id = s.id
    ),
    lm.created_at,
    left(lm.content, 120),
    lm.role,
    s.created_at,
    s.updated_at,
    s.completed_at
  from public.lana_sessions s
  join public.users u on u.id = s.user_id
  left join lateral (
    select m.role, m.content, m.created_at
    from public.lana_messages m
    where m.session_id = s.id
    order by m.created_at desc
    limit 1
  ) lm on true
  where (p_status is null or s.status = p_status)
  order by coalesce(lm.created_at, s.updated_at) desc nulls last
  limit greatest(1, least(p_limit, 100))
  offset greatest(0, p_offset);
end;
$$;
grant execute on function public.admin_list_lana_sessions(int, int, public.lana_session_status)
  to authenticated;
create or replace function public.admin_get_lana_conversation(
  p_session_id uuid
)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  result jsonb;
begin
  if not public.is_tagalng_admin() then
    raise exception 'admin_forbidden' using errcode = '42501';
  end if;

  select jsonb_build_object(
    'session', jsonb_build_object(
      'id', s.id,
      'user_id', s.user_id,
      'purpose', s.purpose,
      'status', s.status,
      'context', s.context,
      'created_at', s.created_at,
      'updated_at', s.updated_at,
      'completed_at', s.completed_at
    ),
    'user', jsonb_build_object(
      'id', u.id,
      'phone', u.phone,
      'nickname', u.nickname,
      'home_block_id', u.home_block_id,
      'home_zip', u.home_zip
    ),
    'messages', coalesce(
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', m.id,
            'role', m.role,
            'content', m.content,
            'metadata', m.metadata,
            'ui', m.metadata -> 'ui',
            'created_at', m.created_at
          )
          order by m.created_at asc
        )
        from public.lana_messages m
        where m.session_id = s.id
      ),
      '[]'::jsonb
    ),
    'claims', coalesce(
      (
        select jsonb_agg(
          jsonb_build_object(
            'id', c.id,
            'concept', c.concept,
            'label', c.label,
            'confidence', c.confidence,
            'disclosure', c.disclosure,
            'synonyms', c.synonyms,
            'source_quote', c.source_quote,
            'bucket', c.bucket,
            'created_at', c.created_at
          )
          order by c.confidence desc
        )
        from public.user_identity_claims c
        where c.user_id = s.user_id
          and c.dismissed_at is null
      ),
      '[]'::jsonb
    )
  )
  into result
  from public.lana_sessions s
  join public.users u on u.id = s.user_id
  where s.id = p_session_id;

  if result is null then
    raise exception 'session_not_found' using errcode = 'P0002';
  end if;

  return result;
end;
$$;
grant execute on function public.admin_get_lana_conversation(uuid) to authenticated;
