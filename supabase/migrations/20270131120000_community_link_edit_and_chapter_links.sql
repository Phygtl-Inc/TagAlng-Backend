-- Community link · live check for the edit sheet · chapter links under their parent
--
-- WHY (Asjid, 2026-10-09, approved with the PWA edit-sheet mockup)
--   1. The edit sheet lets an operator change the community's link once. Until now the only
--      way to learn whether a new link would work was to submit it: rename_community_handle
--      raised, and the one rename was the only try. The sheet needs a live check that says
--      exactly what the rename would say — without writing anything.
--   2. A chapter had no link of its own worth sharing. RCC inside SJSU could only be
--      reached through a global handle, competing with every other community for words
--      like "strollerwalk". A chapter's link is now get.lana.help/{parent}/{chapter}: the
--      chapter part is unique WITHIN its parent only, so two communities can each have a
--      strollerwalk chapter. Same rule as communities: one change, and the old one keeps
--      resolving.
--
-- WHAT
--   places.chapter_handle / chapter_handle_renamed_at, unique per parent.
--   place_chapter_handle_aliases: retired chapter links, scoped by parent; RLS on, no
--     grants (everything goes through the security definer functions below).
--   places_assign_chapter_handle (BEFORE trigger): a chapter gets a chapter_handle from its
--     name when it is attached (attach_chapter, a hand-written update, anything that sets
--     parent_place_ref) — compact form first, then dashed, then a numeric suffix — and a
--     collision with a sibling gets the next free suffix. Automatic assignment is not the
--     chapter's one rename. Backfill runs every existing chapter through the same trigger.
--   check_community_link(place, handle): the edit sheet's live check, for a community's
--     global handle or a chapter's handle under its parent.
--   rename_chapter_handle(chapter, handle): the one change of a chapter's link.
--   resolve_chapter_handle(parent, chapter): the public read behind
--     get.lana.help/{parent}/{chapter}; anon + authenticated.
--   community_settings: + chapterHandle, canRenameLink, link, parent.
--
-- AUTH: check and rename take the operator of the place OR, for a chapter, of its parent
-- (is_community_operator: place_managers role 'operator', not removed). A chapter's
-- creator who is not an operator cannot open the settings sheet either, so the two agree.
--
-- A detached chapter keeps its chapter_handle (it is not in the per-parent index while its
-- parent is null). Re-attached to a parent where that word is taken, it gets a suffix.

-- ── 1 · columns ─────────────────────────────────────────────────────────────────

alter table public.places
  add column if not exists chapter_handle text,
  add column if not exists chapter_handle_renamed_at timestamptz;

alter table public.places drop constraint if exists places_chapter_handle_shape;
alter table public.places
  add constraint places_chapter_handle_shape check (
    chapter_handle is null
    or (chapter_handle ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
        and length(chapter_handle) between 3 and 48)
  );

comment on column public.places.chapter_handle is
  'The chapter part of get.lana.help/{parent handle}/{chapter handle}. Unique within '
  'parent_place_ref only. Assigned from the name on attach (places_assign_chapter_handle); '
  'changed once by rename_chapter_handle.';
comment on column public.places.chapter_handle_renamed_at is
  'When the chapter''s one link change was spent. Automatic assignment never sets it.';

-- ── 2 · retired chapter links ───────────────────────────────────────────────────

create table if not exists public.place_chapter_handle_aliases (
  parent_place_ref uuid not null references public.places(id) on delete cascade,
  handle           text not null
                     check (handle ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
                            and length(handle) between 3 and 48),
  place_id         uuid not null references public.places(id) on delete cascade,
  retired_by       uuid references public.users(id) on delete set null,
  created_at       timestamptz not null default now(),
  primary key (parent_place_ref, handle)
);

create index if not exists place_chapter_handle_aliases_place_idx
  on public.place_chapter_handle_aliases(place_id);

comment on table public.place_chapter_handle_aliases is
  'Chapter links a chapter used to have, scoped by parent. resolve_chapter_handle falls '
  'through to here while the chapter is still attached to that parent. Never reissued to '
  'another chapter of the same parent.';

alter table public.place_chapter_handle_aliases enable row level security;
-- Deliberately no policy and no grants (revoke from public alone leaves anon's explicit
-- default grant in place — name each role).
revoke all on table public.place_chapter_handle_aliases from public, anon, authenticated;

-- ── 3 · helpers ─────────────────────────────────────────────────────────────────

