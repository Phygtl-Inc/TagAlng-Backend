-- Host approval queue (C-18): list the join requests for events you host (enriched
-- with the requester's name + public affinity labels + same-block flag), and harden
-- decide_event_request so only the event's host can approve/decline.

create or replace function public.get_my_event_requests(p_event_id uuid default null)
returns table (
  request_id uuid,
  event_id uuid,
  event_title text,
  requester_id uuid,
  requester_name text,
  status text,
  message text,
  created_at timestamptz,
  same_block boolean,
  affinities text[]
)
language sql
security definer
set search_path = pg_catalog, public
as $$
  select
    er.id,
    e.id,
    e.title,
    er.requester_id,
    coalesce(nullif(trim(u.nickname), ''), 'A neighbor'),
    er.status,
    er.message,
    er.created_at,
    (u.home_block_id is not null and u.home_block_id = e.block_id),
    coalesce(
      (
        select array_agg(c.label)
        from (
          select label
          from public.user_identity_claims
          where user_id = er.requester_id
            and dismissed_at is null
            and disclosure = 'public'
          order by confidence desc
          limit 4
        ) c
      ),
      '{}'::text[]
    )
  from public.event_requests er
  join public.events e on e.id = er.event_id
  left join public.users u on u.id = er.requester_id
  where e.host_id = auth.uid()
    and (p_event_id is null or e.id = p_event_id)
    and er.status in ('pending', 'approved')
  order by (er.status = 'pending') desc, er.created_at desc;
$$;

revoke execute on function public.get_my_event_requests(uuid) from public;
grant execute on function public.get_my_event_requests(uuid) to authenticated;

-- Harden: only the host of the request's event may decide it.
create or replace function public.decide_event_request(
  p_request_id uuid,
  p_decision text
)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if p_decision not in ('approved', 'declined') then
    raise exception 'invalid_decision' using errcode = 'P0001';
  end if;

  update public.event_requests er
  set status = p_decision,
      decided_at = now()
  where er.id = p_request_id
    and exists (
      select 1 from public.events e
      where e.id = er.event_id and e.host_id = auth.uid()
    );

  if not found then
    raise exception 'request_not_found_or_not_host' using errcode = 'P0001';
  end if;
end;
$$;
