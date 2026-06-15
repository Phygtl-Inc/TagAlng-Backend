-- Pending intro inbox: mirror get_my_nudges for formal intros (status proposed, not expired).

create or replace function public.get_my_intros(p_direction text default 'all')
returns table (
  id uuid,
  other_user_id uuid,
  nickname text,
  avatar_url text,
  created_at timestamptz,
  expires_at timestamptz,
  status text,
  match_reason text,
  shared_dimensions text[],
  direction text
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

  if p_direction not in ('sent', 'received', 'all') then
    raise exception 'invalid_direction' using errcode = 'P0001';
  end if;

  return query
  select
    i.id,
    case when i.initiator_id = auth.uid() then i.candidate_id else i.initiator_id end,
    u.nickname,
    u.profile_photo_url,
    i.created_at,
    i.expires_at,
    i.status,
    i.match_reason,
    i.shared_dimensions,
    case when i.initiator_id = auth.uid() then 'sent' else 'received' end
  from public.intros i
  join public.users u
    on u.id = case when i.initiator_id = auth.uid() then i.candidate_id else i.initiator_id end
  where i.status = 'proposed'
    and i.expires_at > now()
    and (
      (p_direction = 'sent' and i.initiator_id = auth.uid())
      or (p_direction = 'received' and i.candidate_id = auth.uid())
      or (
        p_direction = 'all'
        and (i.initiator_id = auth.uid() or i.candidate_id = auth.uid())
      )
    )
  order by i.created_at desc;
end;
$$;

comment on function public.get_my_intros(text) is
  'Pending intro inbox for authenticated user: proposed intros not yet expired.';

revoke all on function public.get_my_intros(text) from public, anon;
grant execute on function public.get_my_intros(text) to authenticated;