-- Why p_handle cannot be a chapter link under p_parent for p_self, or null when it can.
create or replace function public._chapter_handle_unavailable(
  p_parent uuid,
  p_handle text,
  p_self   uuid
)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select case
    when exists (select 1 from public.places c
                  where c.parent_place_ref = p_parent
                    and c.chapter_handle = p_handle
                    and c.id is distinct from p_self) then 'taken'
    when exists (select 1 from public.place_chapter_handle_aliases a
                  where a.parent_place_ref = p_parent
                    and a.handle = p_handle) then 'retired'
  end;
$$;

revoke all on function public._chapter_handle_unavailable(uuid, text, uuid)
  from public, anon, authenticated;

-- The first free chapter link for a name under a parent: compact, dashed, then compact2…
create or replace function public._next_chapter_handle(
  p_parent uuid,
  p_name   text,
  p_self   uuid
)
returns text
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_dash    text := public.normalize_place_handle(p_name);
  v_compact text := replace(coalesce(v_dash, ''), '-', '');
  v_try     text;
  i         int;
begin
  -- A name too short for a link ("AI") still gets one.
  if length(v_compact) < 3 then
    v_compact := v_compact || 'chapter';
    v_dash := null;
  end if;
  v_compact := left(v_compact, 44);
  if v_dash is not null and length(v_dash) > 48 then
    v_dash := null;
  end if;

  foreach v_try in array array[v_compact, v_dash] loop
    if v_try is not null
       and public._chapter_handle_unavailable(p_parent, v_try, p_self) is null then
      return v_try;
    end if;
  end loop;
  for i in 2..9999 loop
    v_try := v_compact || i::text;
    if public._chapter_handle_unavailable(p_parent, v_try, p_self) is null then
      return v_try;
    end if;
  end loop;
  raise exception 'chapter_handle_exhausted';
end;
$$;

revoke all on function public._next_chapter_handle(uuid, text, uuid)
  from public, anon, authenticated;

-- ── 4 · assignment on attach ────────────────────────────────────────────────────

create or replace function public.places_assign_chapter_handle()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if new.parent_place_ref is null then
    return new;
  end if;
  if new.chapter_handle is null then
    new.chapter_handle := public._next_chapter_handle(new.parent_place_ref, new.name, new.id);
    return new;
  end if;
  -- Moving under a (new) parent: keep the word if it is free there, else the next free
  -- one. An explicit change of the word under the same parent (rename_chapter_handle) is
  -- not second-guessed here: its checks and the unique index decide.
  if tg_op = 'INSERT' or new.parent_place_ref is distinct from old.parent_place_ref then
    if public._chapter_handle_unavailable(new.parent_place_ref, new.chapter_handle, new.id)
       is not null then
      new.chapter_handle := public._next_chapter_handle(new.parent_place_ref, new.name, new.id);
    end if;
  end if;
  return new;
end;
$$;

revoke all on function public.places_assign_chapter_handle() from public, anon, authenticated;

drop trigger if exists places_assign_chapter_handle_trg on public.places;
create trigger places_assign_chapter_handle_trg
  before insert or update of parent_place_ref, chapter_handle on public.places
  for each row
  when (new.parent_place_ref is not null)
  execute function public.places_assign_chapter_handle();

-- ── 5 · backfill · oldest chapter first, so it gets the plain word ──────────────

do $$
declare
  r record;
begin
  for r in
    select p.id from public.places p
     where p.parent_place_ref is not null and p.chapter_handle is null
     order by p.created_at asc nulls last, p.id
  loop
    -- Setting it to null fires the trigger, which assigns the next free word.
    update public.places set chapter_handle = null where id = r.id;
  end loop;
end;
$$;

create unique index if not exists places_chapter_handle_parent_uniq
  on public.places(parent_place_ref, chapter_handle)
  where parent_place_ref is not null and chapter_handle is not null;

-- ── 6 · check_community_link · the edit sheet's live check ──────────────────────
--
-- {status, reason?, normalizedHandle?, kind}
--   status: available | unchanged | unavailable | invalid | rename_used | not_operator
--   kind:   chapter when the place is a chapter whose parent has a link, else community
-- Mirrors rename_community_handle / rename_chapter_handle exactly; never writes.

