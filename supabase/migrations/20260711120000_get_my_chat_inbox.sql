-- Unified Chats inbox: merge nudges + formal intros into one list so the FE
-- "Chats" screen shows everything (who you nudged, who nudged you, and Lana
-- intros) with proper direction + status — not just the nudges table.
--
-- security definer so the counterparty nickname/avatar resolves under
-- users_select_own RLS; rows are still filtered to where auth.uid() is a party.
-- Nudges: all statuses. Intros: proposed/accepted/declined (expired = timed out,
-- never connected, omitted as noise). `kind` tells the FE which accept/decline
-- RPC to call (accept_nudge vs accept_intro).

create or replace function public.get_my_chat_inbox(p_direction text default 'all')
returns table (
  id uuid,
  kind text,
  other_user_id uuid,
  nickname text,
  avatar_url text,
  created_at timestamptz,
  status text,
  message text,
  shared_dimensions text[],
  expires_at timestamptz,
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
    n.id,
    'nudge'::text as kind,
    case when n.sender_id = v_me then n.recipient_id else n.sender_id end as other_user_id,
    coalesce(nullif(trim(u.nickname), ''), 'A neighbour') as nickname,
    u.profile_photo_url as avatar_url,
    n.sent_at as created_at,
    n.status,
    n.context_message as message,
    '{}'::text[] as shared_dimensions,
    null::timestamptz as expires_at,
    case when n.sender_id = v_me then 'sent' else 'received' end as direction
  from public.nudges n
  join public.users u
    on u.id = case when n.sender_id = v_me then n.recipient_id else n.sender_id end
  where (n.sender_id = v_me or n.recipient_id = v_me)
    and (
      p_direction = 'all'
      or (p_direction = 'sent' and n.sender_id = v_me)
      or (p_direction = 'received' and n.recipient_id = v_me)
    )

  union all

  select
    i.id,
    'intro'::text as kind,
    case when i.initiator_id = v_me then i.candidate_id else i.initiator_id end as other_user_id,
    coalesce(nullif(trim(u2.nickname), ''), 'A neighbour') as nickname,
    u2.profile_photo_url as avatar_url,
    i.created_at,
    i.status,
    i.match_reason as message,
    i.shared_dimensions,
    i.expires_at,
    case when i.initiator_id = v_me then 'sent' else 'received' end as direction
  from public.intros i
  join public.users u2
    on u2.id = case when i.initiator_id = v_me then i.candidate_id else i.initiator_id end
  where (i.initiator_id = v_me or i.candidate_id = v_me)
    and i.status in ('proposed', 'accepted', 'declined')
    and (
      p_direction = 'all'
      or (p_direction = 'sent' and i.initiator_id = v_me)
      or (p_direction = 'received' and i.candidate_id = v_me)
    )

  order by created_at desc;
end;
$$;

comment on function public.get_my_chat_inbox(text) is
  'Unified Chats inbox: nudges (all statuses) + intros (proposed/accepted/declined) for the caller, '
  'with kind, direction and status. Drives the FE Chats screen.';

revoke all on function public.get_my_chat_inbox(text) from public, anon;
grant execute on function public.get_my_chat_inbox(text) to authenticated;
