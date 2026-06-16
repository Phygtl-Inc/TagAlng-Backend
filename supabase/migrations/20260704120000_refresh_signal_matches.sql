-- Re-run matcher for the caller's active listening signals before block-log reads.
-- Covers the case where a peer posted their complementary signal after yours.

create or replace function public.refresh_my_signal_matches()
returns int
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_sig record;
  v_total int := 0;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  for v_sig in
    select s.id
    from public.local_signals s
    where s.user_id = v_me
      and s.status = 'listening'
      and s.block_id is not null
      and s.expires_at > now()
  loop
    v_total := v_total + public._match_local_signal(v_sig.id);
  end loop;

  return v_total;
end;
$$;

revoke all on function public.refresh_my_signal_matches() from public, anon;
grant execute on function public.refresh_my_signal_matches() to authenticated;

create or replace function public.get_my_block_log()
returns table (
  id uuid,
  match_type text,
  peer_user_id uuid,
  peer_preview_label text,
  match_strength real,
  match_reasons text[],
  created_at timestamptz,
  expires_at timestamptz,
  notification_sent_to_peer boolean,
  block_id text,
  block_name text
)
language plpgsql
security invoker
set search_path = pg_catalog, public
stable
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  perform public.refresh_my_signal_matches();

  return query
  select
    e.id,
    e.match_type,
    e.peer_user_id,
    coalesce(u.nickname, 'A neighbor on your block') as peer_preview_label,
    e.match_strength,
    e.match_reasons,
    e.created_at,
    e.expires_at,
    e.notification_sent_to_peer,
    e.block_id,
    b.display_name as block_name
  from public.block_log_entries e
  left join public.users u on u.id = e.peer_user_id
  left join public.blocks b on b.id = e.block_id
  where e.for_user_id = auth.uid()
    and e.action_taken is null
    and e.expires_at > now()
  order by e.match_strength desc, e.created_at desc
  limit 20;
end;
$$;
