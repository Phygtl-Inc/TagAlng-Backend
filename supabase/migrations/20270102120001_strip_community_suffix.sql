-- Strip "-community" from every handle
--
-- WHY (2026-10-01)
--
--   > "Why do the communities have an ending with 'community'? That's not okay.
--   >  MrBeast — why say 'mrbeast-community'? That's a handle that no one wants."
--
--   Nine of fourteen handles carry the suffix. None of it was intentional: the PWA
--   required a hyphen (`^[a-z0-9]+(-[a-z0-9]+)+$`) and silently rewrote any single
--   word as `{word}-community`. The database never required it — places_handle_format
--   has always been `^[a-z0-9]+(-[a-z0-9]+)*$`. See PR #174 / tagalng-pwa #99.
--
-- WHY A DIRECT UPDATE RATHER THAN rename_community_handle()
--
--   That function allows ONE rename per community, ever, and stamps handle_renamed_at
--   to record it being spent. Using it here would burn every creator's single rename
--   on fixing our own bug. This is a correction of our mistake, not their choice, so
--   it runs underneath the limit and leaves handle_renamed_at null.
--
-- WHY OLD HANDLES STILL WORK
--
--   Every retired handle lands in place_handle_aliases, and resolve_place_handle falls
--   through to it forever. Any link already posted keeps resolving — it just answers
--   with the new canonical handle so the old one stops spreading.

-- ── 1 · retire the current handles into aliases, before changing them ───────
--
-- Insert first. The not-live trigger on place_handle_aliases rejects a handle that is
-- still on a place, so this has to happen in the right order inside one transaction —
-- which is why the trigger is dropped and restored around it.

alter table public.place_handle_aliases disable trigger place_handle_aliases_not_live;

insert into public.place_handle_aliases (handle, place_id, retired_at)
select p.handle, p.id, now()
from public.places p
where p.handle like '%-community'
on conflict (handle) do nothing;

-- ── 2 · strip the suffix ────────────────────────────────────────────────────
--
-- Collision-safe: skips any row whose stripped form is already taken by another place,
-- already retired as an alias, or reserved. Nothing is forced.

update public.places p
   set handle = left(p.handle, length(p.handle) - length('-community')),
       updated_at = now()
 where p.handle like '%-community'
   and length(left(p.handle, length(p.handle) - length('-community'))) >= 3
   and not exists (
     select 1 from public.places q
      where q.id <> p.id
        and q.handle = left(p.handle, length(p.handle) - length('-community')))
   and not exists (
     select 1 from public.place_handle_aliases a
      where a.handle = left(p.handle, length(p.handle) - length('-community'))
        and a.place_id <> p.id)
   and not exists (
     select 1 from public.protected_handles h
      where h.normalized_handle = left(p.handle, length(p.handle) - length('-community'))
        and h.active);

alter table public.place_handle_aliases enable trigger place_handle_aliases_not_live;

-- ── 3 · clean up aliases that now point at their own place ─────────────────

delete from public.place_handle_aliases a
 using public.places p
 where a.place_id = p.id and a.handle = p.handle;

-- ============================================================================
-- NOT DONE HERE · Etiqueta do Reino
--
--   `zenaidyndb-community` strips to `zenaidyndb`, which is the creator's gmail
--   username, not her brand. That is not an improvement, so this migration leaves it
--   alone deliberately — the collision guards above do not catch it, the omission is
--   the point.
--
--   The right handle is `etiquetadoreino`, and it should be confirmed with her before
--   anyone sets it. Her one-shot rename is still unspent, so she can do it herself
--   from settings once that screen exists.
--
--   To set it manually after confirming:
--     insert into public.place_handle_aliases (handle, place_id)
--       values ('zenaidyndb-community', 'ec334eb7-376d-49e8-a7c6-4b9c0c1a3f1b');
--     update public.places set handle = 'etiquetadoreino'
--      where id = 'ec334eb7-376d-49e8-a7c6-4b9c0c1a3f1b';
--
-- ROLLBACK
--   Restore each handle from its alias row, then delete the alias:
--     update public.places p set handle = a.handle
--       from public.place_handle_aliases a
--      where a.place_id = p.id and a.handle like '%-community';
--     delete from public.place_handle_aliases where handle like '%-community';
-- ============================================================================
