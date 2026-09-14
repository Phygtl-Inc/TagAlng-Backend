-- place_features accepts 'community_create' — the source it has been rejecting all along.
--
-- publish_community writes one place_features row per answered question, with
-- source='community_create' (community_capture.py). That value was never added to the
-- CHECK, which allows only ('rapport','owner','import','inferred'). So every one of those
-- inserts has been raising 23514 since the create-a-community flow shipped.
--
-- Nothing surfaced, because the write is wrapped in `except Exception: logger.exception(...)`
-- — correctly, since a failed feature must not cost the neighbour their community. The
-- result is the worst shape a bug can take: the community publishes, the card says it
-- worked, and every answer the person typed is gone. Same class as the silent save failure
-- Tim flagged in the recommendation eval ("the user is told the tip posted while every
-- captured field is lost"), in a different table.
--
-- Verified before writing this: place_features holds 13 rows on prod and 0 on dev, all of
-- them source='rapport'. Not one 'community_create' row exists in either environment.
--
-- Widening the CHECK is the whole fix; the write path is already correct and needs no code
-- change. Existing rows are unaffected — the old four values all still pass.

alter table public.place_features
  drop constraint if exists place_features_source_check;

alter table public.place_features
  add constraint place_features_source_check check (
    source in ('rapport', 'owner', 'import', 'inferred', 'community_create')
  );

comment on column public.place_features.source is
  'Where the fact came from. ''community_create'' = the creator answered Lana''s own '
  'question set while making the community: first-hand, so confidence 0.9, but not an '
  'owner claim — a later source=''owner'' write still overwrites it.';

-- ============================================================================
-- ROLLBACK — only after any 'community_create' rows are gone, or the CHECK will not apply:
--   alter table public.place_features drop constraint if exists place_features_source_check;
--   alter table public.place_features add constraint place_features_source_check
--     check (source in ('rapport','owner','import','inferred'));
-- ============================================================================
