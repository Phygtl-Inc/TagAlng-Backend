-- A community's own noun and emoji, instead of its grouping bucket's.
--
-- circle_type has ten values and every sport lands on "fitness", so three separate
-- features rendered a table-tennis club as a gym: the Places search looked for
-- "gym" (and restricted includedType to gym, which also broke the user's typed
-- search), the card showed 🏋️ on every option, and _GROUND_NOUN called it "your
-- gym" — in the question text the person reads (2026-08-06/07).
--
-- The activity was always in circle_key, but a slug cannot be turned into readable
-- copy by string surgery ("lagoinha_small_group" -> "lagoinha small"). So the
-- extractor now picks both at capture, exactly as events.cover_emoji is picked.
-- Null on existing rows: place_relation_noun/emoji fall back to the type maps, so
-- old circles keep today's behaviour until they are mentioned again.

alter table public.circle_affiliations
  add column if not exists noun  text,
  add column if not exists emoji text;

comment on column public.circle_affiliations.noun is
  'Short lower-case noun for this community, shown as "your <noun>". No venue name '
  '(that would leak the place). Falls back to circles_flow._GROUND_NOUN when null.';
comment on column public.circle_affiliations.emoji is
  'One emoji for this community, chosen at capture like events.cover_emoji. Falls '
  'back to circles_flow._RELATION_EMOJI when null.';
