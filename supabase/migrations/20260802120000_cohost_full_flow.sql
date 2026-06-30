-- Co-host full flow (C-18 / C-CHAT-COHOST-INVITE v0.2.59)
-- ---------------------------------------------------------------------------
-- Builds the receiver-side + management surface on top of the existing co-host
-- foundation (event_cohost_invites, propose_cohost, accept_cohost_invite,
-- events.cohost_id). Single co-host per event ("one at a time").
--
-- What this migration adds:
--   1. Schema: 'revoked' invite status + thread_id link on the invite.
--   2. _users_share_cluster — relaxes the candidate pool from "same block"
--      to "same cluster / nearby block + lane" (mockup shows 2 BLOCKS / SAME LANE).
--   3. get_cohost_candidates — ranked, name-searchable neighbor picker with
--      affinity chips + social-proof stats (meets hosted, contributions).
--   4. propose_cohost — rewritten: cluster gate, single-co-host guard, and it
--      opens a 1:1 chat thread so the invite lands as a "X via Lana" thread and
--      the "Message" button just works.
--   5. accept_cohost_invite — guards cohost_id is null, posts a thread note.
--   6. decline_cohost_invite — receiver's "Decline" button.
--   7. revoke_cohost — host removes the co-host.
--   8. get_my_cohost_invites — enriched read for both sides (radar/inbox + cards).
--   9. Co-host edit/visibility rights: events UPDATE + SELECT RLS, plus the join
--      admin RPCs (get_my_event_requests / decide_event_request) broadened to
--      co-host. Cancel + revoke stay host-only.
-- ---------------------------------------------------------------------------

-- 1. Schema -----------------------------------------------------------------

alter table public.event_cohost_invites
  drop constraint if exists event_cohost_invites_status_check;

alter table public.event_cohost_invites
  add constraint event_cohost_invites_status_check
  check (status in ('proposed', 'accepted', 'declined', 'revoked'));

alter table public.event_cohost_invites
  add column if not exists thread_id uuid references public.chat_threads (id) on delete set null;

-- 2. Candidate-pool helper: same cluster (block + nearby / same lane) --------

create or replace function public._users_share_cluster(p_a uuid, p_b uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1
    from public.users ua
    join public.blocks ba on ba.id = ua.home_block_id
    join public.users ub on ub.id = p_b
    join public.blocks bb on bb.id = ub.home_block_id
    where ua.id = p_a
      and ba.cluster_id = bb.cluster_id
  );
$$;

