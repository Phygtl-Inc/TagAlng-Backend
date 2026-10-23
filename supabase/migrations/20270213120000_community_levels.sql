-- Community levels — each community names and orders its own levels, and a document says
-- the lowest level that may read it.
--
-- RCC (2026-10-19 questionnaire, Q11): "Executive Board → Leads → Ambassadors. Ambassadors
-- cannot see the Leads folder, Leads cannot see the Executive Board folder." Q16: any member
-- should be able to add documents. Q15: wrong answers without visibility break trust.
--
-- The ladder, lowest first:
--   visitor (-1)  not a member — the description and public documents
--   member   (0)  every confirmed member — fixed, every community has it
--   custom (1–N)  named and ordered by the owner ("Ambassador" < "Lead" < "E-board")
--   owner (1000)  the owner and co-owners — fixed, always on top
-- One level per member. A document is readable when the asker's rank ≥ the document's rank,
-- and that comparison happens here, in SQL — the worker passes Lana only what
-- community_docs_readable() returns, so a members-only line never reaches a visitor's turn.
--
-- Uploads: any member may upload, at their own rank or below. Managers' and approvers'
-- uploads go live at once; everyone else's wait as 'pending' until an approver publishes
-- them. Pending documents are never answered from.
--
-- WHAT IS NEW
--   community_levels                        the community's named levels (rank 1..10)
--   community_member_levels                 one level per member (absent = plain member)
--   community_doc_policy                    which level approves uploads / reads Lana's log
--   community_documents.min_level_id        a custom level, with visibility 'members'
--   community_documents.visibility 'owners' owner and co-owners only
--   community_documents.review_status       approved | pending | rejected
--   community_doc_answers.reply / sources   what Lana actually said, for the log
--   community_member_rank / community_doc_rank / community_docs_access /
--   community_docs_readable                 worker-side (service role)
--   community_levels / community_level_add / _rename / _delete / community_levels_reorder /
--   community_level_members / community_member_set_level / community_doc_policy_set
--                                           the owner's settings screen (authenticated)

-- ── 1 · levels ──────────────────────────────────────────────────────────────

create table if not exists public.community_levels (
  id          uuid primary key default gen_random_uuid(),
  place_id    uuid not null references public.places(id) on delete cascade,
  name        text not null check (length(btrim(name)) between 1 and 40),
  rank        integer not null check (rank between 1 and 10),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  -- Deferred so a reorder can rewrite every rank in one statement.
  constraint community_levels_rank_unique unique (place_id, rank) deferrable initially deferred
);

create unique index if not exists community_levels_name_unique
  on public.community_levels (place_id, lower(btrim(name)));

alter table public.community_levels enable row level security;
revoke all on public.community_levels from anon, authenticated;

-- Absent row = plain member (rank 0). The row survives leaving the community but counts
-- for nothing while the person is not a confirmed member.
create table if not exists public.community_member_levels (
  place_id  uuid not null references public.places(id) on delete cascade,
  user_id   uuid not null references public.users(id) on delete cascade,
  level_id  uuid not null references public.community_levels(id) on delete cascade,
  set_by    uuid references public.users(id) on delete set null,
  set_at    timestamptz not null default now(),
  primary key (place_id, user_id)
);

create index if not exists community_member_levels_level_idx
  on public.community_member_levels (level_id);

alter table public.community_member_levels enable row level security;
revoke all on public.community_member_levels from anon, authenticated;

-- Null = owner and co-owners only. A deleted level falls back to that, never to "anyone".
create table if not exists public.community_doc_policy (
  place_id           uuid primary key references public.places(id) on delete cascade,
  approver_level_id  uuid references public.community_levels(id) on delete set null,
  log_level_id       uuid references public.community_levels(id) on delete set null,
  updated_at         timestamptz not null default now()
);

alter table public.community_doc_policy enable row level security;
revoke all on public.community_doc_policy from anon, authenticated;

-- ── 2 · documents learn levels and review ───────────────────────────────────

alter table public.community_documents
  drop constraint if exists community_documents_visibility_check;
alter table public.community_documents
  add constraint community_documents_visibility_check
  check (visibility in ('public', 'members', 'owners'));

