-- ---------------------------------------------------------------------------
-- A community's description is written once, not on every open.
-- ---------------------------------------------------------------------------
-- `community_profile` authored the place's one-line description with a model
-- call in the request path: ~1.6s, and the card cannot render until it returns.
-- An in-process lru_cache hid it locally and hid nothing in production — the
-- cache dies with the process and every pod pays its own miss, so the same
-- sentence was bought again and again for a place whose facts never changed.
--
-- The description belongs to the place, so it lives on the place. `blurb_key`
-- fingerprints the facts it was written from (name, the members' noun for it,
-- area, features, member count); a mismatch means a fact moved and the line is
-- rewritten in the background. Readers never block on it: a stale or missing
-- blurb serves the factual template and the authored line lands for next time.
--
-- ROLLBACK: alter table public.places drop column blurb, drop column blurb_key;
-- ---------------------------------------------------------------------------

alter table public.places
  add column if not exists blurb text,
  add column if not exists blurb_key text;

comment on column public.places.blurb is
  'AI-authored one-line description of the place, written from facts the DB already '
  'proves (features, member count, area). Never user-visible truth on its own — the '
  'worker falls back to a factual template whenever it is absent or stale.';
comment on column public.places.blurb_key is
  'Fingerprint of the facts blurb was written from. Worker rewrites the blurb in the '
  'background when the current facts hash to something else.';