-- 3. Co-host candidate picker -----------------------------------------------
-- Ranked neighbors in the caller's cluster: same-block first (TOP FIT), then
-- ranked by shared public affinities and hosting track record. Name-searchable.
create or replace function public.get_cohost_candidates(
  p_event_id uuid default null,
  p_search   text default null,
  p_limit    int  default 20
)
returns table (
  candidate_id          uuid,
  nickname              text,
  avatar_url            text,
  block_id              text,
  block_name            text,
  same_block            boolean,
  shared_affinity_count int,
  affinities            text[],
  meets_hosted          int,
  contributions         int,
  suggested_overlap_reason text,
  already_invited       boolean,
  is_current_cohost     boolean
)
language plpgsql
security definer
set search_path = pg_catalog, public
stable
as $$
declare
  v_caller  uuid := auth.uid();
  v_cluster text;
  v_block   text;
  v_search  text := nullif(trim(coalesce(p_search, '')), '');
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  select u.home_block_id, b.cluster_id
    into v_block, v_cluster
  from public.users u
  join public.blocks b on b.id = u.home_block_id
  where u.id = v_caller;

  if v_cluster is null then
    return;
  end if;

  return query
  with caller_concepts as (
    select distinct lower(c.concept) as concept
    from public.user_identity_claims c
    where c.user_id = v_caller
      and c.dismissed_at is null
      and c.disclosure = 'public'
  ),
  cand as (
    select
      u.id,
      coalesce(nullif(trim(u.nickname), ''), 'A neighbor') as nickname,
      u.profile_photo_url,
      u.home_block_id,
      b.display_name,
      (u.home_block_id = v_block) as same_block
    from public.users u
    join public.blocks b on b.id = u.home_block_id
    where b.cluster_id = v_cluster
      and u.id <> v_caller
      and (v_search is null or u.nickname ilike '%' || v_search || '%')
  ),
  scored as (
    select
      cand.id,
      cand.nickname,
      cand.profile_photo_url,
      cand.home_block_id,
      cand.display_name,
      cand.same_block,
      -- shared public affinities with the caller
      coalesce((
        select count(*)::int
        from public.user_identity_claims c
        where c.user_id = cand.id
          and c.dismissed_at is null
          and c.disclosure = 'public'
          and lower(c.concept) in (select concept from caller_concepts)
      ), 0) as shared_cnt,
      -- top public affinity labels for the chips
      coalesce((
        select array_agg(label)
        from (
          select c.label
          from public.user_identity_claims c
          where c.user_id = cand.id
            and c.dismissed_at is null
            and c.disclosure = 'public'
          order by c.confidence desc nulls last
          limit 3
        ) t
      ), '{}'::text[]) as affinities,
      -- social proof
      coalesce((
        select count(*)::int from public.events e
        where e.host_id = cand.id and e.status in ('open', 'completed')
      ), 0) as meets_cnt,
      coalesce((
        select count(*)::int from public.local_signals s
        where s.user_id = cand.id
      ), 0) as contrib_cnt,
      -- prefilled note (>= 10 chars so propose_cohost accepts it as-is)
      (
        case when cand.same_block then 'Same block' else 'Same lane nearby' end
        || coalesce(
             ' — ' || (
               select c.label
               from public.user_identity_claims c
               where c.user_id = cand.id
                 and c.dismissed_at is null
                 and c.disclosure = 'public'
                 and lower(c.concept) in (select concept from caller_concepts)
               order by c.confidence desc nulls last
               limit 1
             ),
             ''
           )
      ) as suggested_reason,
      (
        p_event_id is not null and exists (
          select 1 from public.event_cohost_invites i
          where i.event_id = p_event_id
            and i.candidate_id = cand.id
            and i.status in ('proposed', 'accepted')
        )
      ) as already_inv,
      (
        p_event_id is not null and exists (
          select 1 from public.events e
          where e.id = p_event_id and e.cohost_id = cand.id
        )
      ) as is_cohost
    from cand
  )
  select
    scored.id,
    scored.nickname,
    scored.profile_photo_url,
    scored.home_block_id,
    scored.display_name,
    scored.same_block,
    scored.shared_cnt,
    scored.affinities,
    scored.meets_cnt,
    scored.contrib_cnt,
    scored.suggested_reason,
    scored.already_inv,
    scored.is_cohost
  from scored
  order by scored.same_block desc, scored.shared_cnt desc, scored.meets_cnt desc, scored.nickname
  limit greatest(1, least(coalesce(p_limit, 20), 100));
end;
$$;