-- No ON DELETE action: community_level_delete moves a level's documents up first, and a
-- stray delete of a level that still guards documents fails instead of opening them.
alter table public.community_documents
  add column if not exists min_level_id uuid references public.community_levels(id);

alter table public.community_documents
  drop constraint if exists community_documents_level_needs_members;
alter table public.community_documents
  add constraint community_documents_level_needs_members
  check (min_level_id is null or visibility = 'members');

alter table public.community_documents
  add column if not exists review_status text not null default 'approved';
alter table public.community_documents
  drop constraint if exists community_documents_review_status_check;
alter table public.community_documents
  add constraint community_documents_review_status_check
  check (review_status in ('approved', 'pending', 'rejected'));
alter table public.community_documents
  add column if not exists reviewed_by uuid references auth.users(id) on delete set null;
alter table public.community_documents
  add column if not exists reviewed_at timestamptz;

create index if not exists community_documents_min_level_idx
  on public.community_documents (min_level_id) where min_level_id is not null;

alter table public.community_doc_answers add column if not exists reply text;
alter table public.community_doc_answers
  add column if not exists sources jsonb not null default '[]'::jsonb;

-- ── 3 · ranks (worker side) ─────────────────────────────────────────────────

create or replace function public.community_member_rank(p_place_id uuid, p_user_id uuid)
returns integer
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when p_user_id is null then -1
    when public._community_manage_status(p_user_id, p_place_id) is null then 1000
    when public._is_confirmed_member(p_place_id, p_user_id) then coalesce(
      (select l.rank
         from public.community_member_levels ml
         join public.community_levels l on l.id = ml.level_id
        where ml.place_id = p_place_id and ml.user_id = p_user_id),
      0)
    else -1
  end;
$$;

revoke all on function public.community_member_rank(uuid, uuid) from public, anon, authenticated;
grant execute on function public.community_member_rank(uuid, uuid) to service_role;

-- A level that has gone missing reads as owners-only: the safe direction.
create or replace function public.community_doc_rank(p_visibility text, p_min_level_id uuid)
returns integer
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when p_visibility = 'public' then -1
    when p_visibility = 'owners' then 1000
    when p_min_level_id is null then 0
    else coalesce((select rank from public.community_levels where id = p_min_level_id), 1000)
  end;
$$;

revoke all on function public.community_doc_rank(text, uuid) from public, anon, authenticated;
grant execute on function public.community_doc_rank(text, uuid) to service_role;

create or replace function public._community_policy_rank(p_place_id uuid, p_which text)
returns integer
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select coalesce(
    (select l.rank
       from public.community_doc_policy p
       join public.community_levels l
         on l.id = case when p_which = 'approve' then p.approver_level_id else p.log_level_id end
      where p.place_id = p_place_id),
    1000);
$$;

revoke all on function public._community_policy_rank(uuid, text) from public, anon, authenticated;

