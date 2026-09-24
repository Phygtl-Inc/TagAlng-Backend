-- One community entity. Two kinds of EVIDENCE.
--
-- DECISION (2026-09-24). `creator` and `place` are not entity types. They are the two
-- kinds of evidence a community can have:
--
--     geography (lat/lng)                -> it appears on the map
--     an external identity (IG/YT/...)   -> it carries an audience, verifiable by backlink
--
-- A community may have NEITHER, EITHER, or BOTH — and may gain the second one later.
-- A creator who opens a studio adds an address. A gym that starts an Instagram adds an
-- identity. Neither event changes what the community IS, who its members are, or what
-- its handle is.
--
-- ============================================================================
-- WHY THIS CONSTRAINT HAS TO GO
-- ============================================================================
-- 20261207120000 shipped:
--
--     places_creator_has_no_geography
--       check (place_type is distinct from 'creator'
--              or (lat is null and lng is null and zip is null and h3 is null))
--
-- which makes place_type STRUCTURAL: once a row is 'creator' it may never have
-- coordinates. That is exactly the merge above, forbidden. A creator adding a studio
-- address gets a constraint violation, and the only way out would be to change
-- place_type — mutating the row's declared identity to record a new fact about it.
--
-- It also contradicts that migration's own comment on the column, which calls place_type
-- an "Advisory canonical type (O6)". This restores that intent.
--
-- ============================================================================
-- THE LEAK IT WAS PROTECTING IS REAL, AND IS CLOSED PROPERLY IN 20261215120000
-- ============================================================================
-- The constraint existed for a good reason, quoted from its own comment: "A creator
-- community is not anywhere. Geography on it would leak it into
-- discover_communities_near, whose no-coordinates arm matches on ZIP equality."
--
-- True. But that arm exists to rescue IMPORTED rows — ones with a real street address
-- that simply have no geocode yet. A community with no address was never its subject.
-- So the next migration requires an address on that arm, which closes the leak at the
-- source, for every row, with no reference to place_type at all.
--
-- Dropping this WITHOUT that change would reopen the leak. The two migrations are a
-- pair; do not apply one alone.

alter table public.places
  drop constraint if exists places_creator_has_no_geography;

comment on column public.places.place_type is
  'ADVISORY canonical type only (O6) — a label for copy and iconography. It carries NO '
  'structural meaning and nothing may branch on it. Whether a community is on the map is '
  'decided by lat/lng. Whether it has an external audience is decided by '
  'external_community_identities. A row may be place_type=''creator'' AND have '
  'coordinates: that is a creator who added an address, and it is expected.';

comment on column public.places.hq_city is
  'Where this community is RUN FROM, as a human label ("Tampa, FL"). NEVER a discovery '
  'predicate: not in any near/radius/distance query, ever. A community appears on the map '
  'if and only if it has lat/lng. hq_city is what a community says about itself when it '
  'has no coordinates — it is not a position.';

-- ============================================================================
-- ROLLBACK
--   Re-adding requires that no creator row has acquired geography in the meantime:
--
--     alter table public.places add constraint places_creator_has_no_geography check (
--       place_type is distinct from 'creator'
--       or (lat is null and lng is null and zip is null and h3 is null));
--
--   Roll back 20261215120000 at the same time or the ZIP arm stays narrowed (which is
--   harmless — it is strictly more conservative — but it is not the prior behaviour).
-- ============================================================================
