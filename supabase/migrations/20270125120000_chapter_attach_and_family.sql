-- Chapters you can make, unmake, and see content across.
--
-- 20261214120000 built chapters (places.parent_place_ref) and wrote down the contract;
-- 20270112120000 and 20270119120000 made them readable and findable. Nothing could WRITE
-- one: RCC is a chapter of San Jose State only because it was attached by hand in SQL, and
-- "create RCC as a club inside SJSU" in chat made a standalone community (2026-10-05).
-- And the content half of the contract — a parent's members see the chapters' meets, a
-- chapter's members see the parent's, nobody sees a sibling's — was never implemented.
--
-- ── 1 · attach_chapter / detach_chapter ─────────────────────────────────────────────
--
-- Product rule (2026-10-07): ANY confirmed member of the parent may attach a community
-- they run to it; the operator or creator of EITHER side may detach. Attaching needs
-- standing on both sides — you cannot claim someone else's club as yours, and you cannot
-- hang a club off a community you are not part of. Approval was rejected for now: SJSU
-- was started by a member and has no operator, so nobody could have approved RCC.
--
-- A chapter must have a point (places_chapter_has_geography). A club made in chat often
-- has none, and a campus club meets on its campus, so a chapter with no point takes its
-- parent's. A creator community never becomes a chapter: it has no geography BY
-- CONSTRAINT (places_creator_has_no_geography) and is a topic, not a local club.
--
-- Depth and cycles stay the trigger's job (places_guard_chapter_depth, 20261216120000):
-- its errors surface here unchanged.
--
-- ── 2 · community_family ────────────────────────────────────────────────────────────
--
-- Which communities' CONTENT a viewer sees when looking at one community — the read side
-- of the 20261214120000 contract, in one place so every content read applies the same
-- rule:
--
--   looking at C (a chapter of P):  C, and P.                      Never a sibling.
--   looking at P (has chapters):    P, plus every chapter if she is a member of P,
--                                   plus only her own chapter(s) otherwise.
--   anything else:                  itself.
--
-- A stranger looking at P gets P alone: the chapter DIRECTORY is public
-- (discover_community_chapters), chapter CONTENT is for the family. Every row says
-- `relation` so a caller can label anything that crossed a boundary.

-- ── 1 ───────────────────────────────────────────────────────────────────────────