-- 4. propose_cohost (rewritten) ---------------------------------------------
-- Cluster gate (not strictly same block), single-co-host guard, opens a 1:1
-- chat thread so the invite arrives as a "X via Lana" thread + action card.
create or replace function public.propose_cohost(
  p_candidate_id   uuid,
  p_overlap_reason text,
  p_event_id       uuid default null,
  p_session_id     uuid default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_host      uuid;
  v_invite_id uuid;
  v_thread    uuid;
  v_title     text;
begin
  perform public._require_verified_neighbor_comms();
  v_host := auth.uid();

  if p_candidate_id is null or p_candidate_id = v_host then
    raise exception 'invalid_candidate' using errcode = 'P0001';
  end if;
  if char_length(trim(p_overlap_reason)) < 10 then
    raise exception 'overlap_reason_too_short' using errcode = 'P0001';
  end if;
  if not public._users_share_cluster(v_host, p_candidate_id) then
    raise exception 'candidate_not_in_cluster' using errcode = 'P0001';
  end if;

  if p_event_id is not null then
    if not exists (
      select 1 from public.events e
      where e.id = p_event_id and e.host_id = v_host
    ) then
      raise exception 'not_event_host' using errcode = 'P0001';
    end if;
    -- One co-host at a time.
    if exists (
      select 1 from public.events e
      where e.id = p_event_id and e.cohost_id is not null
    ) then
      raise exception 'cohost_already_set' using errcode = 'P0001';
    end if;
    if exists (
      select 1 from public.event_cohost_invites i
      where i.event_id = p_event_id and i.status = 'proposed'
    ) then
      raise exception 'cohost_invite_pending' using errcode = 'P0001';
    end if;
  end if;

  -- Open (or reuse) the 1:1 thread so the invite lands in chat and "Message" works.
  v_thread := public._open_relationship_thread(v_host, p_candidate_id);

  insert into public.event_cohost_invites (
    event_id, host_id, candidate_id, overlap_reason, session_id, thread_id
  )
  values (
    p_event_id, v_host, p_candidate_id, trim(p_overlap_reason), p_session_id, v_thread
  )
  returning id into v_invite_id;

  if p_event_id is not null then
    select title into v_title from public.events where id = p_event_id;
  end if;

  if v_thread is not null then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (
      v_thread, null, 'lana',
      'Co-host invite' || coalesce(' for "' || v_title || '"', '') ||
      ' — tap the card above to accept or decline.'
    );
    update public.chat_threads set last_message_at = now() where id = v_thread;
  end if;

  return v_invite_id;
end;
$$;

-- 5. accept_cohost_invite (rewritten) ---------------------------------------
create or replace function public.accept_cohost_invite(p_invite_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_event_id uuid;
  v_host_id  uuid;
  v_thread   uuid;
begin
  perform public._require_verified_neighbor_comms();

  update public.event_cohost_invites i
  set status = 'accepted',
      responded_at = now()
  where i.id = p_invite_id
    and i.candidate_id = auth.uid()
    and i.status = 'proposed'
  returning i.event_id, i.host_id, i.thread_id into v_event_id, v_host_id, v_thread;

  if v_host_id is null then
    raise exception 'cohost_invite_not_found' using errcode = 'P0001';
  end if;

  if v_event_id is not null then
    update public.events e
    set cohost_id = auth.uid(),
        updated_at = now()
    where e.id = v_event_id
      and e.host_id = v_host_id
      and e.cohost_id is null;

    if not found then
      raise exception 'cohost_already_set' using errcode = 'P0001';
    end if;
  end if;

  if v_thread is not null then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_thread, null, 'system', 'Co-host invite accepted — you''re hosting together now.');
    update public.chat_threads set last_message_at = now() where id = v_thread;
  end if;

  return v_event_id;
end;
$$;

-- 6. decline_cohost_invite --------------------------------------------------
create or replace function public.decline_cohost_invite(p_invite_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.event_cohost_invites i
  set status = 'declined',
      responded_at = now()
  where i.id = p_invite_id
    and i.candidate_id = auth.uid()
    and i.status = 'proposed'
  returning i.thread_id into v_thread;

  if not found then
    raise exception 'cohost_invite_not_found' using errcode = 'P0001';
  end if;

  if v_thread is not null then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_thread, null, 'system', 'Co-host invite declined.');
    update public.chat_threads set last_message_at = now() where id = v_thread;
  end if;
end;
$$;

-- 7. revoke_cohost (host only) ----------------------------------------------
create or replace function public.revoke_cohost(p_event_id uuid)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_thread uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  update public.events e
  set cohost_id = null,
      updated_at = now()
  where e.id = p_event_id
    and e.host_id = auth.uid()
    and e.cohost_id is not null;

  if not found then
    raise exception 'event_not_found_or_no_cohost' using errcode = 'P0001';
  end if;

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
end;
$$;

-- 8. get_my_cohost_invites — enriched read for both sides -------------------
create or replace function public.get_my_cohost_invites(p_direction text default 'all')
returns table (
  invite_id      uuid,
  direction      text,
  event_id       uuid,
  event_title    text,
  starts_at      timestamptz,
  venue_name     text,
  venue_address  text,
  host_id        uuid,
  host_name      text,
  host_avatar    text,
  candidate_id   uuid,
  candidate_name text,
  candidate_avatar text,
  overlap_reason text,
  status         text,
  thread_id      uuid,
  created_at     timestamptz,
  responded_at   timestamptz
)
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  select
    i.id,
    case when i.host_id = auth.uid() then 'sent' else 'received' end,
    i.event_id,
    e.title,
    e.starts_at,
    e.venue_name,
    e.venue_address,
    i.host_id,
    coalesce(nullif(trim(hu.nickname), ''), 'A neighbor'),
    hu.profile_photo_url,
    i.candidate_id,
    coalesce(nullif(trim(cu.nickname), ''), 'A neighbor'),
    cu.profile_photo_url,
    i.overlap_reason,
    i.status,
    i.thread_id,
    i.created_at,
    i.responded_at
  from public.event_cohost_invites i
  left join public.events e on e.id = i.event_id
  left join public.users hu on hu.id = i.host_id
  left join public.users cu on cu.id = i.candidate_id
  where (
    (p_direction in ('all', 'received') and i.candidate_id = auth.uid())
    or (p_direction in ('all', 'sent') and i.host_id = auth.uid())
  )
  order by (i.status = 'proposed') desc, i.created_at desc;
$$;

-- 9. Co-host edit + visibility rights ---------------------------------------
-- Co-host may edit event details (update_event relies on RLS) and is visible
-- the event in any status. Cancel/delete + revoke stay host-only.
drop policy if exists "events_update_host_only" on public.events;
create policy "events_update_host_or_cohost"
  on public.events for update
  to authenticated
  using (host_id = auth.uid() or cohost_id = auth.uid())
  with check (host_id = auth.uid() or cohost_id = auth.uid());

drop policy if exists "events_select_cohost_all_status" on public.events;
create policy "events_select_cohost_all_status"
  on public.events for select
  to authenticated
  using (cohost_id = auth.uid());

-- Join-request admin queue: host OR co-host may view + decide.
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
  where (e.host_id = auth.uid() or e.cohost_id = auth.uid())
    and (p_event_id is null or e.id = p_event_id)
    and er.status in ('pending', 'approved')
  order by (er.status = 'pending') desc, er.created_at desc;
$$;

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
      where e.id = er.event_id
        and (e.host_id = auth.uid() or e.cohost_id = auth.uid())
    );

  if not found then
    raise exception 'request_not_found_or_not_host' using errcode = 'P0001';
  end if;
