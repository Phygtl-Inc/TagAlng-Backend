-- Headquarters, and chapters.
--
-- Two product decisions from 2026-09-16, one migration, because they are the same problem:
-- a community that is not anywhere still has to be findable, and the way it becomes findable
-- is by having somewhere it is FROM.
--
--   1. Every community has a headquarters. City is required for a creator community,
--      optional-but-present for everyone else (they have a real address instead).
--      hq_city IS VISIBLE ON THE MAP.
--   2. A community may have local chapters — "Chapter Orlando" of a global topic community.
--      A chapter has real geography; its parent may not.
--
-- ============================================================================
-- WHY NOT JUST SET lat/lng ON THE CREATOR ROW
-- ============================================================================
-- 20261207120000 shipped places_creator_has_no_geography:
--
--     check (place_type is distinct from 'creator'
--            or (lat is null and lng is null and zip is null and h3 is null))
--
-- and its reasoning is still correct. discover_communities_near matches on
-- "inside the radius OR (no coordinates AND the place's ZIP equals mine)". Put a ZIP or a
-- point on a creator community and every follower in that ZIP starts seeing it under
-- Communities Near Me — a global topic community rendered as a neighbour's gym.
--
-- So hq_lat/hq_lng are SEPARATE COLUMNS, not a relaxation of that constraint. The constraint
-- is untouched and still holds. The distinction they encode is real and worth keeping sharp:
--
--     lat / lng    = "this community is HERE."     A building. A distance can be computed.
--     hq_lat/hq_lng= "this community is RUN from
--                     here."                        A city centroid. NOT a location.
--
-- ============================================================================
-- THE ONE RULE THAT MAKES THIS SAFE
-- ============================================================================
-- hq_city, hq_lat and hq_lng MUST NOT APPEAR IN ANY DISCOVERY PREDICATE. Not in
-- discover_communities_near, not in find_places_by_activity_semantic, not in
-- local_signal_for_places, not in any distance sort, not in any "near me" arm anywhere.
-- They are a label and a pin position. The moment one of them is used to decide whether
-- something is NEAR someone, this migration has re-created exactly the leak that
-- places_creator_has_no_geography exists to prevent — and it will do it silently, because
-- the results will look plausible.
--
-- Rendering is a separate concern from discovery and is allowed to read them. Discovery is
-- not. Reviewers: grep for hq_ in any function whose name contains near/radius/distance.

-- ---------------------------------------------------------------------------
-- 1. Headquarters
-- ---------------------------------------------------------------------------
alter table public.places
  add column if not exists hq_city text,
  add column if not exists hq_lat  double precision,
  add column if not exists hq_lng  double precision;

comment on column public.places.hq_city is
  'Where this community is RUN FROM, as a human label ("Tampa, FL"). Required for '
  'place_type = ''creator''. NEVER a discovery predicate: not in any near/radius/distance '
  'query, ever. See the header of 20261212120000 before using this column anywhere.';

comment on column public.places.hq_lat is
  'Display-only latitude of hq_city, for the map pin. Deliberately NOT places.lat: a '
  'creator community has no lat/lng by constraint (20261207120000), and a city centroid '
  'is not a location. Never use for distance.';

comment on column public.places.hq_lng is
  'Display-only longitude of hq_city. See hq_lat.';

-- Both or neither. A half-populated pin renders at the equator.
alter table public.places
  drop constraint if exists places_hq_point_complete;
alter table public.places
  add constraint places_hq_point_complete check (
    (hq_lat is null) = (hq_lng is null)
  );

-- A creator community with no headquarters is undiscoverable: it is excluded from
-- geographic discovery by construction, so hq_city is the only thing that can place it
-- anywhere at all. Enforced rather than expected, because the failure is invisible —
-- the row exists, nothing errors, and no user ever sees it.
alter table public.places
  drop constraint if exists places_creator_has_hq_city;
alter table public.places
  add constraint places_creator_has_hq_city check (
    place_type is distinct from 'creator'
    or (hq_city is not null and length(btrim(hq_city)) > 0)
  );

