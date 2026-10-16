-- Community co-owners — the meet co-host flow, for communities.
--
-- The owner (a live place_managers 'operator', or the starter of a community that is still
-- community_started) invites a member to co-own. The invite lands in their 1:1 chat as a
-- card, exactly like a co-host invite; accepting writes a place_managers row with
-- role = 'manager'. A co-owner edits everything an owner edits. Only the owner adds or
-- removes co-owners; a co-owner may step down themself.
--
-- WHAT CHANGES FOR EXISTING VERBS
--   is_community_operator() now answers "may edit this community?" — it accepts
--   role in ('operator','manager'). Every settings / handle / chapter / HQ verb goes
--   through it (directly or via _community_manage_status / _community_handle_claim_status),
--   so co-owners get edit rights in one place. Nothing that picks THE operator
--   (resolve_place_handle, place_claim_card, activate_handle_on_backlink, the
--   keep-one-operator trigger, places_sync_operator) reads role = 'manager', so the
--   owner stays the owner.
--
-- WHAT IS NEW
--   community_coowner_invites           invite rows (proposed → accepted|declined|revoked)
--   is_community_owner(place, user)     who may add/remove co-owners
--   community_coowners(place)           roster for the edit sheet
--   coowner_candidates(place, search)   the owner's picker: confirmed members
--   propose_coowner(place, candidate)   owner → invite in chat
--   accept_coowner_invite(invite)       candidate → becomes co-owner
--   decline_coowner_invite(invite)      candidate → no
--   remove_coowner(place, user)         owner removes / withdraws; co-owner steps down
--   get_my_coowner_invites(direction)   both sides, for the chat card and badge

-- ── 1 · invites ─────────────────────────────────────────────────────────────

create table if not exists public.community_coowner_invites (
  id            uuid primary key default gen_random_uuid(),
  place_id      uuid not null references public.places (id) on delete cascade,
  owner_id      uuid not null references public.users (id) on delete cascade,
  candidate_id  uuid not null references public.users (id) on delete cascade,
  status        text not null default 'proposed'
                  check (status in ('proposed', 'accepted', 'declined', 'revoked')),
  thread_id     uuid references public.chat_threads (id) on delete set null,
  created_at    timestamptz not null default now(),
  responded_at  timestamptz,
  constraint community_coowner_distinct check (owner_id <> candidate_id)
);

-- One open invite per person per community.
create unique index if not exists community_coowner_invites_one_open
  on public.community_coowner_invites (place_id, candidate_id)
  where status = 'proposed';
create index if not exists community_coowner_invites_candidate_idx
  on public.community_coowner_invites (candidate_id, created_at desc);
create index if not exists community_coowner_invites_owner_idx
  on public.community_coowner_invites (owner_id, created_at desc);

alter table public.community_coowner_invites enable row level security;

drop policy if exists community_coowner_invites_parties on public.community_coowner_invites;
create policy community_coowner_invites_parties on public.community_coowner_invites
  for select to authenticated
  using (owner_id = auth.uid() or candidate_id = auth.uid());

-- Writes go through the RPCs below only.
revoke insert, update, delete on public.community_coowner_invites from anon, authenticated;

-- ── 2 · who may edit, who owns ──────────────────────────────────────────────

