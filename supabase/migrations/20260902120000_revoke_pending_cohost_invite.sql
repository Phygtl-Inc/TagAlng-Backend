-- Revoke pending co-host invites (host-side cancel)
-- ---------------------------------------------------------------------------
-- Bug: revoke_cohost(p_event_id) only handled an *accepted* co-host — it was
-- guarded on events.cohost_id is not null, which is only set on acceptance.
-- A still-pending invite (event_cohost_invites.status = 'proposed') therefore
-- failed with event_not_found_or_no_cohost ("There is no co-host to remove"),
-- and since decline_cohost_invite is candidate-only and direct writes to
-- event_cohost_invites are blocked by RLS, the host had no way to cancel it.
--
-- Fix: revoke_cohost now also withdraws a proposed invite for the calling
-- host — status = 'revoked', responded_at = now(), plus a system line in the
-- invite's chat thread, mirroring the accepted-revoke behavior. The accepted
-- path is unchanged. Non-host callers now get not_event_host (consistent with
-- the other co-host RPCs) instead of the generic not-found error.
-- ---------------------------------------------------------------------------

create or replace function public.revoke_cohost(p_event_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread      uuid;
  v_had_cohost  boolean;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  -- Host-only, enforced server-side like propose_cohost.
  if not exists (
    select 1 from public.events e
    where e.id = p_event_id and e.host_id = auth.uid()
  ) then
    raise exception 'not_event_host' using errcode = 'P0001';
  end if;

  -- Accepted co-host: clear the event link (unchanged behavior).
  update public.events e
  set cohost_id = null,
      updated_at = now()
  where e.id = p_event_id
    and e.host_id = auth.uid()
    and e.cohost_id is not null;
  v_had_cohost := found;

  if v_had_cohost then
    -- Mark the accepted invite revoked + note the thread.
    update public.event_cohost_invites i
    set status = 'revoked',
        responded_at = now()
    where i.event_id = p_event_id
      and i.host_id = auth.uid()
      and i.status = 'accepted'
    returning i.thread_id into v_thread;

    if v_thread is not null then
      insert into public.messages (thread_id, sender_id, kind, content)
      values (v_thread, null, 'system', 'Co-host role was ended by the host.');
      update public.chat_threads set last_message_at = now() where id = v_thread;
    end if;
  else
    -- No accepted co-host: withdraw a still-pending invite instead.
    -- (propose_cohost guarantees at most one 'proposed' invite per event.)
    update public.event_cohost_invites i
    set status = 'revoked',
        responded_at = now()
    where i.event_id = p_event_id
      and i.host_id = auth.uid()
      and i.status = 'proposed'
    returning i.thread_id into v_thread;

    if not found then
      raise exception 'event_not_found_or_no_cohost' using errcode = 'P0001';
    end if;

    if v_thread is not null then
      insert into public.messages (thread_id, sender_id, kind, content)
      values (v_thread, null, 'system', 'Co-host invite was withdrawn by the host.');
      update public.chat_threads set last_message_at = now() where id = v_thread;
    end if;
  end if;
end;
$$;

revoke all on function public.revoke_cohost(uuid) from public, anon;
grant execute on function public.revoke_cohost(uuid) to authenticated;
