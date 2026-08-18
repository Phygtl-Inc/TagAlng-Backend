-- ── the member's own words for a feature, next to its key ─────────────────────
--
-- place_features stored only the key, and the chip's text was reverse-engineered from
-- it (community_surface._feature_label: strip has_/is_/offers_/allows_, underscores to
-- spaces, capitalize the first letter). That round-trips a lowercase word — "Pool" goes
-- in, has_pool is stored, "Pool" comes back — and destroys everything else:
--
--   "BYOB"        -> has_byob         -> "Byob"
--   "24/7 access" -> has_24_7_access  -> "24 7 access"
--   "Kids' area"  -> has_kids_area    -> "Kids area"
--
-- The key CANNOT hold the answer: it is the identity (unique per place/key/sub_group,
-- the thing that folds one member's "Pool" and another's "pool" into a single shared
-- truth, and what remove_feature deletes by), so it has to stay a slug. The typed text
-- is a second fact and gets a second column — exactly the concept/label pair
-- place_activities has carried since 20261010.
--
-- NO BACKFILL, deliberately. Null keeps the derivation, which is still the right answer
-- for two populations: every row written before today, and every row chat learns
-- (vertex_extract emits a key, nobody types a label). _feature_label prefers the stored
-- text and falls back to the key, so both paths stay correct with no data migration.
--
-- DEPLOY ORDER: this migration, then the worker (the worker writes the column).
--
-- ROLLBACK: alter table public.place_features drop column if exists label;

alter table public.place_features
  add column if not exists label text
  check (label is null or char_length(label) between 1 and 48);

comment on column public.place_features.label is
  'The member''s own words for this feature ("BYOB", "24/7 access"), written from the '
  'community panel. Null on rows learned in chat and on every row predating this column '
  '— community_surface._feature_label derives the text from `key` in that case, which is '
  'lossy for casing, digits and punctuation. Never the identity: that is `key`.';