end;
$$;

-- Grants --------------------------------------------------------------------
revoke all on function public._users_share_cluster(uuid, uuid) from public, anon;
grant execute on function public._users_share_cluster(uuid, uuid) to authenticated;

revoke all on function public.get_cohost_candidates(uuid, text, int) from public, anon;
grant execute on function public.get_cohost_candidates(uuid, text, int) to authenticated;

revoke all on function public.propose_cohost(uuid, text, uuid, uuid) from public, anon;
grant execute on function public.propose_cohost(uuid, text, uuid, uuid) to authenticated;

revoke all on function public.accept_cohost_invite(uuid) from public, anon;
grant execute on function public.accept_cohost_invite(uuid) to authenticated;

revoke all on function public.decline_cohost_invite(uuid) from public, anon;
grant execute on function public.decline_cohost_invite(uuid) to authenticated;

revoke all on function public.revoke_cohost(uuid) from public, anon;
grant execute on function public.revoke_cohost(uuid) to authenticated;

revoke all on function public.get_my_cohost_invites(text) from public, anon;
grant execute on function public.get_my_cohost_invites(text) to authenticated;

revoke all on function public.get_my_event_requests(uuid) from public, anon;
grant execute on function public.get_my_event_requests(uuid) to authenticated;

revoke all on function public.decide_event_request(uuid, text) from public, anon;
grant execute on function public.decide_event_request(uuid, text) to authenticated;
