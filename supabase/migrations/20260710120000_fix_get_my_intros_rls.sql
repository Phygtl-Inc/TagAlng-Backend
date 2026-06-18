-- get_my_intros joined users for counterparty nickname, but users RLS is select-own-only
-- (users_select_own). security invoker hid every intro row. Run as definer; still filter
-- to intros where auth.uid() is initiator or candidate.

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
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_me uuid := auth.uid();
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  if p_direction not in ('sent', 'received', 'all') then
    raise exception 'invalid_direction' using errcode = 'P0001';
  end if;

  return query
  select
    i.id,
    case when i.initiator_id = v_me then i.candidate_id else i.initiator_id end,
    coalesce(nullif(trim(u.nickname), ''), 'A neighbor') as nickname,
    u.profile_photo_url,
    i.created_at,
    i.expires_at,
    i.status,
    i.match_reason,
    i.shared_dimensions,
    case when i.initiator_id = v_me then 'sent' else 'received' end
  from public.intros i
  join public.users u
    on u.id = case when i.initiator_id = v_me then i.candidate_id else i.initiator_id end
  where i.status = 'proposed'
    and i.expires_at > now()
    and (
      (p_direction = 'sent' and i.initiator_id = v_me)
      or (p_direction = 'received' and i.candidate_id = v_me)
      or (
        p_direction = 'all'
        and (i.initiator_id = v_me or i.candidate_id = v_me)
      )
    )
  order by i.created_at desc;
end;
$$;

comment on function public.get_my_intros(text) is
  'Pending intro inbox: proposed intros not expired. security definer so counterparty nickname resolves under users RLS.';

revoke all on function public.get_my_intros(text) from public, anon;
grant execute on function public.get_my_intros(text) to authenticated;
