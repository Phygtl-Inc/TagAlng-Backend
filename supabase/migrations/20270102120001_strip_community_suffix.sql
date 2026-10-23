-- Strip "-community" from every handle, and give the pilot creator her real one
--
-- WHY (2026-10-01)
--
--   > "Why do the communities have an ending with 'community'? That's not okay.
--   >  MrBeast — why say 'mrbeast-community'? That's a handle that no one wants."
--
--   Nine of fourteen handles carry the suffix. None of it was intentional: the PWA
--   required a hyphen (`^[a-z0-9]+(-[a-z0-9]+)+$`) and silently rewrote any single word
--   as `{word}-community`. The database never required it — places_handle_format has
--   always been `^[a-z0-9]+(-[a-z0-9]+)*$`. See PR #174 / tagalng-pwa #99.
--
--   Confirmed by the reservation record: place_handle_reservations holds
--   `zenaidyndb` for this place, and the place got `zenaidyndb-community`. The
--   reservation stored the handle correctly and PLACE CREATION appended the suffix, so
--   the write path needs the same fix as the read path.
--
-- WHY A DIRECT UPDATE RATHER THAN rename_community_handle()
--
--   That function allows ONE rename per community, ever, and stamps handle_renamed_at to
--   record it being spent. Using it here would burn every creator's single rename on
--   fixing our own bug. This is a correction of our mistake, not their choice, so it runs
--   underneath the limit and leaves handle_renamed_at null.
--
-- WHY OLD HANDLES STILL WORK
--
--   Every retired handle lands in place_handle_aliases, and resolve_place_handle falls
--   through to it forever. Any link already posted keeps resolving — it just answers with
--   the new canonical handle so the old one stops spreading.

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

-- ── 2 · Etiqueta do Reino · the handle is the CREATOR, the name is the community ──
--
--   The onboarding form already asks for these separately, so the model is:
--
--       handle          zenaidy              ← the creator
--       display name    Etiqueta do Reino    ← the community
--
--   get.lana.help/zenaidy opens Etiqueta do Reino. Those are not in conflict; they are
--   the two fields the form collects.
--
--   `zenaidyndb` is the local part of her gmail (zenaidyndb@gmail.com) and was never her
--   name. Stripping the suffix generically would have produced it, which is why this runs
--   BEFORE the generic strip below and takes her out of its path.
--
--   `zenaidy` is free: no place, no alias, not protected. There is one EXPIRED
--   reservation on it from 2026-09-15 via locations_hero with no user and no place
--   attached, which holds nothing.

update public.places
   set handle = 'zenaidy', updated_at = now()
 where id = 'ec334eb7-376d-49e8-a7c6-4b9c0c1a3f1b'
   and handle = 'zenaidyndb-community'
   and not exists (select 1 from public.places q where q.handle = 'zenaidy')
   and not exists (select 1 from public.place_handle_aliases a where a.handle = 'zenaidy')
   and not exists (select 1 from public.protected_handles h
                    where h.normalized_handle = 'zenaidy' and h.active);

-- ── 3 · strip the suffix from everything else ──────────────────────────────
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

-- ── 4 · clean up aliases that now point at their own place ─────────────────

delete from public.place_handle_aliases a
 using public.places p
 where a.place_id = p.id and a.handle = p.handle;

-- ── 5 · the rename allowance is untouched ──────────────────────────────────
--
-- Belt and braces. Nothing above sets handle_renamed_at, but if a future edit to this
-- file ever does, every creator silently loses the one rename they are entitled to.

update public.places
   set handle_renamed_at = null
 where handle_renamed_at is not null
   and updated_at > now() - interval '1 minute';

-- ============================================================================
-- RESULTING HANDLES
--
--   zenaidyndb-community  → zenaidy        (Etiqueta do Reino — the pilot)
--   mrbeast-community     → mrbeast        (test)
--   test7-community       → test7          (test)
--   asjidtest5-community  → asjidtest5     (test)
--   asjid-community       → asjid
--   phygtl-community      → phygtl
--   todiba-community      → todiba
--   vyry2-community       → vyry2
--
--   test-7 and asjid-test-6 have no suffix and are unchanged.
--   Every old handle above still resolves, via place_handle_aliases.
--
-- ROLLBACK
--   Restore each handle from its alias row, then delete the alias:
--     update public.places p set handle = a.handle
--       from public.place_handle_aliases a
--      where a.place_id = p.id and a.handle like '%-community';
--     delete from public.place_handle_aliases where handle like '%-community';
-- ============================================================================
