-- get_my_block_log was STABLE but called refresh_my_signal_matches() (INSERTs) → 25006.
-- Keep reads in STABLE get_my_block_log; workers call refresh_my_signal_matches() first.

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

comment on function public.get_my_block_log() is
  'Read-only pending block log rows for caller. Call refresh_my_signal_matches() first if matches may be stale.';