create or replace function public.is_community_operator(p_place_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  -- "May edit this community": the owner (operator) or a co-owner (manager).
  select exists (
    select 1 from public.place_managers m
     where m.place_id = p_place_id
       and m.user_id  = p_user_id
       and m.role     in ('operator', 'manager')
       and m.removed_at is null);
$$;

revoke all on function public.is_community_operator(uuid, uuid) from public, anon, authenticated;
grant execute on function public.is_community_operator(uuid, uuid) to service_role;

-- Owner = may add / remove co-owners. Same people _community_manage_status lets manage,
-- minus co-owners.
create or replace function public.is_community_owner(p_place_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select p_user_id is not null and (
    exists (
      select 1 from public.place_managers m
       where m.place_id = p_place_id
         and m.user_id  = p_user_id
         and m.role     = 'operator'
         and m.removed_at is null)
    or exists (
      select 1 from public.places p
       where p.id = p_place_id
         and p.governance_state = 'community_started'
         and p.created_by = p_user_id)
  );
$$;

revoke all on function public.is_community_owner(uuid, uuid) from public, anon, authenticated;
grant execute on function public.is_community_owner(uuid, uuid) to service_role;

create or replace function public._is_coowner(p_place_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1 from public.place_managers m
     where m.place_id = p_place_id
       and m.user_id  = p_user_id
       and m.role     = 'manager'
       and m.removed_at is null);
$$;

revoke all on function public._is_coowner(uuid, uuid) from public, anon, authenticated;

create or replace function public._is_confirmed_member(p_place_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1 from public.circle_affiliations a
     where a.place_ref = p_place_id
       and a.user_id   = p_user_id
       and a.status    = 'confirmed'
       and a.dismissed_at is null);
$$;

revoke all on function public._is_confirmed_member(uuid, uuid) from public, anon, authenticated;

create or replace function public._coowner_person(p_user_id uuid)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'user_id',    u.id,
    'name',       coalesce(nullif(trim(u.nickname), ''), 'A neighbor'),
    'avatar_url', u.profile_photo_url)
  from public.users u
  where u.id = p_user_id;
$$;

revoke all on function public._coowner_person(uuid) from public, anon, authenticated;

create or replace function public._coowner_thread_note(p_thread uuid, p_kind text, p_text text)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if p_thread is null then
    return;
  end if;
  insert into public.messages (thread_id, sender_id, kind, content)
  values (p_thread, null, p_kind, p_text);
  update public.chat_threads set last_message_at = now() where id = p_thread;
end;
$$;

revoke all on function public._coowner_thread_note(uuid, text, text) from public, anon, authenticated;

-- ── 2b · renames work at all ─────────────────────────────────────────────────
-- places_drop_stale_blurb_embedding (20270119120000) compares blurb_embedding with
-- `is not distinct from` but has no search_path of its own, so it runs on its caller's.
-- update_community_settings pins pg_catalog, public — where vector's = operator is not
-- visible — so every name/description save raised "operator does not exist:
-- extensions.vector = extensions.vector". Owners and co-owners alike need this.
alter function public.places_drop_stale_blurb_embedding()
  set search_path = pg_catalog, public, extensions;

-- ── 3 · roster ──────────────────────────────────────────────────────────────
-- Owners, co-owners, and — for the owner only — open invites. Visible to anyone who
-- manages or is a confirmed member; a visitor gets not_member (the roster is members-only).
create or replace function public.community_coowners(p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid   uuid := auth.uid();
  v_owner boolean;
  v_co    boolean;
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if not exists (select 1 from public.places where id = p_place_id) then
    raise exception 'place_not_found' using errcode = 'P0001';
  end if;

  v_owner := public.is_community_owner(p_place_id, v_uid);
  v_co    := public._is_coowner(p_place_id, v_uid);

  if not (v_owner or v_co or public._is_confirmed_member(p_place_id, v_uid)) then
    raise exception 'not_member' using errcode = 'P0001';
  end if;

  return jsonb_build_object(
    'you', case when v_owner then 'owner' when v_co then 'coowner' else 'member' end,
    'owners', coalesce((
      select jsonb_agg(public._coowner_person(o.user_id) order by o.since)
      from (
        select m.user_id, m.created_at as since
        from public.place_managers m
        where m.place_id = p_place_id and m.role = 'operator' and m.removed_at is null
        union
        select p.created_by, p.created_at
        from public.places p
        where p.id = p_place_id
          and p.governance_state = 'community_started'
          and p.created_by is not null
          and not exists (
            select 1 from public.place_managers m
            where m.place_id = p.id and m.role = 'operator' and m.removed_at is null)
      ) o
    ), '[]'::jsonb),
    'coowners', coalesce((
      select jsonb_agg(public._coowner_person(m.user_id) || jsonb_build_object('since', m.created_at)
                       order by m.created_at)
      from public.place_managers m
      where m.place_id = p_place_id and m.role = 'manager' and m.removed_at is null
    ), '[]'::jsonb),
    'pending', case when v_owner then coalesce((
      select jsonb_agg(public._coowner_person(i.candidate_id)
                       || jsonb_build_object('invite_id', i.id,
                                             'thread_id', i.thread_id,
                                             'invited_at', i.created_at)
                       order by i.created_at)
      from public.community_coowner_invites i
      where i.place_id = p_place_id and i.status = 'proposed'
    ), '[]'::jsonb) else '[]'::jsonb end
  );
end;
$$;

revoke all on function public.community_coowners(uuid) from public, anon;
grant execute on function public.community_coowners(uuid) to authenticated;

-- ── 4 · picker ──────────────────────────────────────────────────────────────
-- Confirmed members of the community (as the owner may see them — blocked people are
-- excluded), minus anyone who already runs it.
create or replace function public.coowner_candidates(
  p_place_id uuid,
  p_search   text default null,
  p_limit    int  default 30
)
returns table (
  user_id         uuid,
  nickname        text,
  avatar_url      text,
  member_since    timestamptz,
  already_invited boolean
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v_search text := nullif(trim(coalesce(p_search, '')), '');
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if not public.is_community_owner(p_place_id, v_uid) then
    raise exception 'not_owner' using errcode = 'P0001';
  end if;

  return query
  select
    u.id,
    coalesce(nullif(trim(u.nickname), ''), 'A neighbor'),
    u.profile_photo_url,
    min(a.created_at),
    exists (
      select 1 from public.community_coowner_invites i
      where i.place_id = p_place_id and i.candidate_id = u.id and i.status = 'proposed')
  from public.circle_affiliations a
  join public.users u on u.id = a.user_id
  where a.place_ref = p_place_id
    and a.status = 'confirmed'
    and a.dismissed_at is null
    and a.user_id <> v_uid
    and not public.lana_is_blocked(v_uid, a.user_id)
    and not public.is_community_operator(p_place_id, a.user_id)
    and not public.is_community_owner(p_place_id, a.user_id)
    and (v_search is null or u.nickname ilike '%' || v_search || '%')
  group by u.id, u.nickname, u.profile_photo_url
  order by min(a.created_at), 2
  limit greatest(1, least(coalesce(p_limit, 30), 100));
end;
$$;

revoke all on function public.coowner_candidates(uuid, text, int) from public, anon;
grant execute on function public.coowner_candidates(uuid, text, int) to authenticated;

-- ── 5 · invite ──────────────────────────────────────────────────────────────
create or replace function public.propose_coowner(p_place_id uuid, p_candidate_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_owner  uuid;
  v_name   text;
  v_thread uuid;
  v_id     uuid;
begin
  perform public._require_verified_neighbor_comms();
  v_owner := auth.uid();

  select p.name into v_name from public.places p where p.id = p_place_id;
  if v_name is null then
    raise exception 'place_not_found' using errcode = 'P0001';
  end if;
  if not public.is_community_owner(p_place_id, v_owner) then
    raise exception 'not_owner' using errcode = 'P0001';
  end if;
  if p_candidate_id is null or p_candidate_id = v_owner then
    raise exception 'invalid_candidate' using errcode = 'P0001';
  end if;
  if public.is_community_operator(p_place_id, p_candidate_id)
     or public.is_community_owner(p_place_id, p_candidate_id) then
    raise exception 'already_coowner' using errcode = 'P0001';
  end if;
  if not public._is_confirmed_member(p_place_id, p_candidate_id)
     or public.lana_is_blocked(v_owner, p_candidate_id) then
    raise exception 'not_a_member' using errcode = 'P0001';
  end if;
  if exists (
    select 1 from public.community_coowner_invites i
    where i.place_id = p_place_id and i.candidate_id = p_candidate_id and i.status = 'proposed'
  ) then
    raise exception 'coowner_invite_pending' using errcode = 'P0001';
  end if;

  v_thread := public._open_relationship_thread(v_owner, p_candidate_id);

  insert into public.community_coowner_invites (place_id, owner_id, candidate_id, thread_id)
  values (p_place_id, v_owner, p_candidate_id, v_thread)
  returning id into v_id;

  perform public._coowner_thread_note(
    v_thread, 'lana',
    'Co-owner invite for "' || v_name || '" — tap the card above to accept or decline.');

  return v_id;
end;
$$;

revoke all on function public.propose_coowner(uuid, uuid) from public, anon;
grant execute on function public.propose_coowner(uuid, uuid) to authenticated;

-- ── 6 · accept / decline ────────────────────────────────────────────────────
create or replace function public.accept_coowner_invite(p_invite_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid;
  v_inv    public.community_coowner_invites%rowtype;
begin
  perform public._require_verified_neighbor_comms();
  v_uid := auth.uid();

  select * into v_inv
  from public.community_coowner_invites i
  where i.id = p_invite_id and i.candidate_id = v_uid and i.status = 'proposed'
  for update;

  if v_inv.id is null then
    raise exception 'coowner_invite_not_found' using errcode = 'P0001';
  end if;

  -- The person who invited must still own it; otherwise the invite has lapsed.
  if not public.is_community_owner(v_inv.place_id, v_inv.owner_id) then
    raise exception 'coowner_invite_lapsed' using errcode = 'P0001';
  end if;

  -- Co-owning implies belonging: someone who left since the invite can't accept it.
  if not public._is_confirmed_member(v_inv.place_id, v_uid) then
    raise exception 'not_a_member' using errcode = 'P0001';
  end if;

  update public.community_coowner_invites
     set status = 'accepted', responded_at = now()
   where id = v_inv.id;

  -- Never demote: if they are somehow already the operator, leave the row alone.
  insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
  values (v_inv.place_id, v_uid, 'manager', 'co_owner_invite', now(), v_inv.owner_id)
  on conflict (place_id, user_id) do update
    set role                = case when place_managers.role = 'operator'
                                     and place_managers.removed_at is null
                                   then 'operator' else 'manager' end,
        removed_at          = null,
        verification_method = case when place_managers.role = 'operator'
                                     and place_managers.removed_at is null
                                   then place_managers.verification_method
                                   else 'co_owner_invite' end,
        verified_at         = coalesce(place_managers.verified_at, now()),
        added_by            = coalesce(place_managers.added_by, excluded.added_by);

  perform public._coowner_thread_note(
    v_inv.thread_id, 'system', 'Co-owner invite accepted — you run this community together now.');

  return v_inv.place_id;
end;
$$;

revoke all on function public.accept_coowner_invite(uuid) from public, anon;
grant execute on function public.accept_coowner_invite(uuid) to authenticated;

create or replace function public.decline_coowner_invite(p_invite_id uuid)
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

  update public.community_coowner_invites i
     set status = 'declined', responded_at = now()
   where i.id = p_invite_id
     and i.candidate_id = auth.uid()
     and i.status = 'proposed'
  returning i.thread_id into v_thread;

  if not found then
    raise exception 'coowner_invite_not_found' using errcode = 'P0001';
  end if;

  perform public._coowner_thread_note(v_thread, 'system', 'Co-owner invite declined.');
end;
$$;

revoke all on function public.decline_coowner_invite(uuid) from public, anon;
grant execute on function public.decline_coowner_invite(uuid) to authenticated;

-- ── 7 · remove ──────────────────────────────────────────────────────────────
-- Owner: removes a co-owner, or withdraws an open invite to that person.
-- Co-owner: may remove themself (step down). Owners are never removed here.
create or replace function public.remove_coowner(p_place_id uuid, p_user_id uuid)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v_owner  boolean;
  v_thread uuid;
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  v_owner := public.is_community_owner(p_place_id, v_uid);
  if not v_owner and v_uid is distinct from p_user_id then
    raise exception 'not_owner' using errcode = 'P0001';
  end if;

  update public.place_managers m
     set removed_at = now()
   where m.place_id = p_place_id
     and m.user_id  = p_user_id
     and m.role     = 'manager'
     and m.removed_at is null;

  if found then
    update public.community_coowner_invites i
       set status = 'revoked', responded_at = now()
     where i.place_id = p_place_id
       and i.candidate_id = p_user_id
       and i.status = 'accepted'
    returning i.thread_id into v_thread;

    perform public._coowner_thread_note(
      v_thread, 'system',
      case when v_uid = p_user_id then 'Stepped down as co-owner.'
           else 'Co-owner role was ended by the owner.' end);
    return case when v_uid = p_user_id then 'stepped_down' else 'removed' end;
  end if;

  if v_owner then
    update public.community_coowner_invites i
       set status = 'revoked', responded_at = now()
     where i.place_id = p_place_id
       and i.candidate_id = p_user_id
       and i.status = 'proposed'
    returning i.thread_id into v_thread;

    if found then
      perform public._coowner_thread_note(
        v_thread, 'system', 'Co-owner invite was withdrawn by the owner.');
      return 'withdrawn';
    end if;
  end if;

  raise exception 'not_coowner' using errcode = 'P0001';
end;
$$;

revoke all on function public.remove_coowner(uuid, uuid) from public, anon;
grant execute on function public.remove_coowner(uuid, uuid) to authenticated;

-- ── 8 · my invites (chat card + badge) ──────────────────────────────────────
create or replace function public.get_my_coowner_invites(p_direction text default 'all')
returns table (
  invite_id      uuid,
  direction      text,
  place_id       uuid,
  place_name     text,
  place_handle   text,
  owner_id       uuid,
  owner_name     text,
  owner_avatar   text,
  candidate_id   uuid,
  candidate_name text,
  candidate_avatar text,
  status         text,
  thread_id      uuid,
  member_count   int,
  created_at     timestamptz,
  responded_at   timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid uuid := auth.uid();
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  return query
  select
    i.id,
    case when i.owner_id = v_uid then 'sent' else 'received' end,
    p.id,
    p.name,
    p.handle,
    o.id,
    coalesce(nullif(trim(o.nickname), ''), 'A neighbor'),
    o.profile_photo_url,
    c.id,
    coalesce(nullif(trim(c.nickname), ''), 'A neighbor'),
    c.profile_photo_url,
    i.status,
    i.thread_id,
    (select count(distinct a.user_id)::int
       from public.circle_affiliations a
      where a.place_ref = p.id and a.status = 'confirmed' and a.dismissed_at is null),
    i.created_at,
    i.responded_at
  from public.community_coowner_invites i
  join public.places p on p.id = i.place_id
  join public.users o on o.id = i.owner_id
  join public.users c on c.id = i.candidate_id
  where (i.owner_id = v_uid or i.candidate_id = v_uid)
    and (coalesce(p_direction, 'all') = 'all'
         or (p_direction = 'sent' and i.owner_id = v_uid)
         or (p_direction = 'received' and i.candidate_id = v_uid))
  order by (i.status = 'proposed') desc, i.created_at desc
  limit 100;
end;
$$;

revoke all on function public.get_my_coowner_invites(text) from public, anon;
grant execute on function public.get_my_coowner_invites(text) to authenticated;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.get_my_coowner_invites(text);
--   drop function if exists public.remove_coowner(uuid, uuid);
--   drop function if exists public.decline_coowner_invite(uuid);
--   drop function if exists public.accept_coowner_invite(uuid);
--   drop function if exists public.propose_coowner(uuid, uuid);
--   drop function if exists public.coowner_candidates(uuid, text, int);
--   drop function if exists public.community_coowners(uuid);
--   drop function if exists public._coowner_thread_note(uuid, text, text);
--   drop function if exists public._coowner_person(uuid);
--   drop function if exists public._is_confirmed_member(uuid, uuid);
--   drop function if exists public._is_coowner(uuid, uuid);
--   drop function if exists public.is_community_owner(uuid, uuid);
--   restore is_community_operator from 20261231120002 (role = 'operator' only);
--   update public.place_managers set removed_at = now()
--    where role = 'manager' and verification_method = 'co_owner_invite' and removed_at is null;
--   drop table if exists public.community_coowner_invites;
-- ============================================================================