-- Everything the worker needs to decide what a person may do with documents, in one read.
create or replace function public.community_docs_access(p_place_id uuid, p_user_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_rank       integer;
  v_level_id   uuid;
  v_level_name text;
begin
  if not exists (select 1 from public.places where id = p_place_id) then
    return jsonb_build_object('role', 'place_not_found');
  end if;
  v_rank := public.community_member_rank(p_place_id, p_user_id);
  if v_rank between 1 and 999 then
    select l.id, l.name into v_level_id, v_level_name
      from public.community_member_levels ml
      join public.community_levels l on l.id = ml.level_id
     where ml.place_id = p_place_id and ml.user_id = p_user_id;
  end if;
  return jsonb_build_object(
    'role', case when v_rank >= 1000 then 'manager' when v_rank >= 0 then 'member' else 'visitor' end,
    'rank', v_rank,
    'level_id', v_level_id,
    'level_name', v_level_name,
    'can_upload', v_rank >= 0,
    'can_approve', v_rank >= public._community_policy_rank(p_place_id, 'approve'),
    'can_read_log', v_rank >= public._community_policy_rank(p_place_id, 'log'));
end;
$$;

revoke all on function public.community_docs_access(uuid, uuid) from public, anon, authenticated;
grant execute on function public.community_docs_access(uuid, uuid) to service_role;

-- The ONLY list of documents Lana may answer this person from.
create or replace function public.community_docs_readable(p_place_id uuid, p_user_id uuid)
returns setof uuid
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select d.id
    from public.community_documents d
   where d.place_id = p_place_id
     and d.review_status = 'approved'
     and public.community_doc_rank(d.visibility, d.min_level_id)
         <= public.community_member_rank(p_place_id, p_user_id);
$$;

revoke all on function public.community_docs_readable(uuid, uuid) from public, anon, authenticated;
grant execute on function public.community_docs_readable(uuid, uuid) to service_role;

-- ── 4 · the owner's settings screen (authenticated) ─────────────────────────

create or replace function public._community_levels_require_manager(p_place_id uuid)
returns uuid
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
  if not exists (select 1 from public.places where id = p_place_id) then
    raise exception 'place_not_found' using errcode = 'P0001';
  end if;
  if public._community_manage_status(v_uid, p_place_id) is not null then
    raise exception 'not_manager' using errcode = 'P0001';
  end if;
  return v_uid;
end;
$$;

revoke all on function public._community_levels_require_manager(uuid) from public, anon, authenticated;

-- Members read the ladder too: their upload picker offers their own level and below.
create or replace function public.community_levels(p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v_rank   integer;
  v_manage boolean;
  v_approver uuid;
  v_log      uuid;
begin
  if v_uid is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  v_rank := public.community_member_rank(p_place_id, v_uid);
  if v_rank < 0 then
    raise exception 'not_member' using errcode = 'P0001';
  end if;
  v_manage := v_rank >= 1000;
  select approver_level_id, log_level_id into v_approver, v_log
    from public.community_doc_policy where place_id = p_place_id;

  return jsonb_build_object(
    'you', case when v_manage then 'manager' else 'member' end,
    'your_rank', v_rank,
    'your_level_id', (select level_id from public.community_member_levels
                       where place_id = p_place_id and user_id = v_uid),
    'can_manage', v_manage,
    'approver_level_id', v_approver,
    'log_level_id', v_log,
    'member_count', case when v_manage then (
      select count(distinct a.user_id) from public.circle_affiliations a
       where a.place_ref = p_place_id and a.status = 'confirmed' and a.dismissed_at is null
         and not public.is_community_operator(p_place_id, a.user_id)
         and not exists (select 1 from public.community_member_levels ml
                          where ml.place_id = p_place_id and ml.user_id = a.user_id)) end,
    'member_doc_count', case when v_manage then (
      select count(*) from public.community_documents d
       where d.place_id = p_place_id and d.visibility = 'members' and d.min_level_id is null) end,
    'levels', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', l.id,
               'name', l.name,
               'rank', l.rank,
               'member_count', case when v_manage then (
                 select count(*) from public.community_member_levels ml
                  where ml.level_id = l.id
                    and public._is_confirmed_member(p_place_id, ml.user_id)) end,
               'doc_count', case when v_manage then (
                 select count(*) from public.community_documents d where d.min_level_id = l.id) end)
             order by l.rank)
        from public.community_levels l
       where l.place_id = p_place_id),
      '[]'::jsonb));
end;
$$;

revoke all on function public.community_levels(uuid) from public, anon;
grant execute on function public.community_levels(uuid) to authenticated;

-- A new level lands just above Member; the owner moves it from there.
create or replace function public.community_level_add(p_place_id uuid, p_name text)
returns uuid
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_name text := btrim(coalesce(p_name, ''));
  v_id   uuid;
begin
  perform public._community_levels_require_manager(p_place_id);
  if length(v_name) not between 1 and 40 then
    raise exception 'invalid_name' using errcode = 'P0001';
  end if;
  if lower(v_name) in ('member', 'members', 'owner', 'owners', 'visitor', 'visitors', 'public') then
    raise exception 'reserved_name' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.community_levels
              where place_id = p_place_id and lower(btrim(name)) = lower(v_name)) then
    raise exception 'name_taken' using errcode = 'P0001';
  end if;
  if (select count(*) from public.community_levels where place_id = p_place_id) >= 10 then
    raise exception 'too_many_levels' using errcode = 'P0001';
  end if;
  update public.community_levels set rank = rank + 1, updated_at = now()
   where place_id = p_place_id;
  insert into public.community_levels (place_id, name, rank)
  values (p_place_id, v_name, 1)
  returning id into v_id;
  return v_id;
