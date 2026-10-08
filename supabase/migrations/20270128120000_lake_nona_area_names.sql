-- ============================================================================
-- The two seeded Lake Nona pilot areas are named for people, not for the seed.
--
-- They were "Lake Nona — Block A (placeholder)" / "Block B"; the placeholder and the word
-- Block were scrubbed, leaving "Lake Nona — Area A" / "Area B". Users still read that
-- verbatim: Lana told a New York user the nearest related meet was "in Lake Nona — Area A,
-- about 956 miles away" (prod 2026-10-08), and the app shows the same name in its area
-- progress. "Area A" means nothing to anyone outside the seed; the place is Lake Nona.
--
-- Only these two rows, and only while they still carry the seed suffix, so a rename made
-- by hand since is left alone. Nothing looks an area up by display_name (every read is by
-- id), so two areas sharing a name is harmless; the ids still tell them apart.
--
-- ROLLBACK:
--   update public.blocks set display_name = 'Lake Nona — Area A' where id = '8a2a1072b59ffff';
--   update public.blocks set display_name = 'Lake Nona — Area B' where id = '8a2a1072b5affff';
-- ============================================================================

update public.blocks
   set display_name = 'Lake Nona'
 where id in ('8a2a1072b59ffff', '8a2a1072b5affff')
   and display_name like 'Lake Nona — Area %';
