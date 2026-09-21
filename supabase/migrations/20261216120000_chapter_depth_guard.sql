-- Chapters are ONE level. Enforced here, because a CHECK cannot do it.
--
-- 20261214120000 ships places_chapter_not_self:
--
--     check (parent_place_ref is null or parent_place_ref <> id)
--
-- and that is all a row-level CHECK can ever be: it cannot contain a subquery and cannot
-- see another row, so it forbids exactly A -> A and nothing else. A -> B -> C passes, and
-- so does the A -> B, B -> A cycle. The constraint was originally named
-- places_chapter_not_nested, which promised the guarantee it could not give — the rename
-- was step one, this trigger is step two.
--
-- Why it matters: the visibility contract in 20261214120000 is a UNION of a community and
-- its chapters. Run that over a chain and it recurses — which is how "Boston sees Orlando's
-- content" ships. The read side is not written yet, and it will be written trusting that
-- depth is bounded. Bound it before anything can write parent_place_ref, not after.
--
-- Two rules, which together also close cycles:
--   1. A chapter's parent must itself be top-level (no grandparents).
--   2. A community that already HAS chapters cannot become one.
-- A -> B then B -> A: the second fails rule 1, because A now has a parent.
-- A -> B then C -> A: also rule 1. Depth can never reach three.

create or replace function public._places_guard_chapter_depth()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_parent_parent uuid;
  v_parent_found  boolean;
  v_has_children  boolean;
begin
  -- Top-level rows are the overwhelming majority; cost them nothing.
  if new.parent_place_ref is null then
    return new;
  end if;

  if new.parent_place_ref = new.id then
    raise exception 'chapter_cannot_parent_itself' using errcode = '23514';
  end if;

  -- FOR UPDATE, not a bare read: without it two concurrent writes (A->B and B->C) each see
  -- a legal world and commit a three-deep chain between them. Locking the parent row
  -- serialises every write that could make this row's parent a child.
  select true, p.parent_place_ref
    into v_parent_found, v_parent_parent
    from public.places p
   where p.id = new.parent_place_ref
     for update;

  if not coalesce(v_parent_found, false) then
    raise exception 'chapter_parent_not_found' using errcode = '23503';
  end if;

  if v_parent_parent is not null then
    raise exception 'chapter_depth_exceeded: % is already a chapter of %',
      new.parent_place_ref, v_parent_parent using errcode = '23514';
  end if;

  select exists (
    select 1 from public.places c where c.parent_place_ref = new.id
  ) into v_has_children;

  if v_has_children then
    raise exception 'parent_cannot_become_a_chapter: % already has chapters', new.id
      using errcode = '23514';
  end if;

  return new;
end;
$$;

comment on function public._places_guard_chapter_depth() is
  'Chapters are one level deep. A row-level CHECK cannot see other rows, so depth and '
  'cycles are enforced here. Rules: a chapter''s parent must be top-level, and a community '
  'with chapters cannot become one. See 20261216120000.';

drop trigger if exists places_guard_chapter_depth on public.places;
create trigger places_guard_chapter_depth
before insert or update of parent_place_ref on public.places
for each row execute function public._places_guard_chapter_depth();

-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
-- Additive and behaviour-preserving for every existing row: nothing writes
-- parent_place_ref today, so no current row can violate either rule.
--
--   drop trigger if exists places_guard_chapter_depth on public.places;
--   drop function if exists public._places_guard_chapter_depth();
-- ============================================================================