end;
$$;

revoke all on function public.community_level_add(uuid, text) from public, anon;
grant execute on function public.community_level_add(uuid, text) to authenticated;

create or replace function public.community_level_rename(p_level_id uuid, p_name text)
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_place uuid;
  v_name  text := btrim(coalesce(p_name, ''));
begin
  select place_id into v_place from public.community_levels where id = p_level_id;
  if v_place is null then
    raise exception 'level_not_found' using errcode = 'P0001';
  end if;
  perform public._community_levels_require_manager(v_place);
  if length(v_name) not between 1 and 40 then
    raise exception 'invalid_name' using errcode = 'P0001';
  end if;
  if lower(v_name) in ('member', 'members', 'owner', 'owners', 'visitor', 'visitors', 'public') then
    raise exception 'reserved_name' using errcode = 'P0001';
  end if;
  if exists (select 1 from public.community_levels
              where place_id = v_place and id <> p_level_id
                and lower(btrim(name)) = lower(v_name)) then
    raise exception 'name_taken' using errcode = 'P0001';
  end if;
  update public.community_levels set name = v_name, updated_at = now() where id = p_level_id;
end;
$$;

revoke all on function public.community_level_rename(uuid, text) from public, anon;
grant execute on function public.community_level_rename(uuid, text) to authenticated;

-- p_level_ids lowest first, and exactly the community's levels.
create or replace function public.community_levels_reorder(p_place_id uuid, p_level_ids uuid[])
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_have uuid[];
begin
  perform public._community_levels_require_manager(p_place_id);
  select coalesce(array_agg(id order by id), '{}') into v_have
    from public.community_levels where place_id = p_place_id;
  if coalesce(array_length(p_level_ids, 1), 0) <> coalesce(array_length(v_have, 1), 0)
     or (select coalesce(array_agg(x order by x), '{}') from unnest(p_level_ids) x) <> v_have then
    raise exception 'levels_mismatch' using errcode = 'P0001';
  end if;
  update public.community_levels l
     set rank = o.ord, updated_at = now()
    from unnest(p_level_ids) with ordinality as o(id, ord)
   where l.id = o.id and l.rank <> o.ord;
end;
$$;

revoke all on function public.community_levels_reorder(uuid, uuid[]) from public, anon;
grant execute on function public.community_levels_reorder(uuid, uuid[]) to authenticated;

-- Deleting never widens access: its members drop to the level below (or plain Member), its
-- documents and its approve / log rights move to the level above (or owners only).
create or replace function public.community_level_delete(p_level_id uuid)
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_place uuid;
  v_rank  integer;
  v_below uuid;
  v_above uuid;
begin
  select place_id, rank into v_place, v_rank from public.community_levels where id = p_level_id;
  if v_place is null then
    raise exception 'level_not_found' using errcode = 'P0001';
  end if;
  perform public._community_levels_require_manager(v_place);

  select id into v_below from public.community_levels
   where place_id = v_place and rank < v_rank order by rank desc limit 1;
  select id into v_above from public.community_levels
   where place_id = v_place and rank > v_rank order by rank asc limit 1;

  if v_below is null then
    delete from public.community_member_levels where level_id = p_level_id;
  else
    update public.community_member_levels set level_id = v_below, set_at = now()
     where level_id = p_level_id;
  end if;

  if v_above is null then
    update public.community_documents
       set visibility = 'owners', min_level_id = null, updated_at = now()
     where min_level_id = p_level_id;
  else
    update public.community_documents set min_level_id = v_above, updated_at = now()
     where min_level_id = p_level_id;
  end if;

  update public.community_doc_policy
     set approver_level_id = case when approver_level_id = p_level_id then v_above else approver_level_id end,
         log_level_id      = case when log_level_id = p_level_id then v_above else log_level_id end,
         updated_at        = now()
   where place_id = v_place;

  delete from public.community_levels where id = p_level_id;
  -- Close the gap so ranks stay 1..N.
  update public.community_levels set rank = rank - 1, updated_at = now()
   where place_id = v_place and rank > v_rank;
