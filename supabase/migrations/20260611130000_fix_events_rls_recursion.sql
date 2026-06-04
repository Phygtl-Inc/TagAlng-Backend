-- Break infinite RLS recursion: events SELECT ↔ event_requests SELECT both reference each other.
-- Host checks use SECURITY DEFINER helper (bypasses RLS on events).

create or replace function public.is_event_host(p_event_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.events e
    where e.id = p_event_id
      and e.host_id = p_user_id
  );
$$;

comment on function public.is_event_host(uuid, uuid) is
  'RLS helper — host check without re-entering events policies.';

grant execute on function public.is_event_host(uuid, uuid) to authenticated;

drop policy if exists "er_select_self_or_host" on public.event_requests;
create policy "er_select_self_or_host"
  on public.event_requests for select
  to authenticated
  using (
    requester_id = auth.uid()
    or public.is_event_host(event_id, auth.uid())
  );

drop policy if exists "er_update_host_or_self_cancel" on public.event_requests;
create policy "er_update_host_or_self_cancel"
  on public.event_requests for update
  to authenticated
  using (
    public.is_event_host(event_id, auth.uid())
    or requester_id = auth.uid()
  )
  with check (
    public.is_event_host(event_id, auth.uid())
    or (requester_id = auth.uid() and status = 'cancelled')
  );

drop policy if exists "events_select_approved_attendee" on public.events;
create policy "events_select_approved_attendee"
  on public.events for select
  to authenticated
  using (
    host_id is distinct from auth.uid()
    and exists (
      select 1
      from public.event_requests r
      where r.event_id = events.id
        and r.requester_id = auth.uid()
        and r.status in ('approved', 'attended')
    )
  );

drop policy if exists "te_select_member" on public.thread_events;
create policy "te_select_member"
  on public.thread_events for select
  to authenticated
  using (
    public.is_event_host(event_id, auth.uid())
    or exists (
      select 1
      from public.event_requests r
      where r.event_id = thread_events.event_id
        and r.requester_id = auth.uid()
        and r.status in ('approved', 'attended')
    )
  );