create or replace function public.attach_chapter(
  p_user_id uuid,
  p_chapter uuid,
  p_parent  uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_ch   public.places%rowtype;
  v_par  public.places%rowtype;
  v_inherit boolean := false;
begin
  if p_user_id is null or p_chapter is null or p_parent is null then
    raise exception 'chapter_args_required' using errcode = '22023';
  end if;
  if p_chapter = p_parent then
    raise exception 'chapter_cannot_parent_itself' using errcode = '23514';
  end if;

  select * into v_ch  from public.places where id = p_chapter for update;
  if not found then raise exception 'chapter_not_found' using errcode = 'P0002'; end if;
  select * into v_par from public.places where id = p_parent;
  if not found then raise exception 'parent_not_found' using errcode = 'P0002'; end if;

  -- Standing on the chapter: its creator or its operator.
  if v_ch.created_by is distinct from p_user_id
     and not public.is_community_operator(p_chapter, p_user_id) then
    raise exception 'not_your_community' using errcode = '42501';
  end if;
  -- Standing on the parent: a confirmed member, its creator, or its operator.
  if v_par.created_by is distinct from p_user_id
     and not public.is_community_operator(p_parent, p_user_id)
     and not exists (
       select 1 from public.circle_affiliations a
        where a.user_id = p_user_id and a.place_ref = p_parent
          and a.status = 'confirmed' and a.dismissed_at is null) then
    raise exception 'not_a_member_of_parent' using errcode = '42501';
  end if;

  if v_ch.parent_place_ref = p_parent then
    return jsonb_build_object('chapterId', v_ch.id, 'parentId', v_par.id,
                              'parentName', v_par.name, 'alreadyAttached', true,
                              'inheritedLocation', false);
  end if;
  if v_ch.parent_place_ref is not null then
    raise exception 'chapter_has_another_parent' using errcode = '23514';
  end if;

  if v_ch.place_type = 'creator' then
    raise exception 'creator_community_cannot_be_chapter' using errcode = '23514';
  end if;
  if v_par.governance_state = 'suspended' or v_ch.governance_state = 'suspended' then
    raise exception 'community_suspended' using errcode = '42501';
  end if;

  if v_ch.lat is null or v_ch.lng is null then
    if v_par.lat is null or v_par.lng is null then
      raise exception 'chapter_needs_location' using errcode = '23514';
    end if;
    v_inherit := true;
  end if;

  update public.places
     set parent_place_ref = p_parent,
         lat     = case when v_inherit then v_par.lat else lat end,
         lng     = case when v_inherit then v_par.lng else lng end,
         zip     = case when v_inherit then coalesce(zip, v_par.zip) else zip end,
         address = case when v_inherit then coalesce(address, v_par.address) else address end,
         updated_at = now()
   where id = p_chapter;

  return jsonb_build_object('chapterId', v_ch.id, 'parentId', v_par.id,
                            'parentName', v_par.name, 'alreadyAttached', false,
                            'inheritedLocation', v_inherit);
end;
$$;

comment on function public.attach_chapter(uuid, uuid, uuid) is
  'Make p_chapter a chapter of p_parent. Caller must run the chapter (creator/operator) and '
  'belong to the parent (member/creator/operator). A pointless chapter takes the parent''s '
  'point. Creator communities never become chapters. Depth: places_guard_chapter_depth.';

create or replace function public.detach_chapter(
  p_user_id uuid,
  p_chapter uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_ch  public.places%rowtype;
  v_par public.places%rowtype;
begin
  select * into v_ch from public.places where id = p_chapter for update;
  if not found then raise exception 'chapter_not_found' using errcode = 'P0002'; end if;
  if v_ch.parent_place_ref is null then
    return jsonb_build_object('chapterId', v_ch.id, 'wasAttached', false);
  end if;
  select * into v_par from public.places where id = v_ch.parent_place_ref;

  if v_ch.created_by is distinct from p_user_id
     and not public.is_community_operator(v_ch.id, p_user_id)
     and v_par.created_by is distinct from p_user_id
     and not public.is_community_operator(v_par.id, p_user_id) then
    raise exception 'not_your_community' using errcode = '42501';
  end if;

  -- The point it inherited stays: it is still where the club meets.
  update public.places set parent_place_ref = null, updated_at = now() where id = p_chapter;
  return jsonb_build_object('chapterId', v_ch.id, 'wasAttached', true,
                            'parentId', v_par.id, 'parentName', v_par.name);
end;
$$;

comment on function public.detach_chapter(uuid, uuid) is
  'Make a chapter standalone again. The creator/operator of the chapter or of its parent '
  'may do it. Keeps the chapter''s point.';

revoke all on function public.attach_chapter(uuid, uuid, uuid) from public, anon, authenticated;
revoke all on function public.detach_chapter(uuid, uuid)       from public, anon, authenticated;
grant execute on function public.attach_chapter(uuid, uuid, uuid) to service_role;
grant execute on function public.detach_chapter(uuid, uuid)       to service_role;

-- ── 2 ───────────────────────────────────────────────────────────────────────────

create or replace function public.community_family(
  p_user_id  uuid,
  p_place_id uuid
)
returns table (
  place_id   uuid,
  place_name text,
  relation   text      -- 'self' | 'parent' | 'chapter'
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  with me as (
    select a.place_ref
      from public.circle_affiliations a
     where a.user_id = p_user_id
       and a.status = 'confirmed'
       and a.dismissed_at is null
  ),
  self as (
    select p.id, p.name, p.parent_place_ref from public.places p where p.id = p_place_id
  )
  select s.id, s.name, 'self'::text from self s
  union all
  -- Chapter → parent: every viewer of a chapter may see its parent.
  select par.id, par.name, 'parent'::text
    from self s join public.places par on par.id = s.parent_place_ref
   where par.governance_state is distinct from 'suspended'
  union all
  -- Parent → chapters: all of them for a member of the parent, only her own otherwise.
  select c.id, c.name, 'chapter'::text
    from self s
    join public.places c on c.parent_place_ref = s.id
   where s.parent_place_ref is null
     and c.governance_state is distinct from 'suspended'
     and (
       exists (select 1 from me where me.place_ref = s.id)
       or exists (select 1 from me where me.place_ref = c.id)
     );
$$;

comment on function public.community_family(uuid, uuid) is
  'The communities whose CONTENT p_user_id sees when looking at p_place_id: itself, its '
  'parent, and its chapters (all for a parent member, her own otherwise). Never a sibling '
  'chapter. 20261214120000 contract; 20270125120000.';

revoke all on function public.community_family(uuid, uuid) from public, anon, authenticated;
grant execute on function public.community_family(uuid, uuid) to service_role;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.community_family(uuid, uuid);
--   drop function if exists public.detach_chapter(uuid, uuid);
--   drop function if exists public.attach_chapter(uuid, uuid, uuid);
--   Chapters attached through it stay attached; detach them first if that matters.
-- ============================================================================
