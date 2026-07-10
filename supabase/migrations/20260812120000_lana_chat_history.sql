-- Lana chat scroll-back: the PWA wants one continuous transcript across ALL of the
-- caller's Lana sessions (today it can only read one session at a time via
-- get_lana_session_messages). Newest-first keyset pagination — each scroll-to-top
-- fetch passes the oldest already-loaded message as the (created_at, id) cursor and
-- prepends the page. Keyset (not offset) so pages stay stable while new turns land.
-- session_id/purpose ride along so the UI can draw "new conversation" separators
-- where session_id changes.

create or replace function public.get_lana_chat_history(
  p_limit integer default 50,
  p_before_created_at timestamptz default null,
  p_before_id uuid default null
)
returns table (
  id uuid,
  session_id uuid,
  purpose public.lana_session_purpose,
  role text,
  content text,
  metadata jsonb,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    m.id,
    m.session_id,
    s.purpose,
    m.role,
    m.content,
    m.metadata,
    m.created_at
  from public.lana_messages m
  join public.lana_sessions s on s.id = m.session_id
  where s.user_id = auth.uid()
    and (
      p_before_created_at is null
      or m.created_at < p_before_created_at
      or (
        p_before_id is not null
        and m.created_at = p_before_created_at
        and m.id < p_before_id
      )
    )
  order by m.created_at desc, m.id desc
  limit greatest(1, least(coalesce(p_limit, 50), 200));
$$;

comment on function public.get_lana_chat_history(integer, timestamptz, uuid) is
  'Caller''s full Lana transcript across sessions, newest first, keyset-paginated by (created_at, id).';

revoke execute on function public.get_lana_chat_history(integer, timestamptz, uuid) from public;
grant execute on function public.get_lana_chat_history(integer, timestamptz, uuid) to authenticated;