comment on constraint places_creator_has_hq_city on public.places is
  'A creator community is excluded from discover_communities_near by construction, so '
  'without a headquarters it cannot be found by anyone. Fail at write, not at read.';

-- ---------------------------------------------------------------------------
-- 2. Chapters
--
-- A community's identity IS places.id — the SWITCH COMMUNITY picker, community_scope, the
-- roster endpoints and events.circle_place_ref all key on it (20261207120000 spells this
-- out). So a chapter is not a new kind of object. It is a place row with real geography
-- whose parent is another place. No new identity model, no FK to move.
-- ---------------------------------------------------------------------------
alter table public.places
  add column if not exists parent_place_ref uuid references public.places(id) on delete set null;

comment on column public.places.parent_place_ref is
  'The community this place is a CHAPTER of ("Chapter Orlando" -> "Iron Man Training"). '
  'Null for ordinary communities. One level only. Visibility is two-way between a chapter '
  'and its parent and NEVER between sibling chapters — see the note below.';

create index if not exists places_parent_idx
  on public.places(parent_place_ref)
  where parent_place_ref is not null;

-- A chapter is local BY DEFINITION — it is the thing that gives a geography-less community
-- somewhere to actually meet. A chapter without coordinates is just another place.
alter table public.places
  drop constraint if exists places_chapter_has_geography;
alter table public.places
  add constraint places_chapter_has_geography check (
    parent_place_ref is null
    or (lat is not null and lng is not null)
  );

-- One level. A chapter of a chapter has no product meaning and would make the visibility
-- union below recursive — which is how "Boston sees Orlando's content" ships by accident.
alter table public.places
  drop constraint if exists places_chapter_not_nested;
alter table public.places
  add constraint places_chapter_not_nested check (
    parent_place_ref is null or parent_place_ref <> id
  );

-- ============================================================================
-- VISIBILITY — the contract the read side must implement (NOT enforced here)
-- ============================================================================
-- Decided 2026-09-16: chapter content is TWO-WAY, labelled with its origin.
--
--   member of chapter C, whose parent is P
--       sees  place_ref = C          (their own)
--        and  place_ref = P          (the parent)
--        and  NOTHING ELSE
--
--   member of parent P, no chapter
--       sees  place_ref = P
--        and  place_ref in (select id from places where parent_place_ref = P)
--             -- every chapter, each row carrying origin_place_ref/origin_place_name
--
-- CHAPTER -> PARENT: yes.  PARENT -> CHAPTER: yes.  CHAPTER -> SIBLING CHAPTER: NO.
--
-- If Orlando's content rolls up to the parent and the parent rolls down to Boston, then
-- Boston reads Orlando's content as local. That is the one failure this product cannot
-- ship. The asymmetry is deliberate and it is not an optimisation.
--
-- Every row that crosses a boundary must carry origin_place_ref AND origin_place_name in
-- the payload. Not for decoration — the client cannot label what the query does not return,
-- and an unlabelled cross-chapter row is indistinguishable from a local one.
-- ============================================================================

-- ============================================================================
-- ROLLBACK
--   Purely additive: three nullable columns, one nullable self-FK, four CHECKs that every
--   existing row satisfies (no current row is 'creator' with a null hq_city — there are two
--   creator rows in prod and they are backfilled by the deploy step below). Nothing that
--   reads these tables changes behaviour until a caller opts in.
--
--     alter table public.places
--       drop constraint if exists places_chapter_not_nested,
--       drop constraint if exists places_chapter_has_geography,
--       drop constraint if exists places_creator_has_hq_city,
--       drop constraint if exists places_hq_point_complete;
--     drop index if exists public.places_parent_idx;
--     alter table public.places
--       drop column if exists parent_place_ref,
--       drop column if exists hq_lng,
--       drop column if exists hq_lat,
--       drop column if exists hq_city;
--
--   DEPLOY ORDER: the two existing place_type='creator' rows must get an hq_city BEFORE
--   places_creator_has_hq_city is added, or this migration fails on them. Either backfill
--   first, or apply section 1's columns, backfill, then the constraint.
-- ============================================================================