create or replace function public.check_community_link(p_place_id uuid, p_handle text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v        text := public.normalize_place_handle(p_handle);
  v_p      public.places%rowtype;
  v_parent public.places%rowtype;
  v_kind   text := 'community';
  v_why    text;
begin
  select * into v_p from public.places where id = p_place_id;
  if v_p.id is not null and v_p.parent_place_ref is not null then
    select * into v_parent from public.places where id = v_p.parent_place_ref;
    if v_parent.handle is not null then
      v_kind := 'chapter';
    end if;
  end if;

  if v_uid is null or v_p.id is null
     or not (public.is_community_operator(v_p.id, v_uid)
             or (v_parent.id is not null
                 and public.is_community_operator(v_parent.id, v_uid))) then
    return jsonb_build_object('status', 'not_operator', 'kind', v_kind);
  end if;

  if v is null or v !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then
    return jsonb_build_object('status', 'invalid', 'reason', 'malformed',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  if length(v) < 3 then
    return jsonb_build_object('status', 'invalid', 'reason', 'too_short',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  if length(v) > 48 then
    return jsonb_build_object('status', 'invalid', 'reason', 'too_long',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;

  if v_kind = 'chapter' then
    if v = v_p.chapter_handle then
      return jsonb_build_object('status', 'unchanged', 'normalizedHandle', v, 'kind', v_kind);
    end if;
    if v_p.chapter_handle_renamed_at is not null then
      return jsonb_build_object('status', 'rename_used', 'normalizedHandle', v, 'kind', v_kind);
    end if;
    v_why := public._chapter_handle_unavailable(v_parent.id, v, v_p.id);
    if v_why is not null then
      return jsonb_build_object('status', 'unavailable', 'reason', v_why,
                                'normalizedHandle', v, 'kind', v_kind);
    end if;
    return jsonb_build_object('status', 'available', 'normalizedHandle', v, 'kind', v_kind);
  end if;

  -- A community's global handle: rename_community_handle (20270107120000) in order, then
  -- what places_single_token_handle_guard would refuse on its update.
  if v = v_p.handle then
    return jsonb_build_object('status', 'unchanged', 'normalizedHandle', v, 'kind', v_kind);
  end if;
  if v_p.handle_renamed_at is not null then
    return jsonb_build_object('status', 'rename_used', 'normalizedHandle', v, 'kind', v_kind);
  end if;
  v_why := case
    when exists (select 1 from public.protected_handles h
                  where h.normalized_handle = v and h.active) then 'protected'
    when exists (select 1 from public.places p where p.handle = v) then 'taken'
    when exists (select 1 from public.place_handle_reservations r
                  where r.normalized_handle = v
                    and r.status in ('active', 'bound')
                    and r.expires_at > now()
                    and r.user_id is distinct from v_uid
                    and r.place_id is distinct from v_p.id) then 'held'
    when exists (select 1 from public.place_handle_aliases a where a.handle = v) then 'retired'
    when position('-' in v) = 0
     and exists (select 1 from public.users u where u.handle = v) then 'member'
  end;
  if v_why is not null then
    return jsonb_build_object('status', 'unavailable', 'reason', v_why,
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  if position('-' in v) = 0 and not public._place_handle_proven(v_p.id, v) then
    return jsonb_build_object('status', 'invalid', 'reason', 'single_token_requires_identity',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  return jsonb_build_object('status', 'available', 'normalizedHandle', v, 'kind', v_kind);
end;
$$;

revoke all on function public.check_community_link(uuid, text) from public, anon, authenticated;
grant execute on function public.check_community_link(uuid, text) to authenticated, service_role;

comment on function public.check_community_link(uuid, text) is
  'Live check for the community edit sheet''s link field. Dispatches to the chapter link '
  '(within its parent) or the global handle, deciding exactly as rename_chapter_handle / '
  'rename_community_handle would. Never writes.';

-- ── 7 · rename_chapter_handle · once, old one kept alive ────────────────────────

create or replace function public.rename_chapter_handle(
  p_chapter_id uuid,
  p_new_handle text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v_new    text := public.normalize_place_handle(p_new_handle);
  v_ch     public.places%rowtype;
  v_parent public.places%rowtype;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;

  select * into v_ch from public.places where id = p_chapter_id for update;
  if v_ch.id is not null and v_ch.parent_place_ref is not null then
    select * into v_parent from public.places where id = v_ch.parent_place_ref;
  end if;
  if v_ch.id is null
     or not (public.is_community_operator(v_ch.id, v_uid)
             or (v_parent.id is not null
                 and public.is_community_operator(v_parent.id, v_uid))) then
    raise exception 'not_operator';
  end if;
  if v_parent.id is null then
    raise exception 'not_a_chapter';
  end if;
  if v_parent.handle is null then
    raise exception 'parent_has_no_handle';
  end if;
  if v_ch.chapter_handle_renamed_at is not null then
    raise exception 'rename_already_used'
      using hint = 'A chapter may change its link once. Contact support.';
  end if;
  if v_new is null or v_new !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then
    raise exception 'handle_shape' using hint = 'Use letters and numbers, like stroller-walk.';
  end if;
  if length(v_new) < 3 or length(v_new) > 48 then
    raise exception 'handle_length';
  end if;
  if v_new = v_ch.chapter_handle then
    raise exception 'handle_unchanged';
  end if;
  if exists (select 1 from public.places c
              where c.parent_place_ref = v_parent.id
                and c.chapter_handle = v_new
                and c.id <> v_ch.id) then
    raise exception 'handle_taken';
  end if;
  -- Never hand one chapter's retired link to a sibling (or back to itself: one change).
  if exists (select 1 from public.place_chapter_handle_aliases a
              where a.parent_place_ref = v_parent.id and a.handle = v_new) then
    raise exception 'handle_retired_elsewhere';
  end if;

  update public.places
     set chapter_handle = v_new, chapter_handle_renamed_at = now(), updated_at = now()
   where id = v_ch.id;

  if v_ch.chapter_handle is not null then
    insert into public.place_chapter_handle_aliases (parent_place_ref, handle, place_id, retired_by)
    values (v_parent.id, v_ch.chapter_handle, v_ch.id, v_uid)
    on conflict (parent_place_ref, handle) do nothing;
  end if;

  return jsonb_build_object(
    'placeId',               v_ch.id,
    'chapterHandle',         v_new,
    'parentHandle',          v_parent.handle,
    'previousChapterHandle', v_ch.chapter_handle,
    'renamesRemaining',      0);
end;
$$;

revoke all on function public.rename_chapter_handle(uuid, text) from public, anon, authenticated;
grant execute on function public.rename_chapter_handle(uuid, text) to authenticated, service_role;

comment on function public.rename_chapter_handle(uuid, text) is
  'One change of a chapter''s link (get.lana.help/{parent}/{chapter}), by an operator of the '
  'chapter or of its parent. The old chapter link is retired into '
  'place_chapter_handle_aliases and keeps resolving under that parent.';

-- ── 8 · resolve_chapter_handle · get.lana.help/{parent}/{chapter} ───────────────
--
-- The parent resolves exactly as get.lana.help/{parent} does (resolve_place_handle, its
-- aliases included). The chapter: by its current chapter_handle, else a retired one, and
-- only while it is still attached to that parent. Same keys as resolve_place_handle, for
-- the chapter, plus chapterHandle, link and parent. viaAlias is true when EITHER part was
-- an old link, so the client can redirect to `link`. Chapters are reachable whatever their
-- governance state (most are community_started); operatorVerified says which. No member
-- identities.

create or replace function public.resolve_chapter_handle(
  p_parent_handle  text,
  p_chapter_handle text
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_parent_j jsonb := public.resolve_place_handle(p_parent_handle);
  v_parent   uuid;
  v_in       text := public.normalize_place_handle(p_chapter_handle);
  v_ch       record;
  v_alias    boolean := false;
begin
  if v_parent_j is null or v_in is null then
    return null;
  end if;
  v_parent := (v_parent_j->>'placeId')::uuid;

  select c.id, c.handle, c.chapter_handle, c.name, c.place_type, c.zip, c.first_action,
         c.governance_state, c.blurb, c.hq_city, c.is_test
    into v_ch
    from public.places c
   where c.parent_place_ref = v_parent
     and c.chapter_handle = v_in
     and c.governance_state is distinct from 'suspended';

  if v_ch.id is null then
    select c.id, c.handle, c.chapter_handle, c.name, c.place_type, c.zip, c.first_action,
           c.governance_state, c.blurb, c.hq_city, c.is_test
      into v_ch
      from public.place_chapter_handle_aliases a
      join public.places c on c.id = a.place_id
     where a.parent_place_ref = v_parent
       and a.handle = v_in
       and c.parent_place_ref = v_parent
       and c.governance_state is distinct from 'suspended';
    v_alias := v_ch.id is not null;
  end if;

  if v_ch.id is null then
    return null;
  end if;

  return jsonb_build_object(
    'placeId',          v_ch.id,
    'handle',           v_ch.handle,
    'displayName',      v_ch.name,
    'placeType',        v_ch.place_type,
    'zip',              v_ch.zip,
    'firstAction',      v_ch.first_action,
    'governanceState',  v_ch.governance_state,
    'operatorVerified', v_ch.governance_state = 'operator_verified',
    'blurb',            v_ch.blurb,
    'hqCity',           v_ch.hq_city,
    'viaAlias',         v_alias or coalesce((v_parent_j->>'viaAlias')::boolean, false),
    'isTest',           coalesce(v_ch.is_test, false),
    'creator',          null,   -- a creator community is never a chapter (attach_chapter)
    'chapterHandle',    v_ch.chapter_handle,
    'link',             (v_parent_j->>'handle') || '/' || v_ch.chapter_handle,
    'parent',           jsonb_build_object(
                          'placeId',     v_parent,
                          'handle',      v_parent_j->>'handle',
                          'displayName', v_parent_j->>'displayName'));
end;
$$;

revoke all on function public.resolve_chapter_handle(text, text) from public, anon, authenticated;
grant execute on function public.resolve_chapter_handle(text, text)
  to anon, authenticated, service_role;

comment on function public.resolve_chapter_handle(text, text) is
  'Public read behind get.lana.help/{parent}/{chapter}. Parent by resolve_place_handle '
  '(aliases included), chapter by chapter_handle or a retired one under that parent while '
  'still attached. resolve_place_handle''s shape for the chapter + chapterHandle, link, parent.';

-- ── 9 · community_settings · body from 20261231120002, link keys ADDED ──────────

create or replace function public.community_settings(p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v_p      record;
  v_parent_id     uuid;                                            -- ADDED
  v_parent_name   text;                                            -- ADDED
  v_parent_handle text;                                            -- ADDED
  v_parent_members int;                                            -- ADDED
  v_chapter boolean := false;                                      -- ADDED
begin
  if v_uid is null or not public.is_community_operator(p_place_id, v_uid) then
    raise exception 'not_operator';
  end if;

  select p.id, p.name, p.first_action, p.blurb, p.handle,
         p.handle_renamed_at, p.blurb_stale, p.place_type, p.hq_city,
         p.governance_state, p.handle_provisional_until,
         p.parent_place_ref, p.chapter_handle, p.chapter_handle_renamed_at  -- ADDED
    into v_p
    from public.places p where p.id = p_place_id;

  if v_p.id is null then
    raise exception 'place_not_found';
  end if;

  -- ADDED: the parent, for a chapter. memberCount is confirmed members as this operator
  -- may see them (visible_place_members: the count discovery and the roster use).
  if v_p.parent_place_ref is not null then
    select pp.id, pp.name, pp.handle into v_parent_id, v_parent_name, v_parent_handle
      from public.places pp where pp.id = v_p.parent_place_ref;
    select count(distinct vm.user_id)::int into v_parent_members
      from public.visible_place_members(v_uid) vm
     where vm.place_ref = v_p.parent_place_ref;
    v_chapter := v_parent_handle is not null and v_p.chapter_handle is not null;
  end if;

  return jsonb_build_object(
    'placeId',          v_p.id,
    'name',             v_p.name,
    'firstAction',      v_p.first_action,
    'blurb',            v_p.blurb,
    'blurbStale',       v_p.blurb_stale,
    'handle',           v_p.handle,
    'canRenameHandle',  v_p.handle_renamed_at is null,
    'placeType',        v_p.place_type,
    'hqCity',           v_p.hq_city,
    'governanceState',  v_p.governance_state,
    'handleProvisionalUntil', v_p.handle_provisional_until,
    -- ADDED (20270131120000)
    'chapterHandle',    v_p.chapter_handle,
    'canRenameLink',    case when v_chapter then v_p.chapter_handle_renamed_at is null
                             else v_p.handle_renamed_at is null end,
    'link',             case when v_chapter then v_parent_handle || '/' || v_p.chapter_handle
                             else v_p.handle end,
    'parent',           case when v_parent_id is null then null
                             else jsonb_build_object(
                                    'placeId',     v_parent_id,
                                    'name',        v_parent_name,
                                    'handle',      v_parent_handle,
                                    'memberCount', coalesce(v_parent_members, 0)) end);
end;
$$;

revoke all on function public.community_settings(uuid) from public, anon;
grant execute on function public.community_settings(uuid) to authenticated, service_role;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.resolve_chapter_handle(text, text);
--   drop function if exists public.rename_chapter_handle(uuid, text);
--   drop function if exists public.check_community_link(uuid, text);
--   drop trigger if exists places_assign_chapter_handle_trg on public.places;
--   drop function if exists public.places_assign_chapter_handle();
--   drop function if exists public._next_chapter_handle(uuid, text, uuid);
--   drop function if exists public._chapter_handle_unavailable(uuid, text, uuid);
--   drop table if exists public.place_chapter_handle_aliases;
--   drop index if exists public.places_chapter_handle_parent_uniq;
--   alter table public.places drop constraint if exists places_chapter_handle_shape,
--     drop column if exists chapter_handle_renamed_at,
--     drop column if exists chapter_handle;
--   Re-run the community_settings section of 20261231120002 (same signature; the added
--   keys stop appearing). Chapter links stop resolving; global handles are untouched.
-- ============================================================================