end;
$$;

revoke all on function public.community_level_delete(uuid) from public, anon;
grant execute on function public.community_level_delete(uuid) to authenticated;

-- Confirmed members with their level. Owner and co-owners are the top tier and not listed.
create or replace function public.community_level_members(
  p_place_id uuid,
  p_search   text default null,
  p_limit    int  default 100
)
returns table (
  user_id    uuid,
  nickname   text,
  avatar_url text,
  level_id   uuid
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid;
  v_search text := nullif(btrim(coalesce(p_search, '')), '');
begin
  v_uid := public._community_levels_require_manager(p_place_id);
  return query
  select u.id,
         coalesce(nullif(btrim(u.nickname), ''), 'A neighbor'),
         u.profile_photo_url,
         ml.level_id
    from (select distinct a.user_id
            from public.circle_affiliations a
           where a.place_ref = p_place_id
             and a.status = 'confirmed'
             and a.dismissed_at is null) m
    join public.users u on u.id = m.user_id
    left join public.community_member_levels ml
      on ml.place_id = p_place_id and ml.user_id = m.user_id
   where not public.is_community_operator(p_place_id, m.user_id)
     and not public.is_community_owner(p_place_id, m.user_id)
     and not public.lana_is_blocked(v_uid, m.user_id)
     and (v_search is null or u.nickname ilike '%' || v_search || '%')
   order by coalesce((select l.rank from public.community_levels l where l.id = ml.level_id), 0) desc,
            2
   limit greatest(1, least(coalesce(p_limit, 100), 300));
end;
$$;

revoke all on function public.community_level_members(uuid, text, int) from public, anon;
grant execute on function public.community_level_members(uuid, text, int) to authenticated;

-- p_level_id null = back to plain Member.
create or replace function public.community_member_set_level(
  p_place_id uuid,
  p_user_id  uuid,
  p_level_id uuid
)
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid uuid;
begin
  v_uid := public._community_levels_require_manager(p_place_id);
  if not public._is_confirmed_member(p_place_id, p_user_id)
     or public.is_community_operator(p_place_id, p_user_id)
     or public.is_community_owner(p_place_id, p_user_id) then
    raise exception 'not_a_member' using errcode = 'P0001';
  end if;
  if p_level_id is null then
    delete from public.community_member_levels
     where place_id = p_place_id and user_id = p_user_id;
    return;
  end if;
  if not exists (select 1 from public.community_levels
                  where id = p_level_id and place_id = p_place_id) then
    raise exception 'level_not_found' using errcode = 'P0001';
  end if;
  insert into public.community_member_levels (place_id, user_id, level_id, set_by)
  values (p_place_id, p_user_id, p_level_id, v_uid)
  on conflict (place_id, user_id)
  do update set level_id = excluded.level_id, set_by = excluded.set_by, set_at = now();
end;
$$;

revoke all on function public.community_member_set_level(uuid, uuid, uuid) from public, anon;
grant execute on function public.community_member_set_level(uuid, uuid, uuid) to authenticated;

-- Null = owner and co-owners only.
create or replace function public.community_doc_policy_set(
  p_place_id          uuid,
  p_approver_level_id uuid,
  p_log_level_id      uuid
)
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
begin
  perform public._community_levels_require_manager(p_place_id);
  if (p_approver_level_id is not null and not exists (
        select 1 from public.community_levels where id = p_approver_level_id and place_id = p_place_id))
     or (p_log_level_id is not null and not exists (
        select 1 from public.community_levels where id = p_log_level_id and place_id = p_place_id)) then
    raise exception 'level_not_found' using errcode = 'P0001';
  end if;
  insert into public.community_doc_policy (place_id, approver_level_id, log_level_id)
  values (p_place_id, p_approver_level_id, p_log_level_id)
  on conflict (place_id)
  do update set approver_level_id = excluded.approver_level_id,
                log_level_id      = excluded.log_level_id,
                updated_at        = now();
end;
$$;

revoke all on function public.community_doc_policy_set(uuid, uuid, uuid) from public, anon;
grant execute on function public.community_doc_policy_set(uuid, uuid, uuid) to authenticated;
