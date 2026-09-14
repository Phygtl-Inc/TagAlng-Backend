-- Creator communities — a community with no geography.
--
-- The pivot needs "Iron Man Training" to be a community: thousands of people who share a
-- topic, scattered across the world, arriving through a link in a creator's bio rather than
-- by walking into a building.
--
-- The obvious move was to drop circle_affiliations_confirmed_has_place (20260916120000) so a
-- community can exist without a place. That is the wrong read of what the constraint does.
-- A community has no id of its own — its identity IS places.id, everywhere: the SWITCH
-- COMMUNITY picker, community_scope, the profile and roster endpoints, mail_community_members,
-- and a real foreign key from events.circle_place_ref. Drop the constraint and you do not get
-- place-less communities; you get rows that exist and cannot be selected, opened, joined,
-- tagged or mailed.
--
-- And the thing people actually mean by "must have a place" is already false: places.lat,
-- .lng, .address and .zip are all nullable today. A place with a name and no coordinates is
-- legal and works. So a creator community is a place with no geography — no new identity
-- model, no FK to move, nothing downstream to rewrite.
--
-- What this migration adds is the ability to SAY SO, rather than leaving it to be inferred
-- from a missing address. Address is already nullable for ordinary communities (someone
-- grounds to a spot Google has no address for), so "address is null" would misclassify real
-- communities as creator ones — silently, and only on the rows where it matters least to
-- notice. An explicit value cannot be wrong by accident.

-- ---------------------------------------------------------------------------
-- 1. 'creator' as a kind of place, and as a kind of affiliation.
--    Two lists, because they answer different questions: places.place_type is what the
--    place IS (advisory, canonical), circle_affiliations.circle_type is the user's own
--    framing of their membership (O6). Both currently carry the same ten values.
-- ---------------------------------------------------------------------------
alter table public.places
  drop constraint if exists places_place_type_check;
alter table public.places
  add constraint places_place_type_check check (
    place_type is null or place_type in (
      'school','faith','fitness','kids_activity','neighborhood',
      'hobby','support','heritage','friends','other','creator'
    )
  );

alter table public.circle_affiliations
  drop constraint if exists circle_affiliations_circle_type_check;
alter table public.circle_affiliations
  add constraint circle_affiliations_circle_type_check check (
    circle_type in (
      'school','faith','fitness','kids_activity','neighborhood',
      'hobby','support','heritage','friends','other','creator'
    )
  );

comment on column public.places.place_type is
  'Advisory canonical type (O6). The user''s own framing lives on '
  'circle_affiliations.circle_type. ''creator'' = a community that exists around a topic '
  'and a creator''s link rather than a location: no coordinates, never surfaced by '
  'geographic discovery, and the client hides address/distance on it.';

-- ---------------------------------------------------------------------------
-- 2. A creator community carries no geography — enforced, not merely expected.
--
--    This is what keeps creator communities out of discover_communities_near without
--    touching that function. Its filter is "inside the radius, OR (no coordinates AND the
--    place's ZIP equals mine)". With lat/lng/zip all null a creator community can satisfy
--    neither arm, so it is excluded by construction.
--
--    Left as a convention, that exclusion lasts until the first person stamps a creator's
--    home city onto the row and every follower in that ZIP starts seeing "Iron Man Training"
--    under Communities Near Me. As a constraint, that write fails loudly at the source.
--    Cheaper than re-declaring an 80-line function to say the same thing, and it cannot
--    drift away from the behaviour it protects.
-- ---------------------------------------------------------------------------
alter table public.places
  add constraint places_creator_has_no_geography check (
    place_type is distinct from 'creator'
    or (lat is null and lng is null and zip is null and h3 is null)
  );

comment on constraint places_creator_has_no_geography on public.places is
  'A creator community is not anywhere. Geography on it would leak it into '
  'discover_communities_near, whose no-coordinates arm matches on ZIP equality.';

-- ============================================================================
-- ROLLBACK
-- ----------------------------------------------------------------------------
-- Additive: both CHECKs widen, so every existing row still passes and nothing that
-- reads these tables changes behaviour. Reverting needs any 'creator' rows gone first.
--
--   alter table public.places drop constraint if exists places_creator_has_no_geography;
--   -- then re-add the two type CHECKs without 'creator'.
-- ============================================================================
