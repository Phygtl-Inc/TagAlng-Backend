-- Rapport tile: suggested one-tap answers on an ask ("what do you enjoy most at X?").
--
-- The place-affinity question shipped as a blank prompt, so the user had to invent the
-- shape of the answer. The gap-opening call already AI-authors the question, so it now
-- also returns 2-3 tappable facets ("The early classes", "The people") which the card
-- shows as chips; a tap posts the chip text through the normal answer path, where it
-- becomes the same place-tagged affinity claim a typed answer would.
--
-- Nullable jsonb array of short English strings; translations ride in
-- question_i18n[lang].answer_options. Absent/empty = free text only, exactly as before.

alter table public.rapport_gaps
  add column if not exists answer_options jsonb;

comment on column public.rapport_gaps.answer_options is
  'Suggested one-tap answers (jsonb array of short strings, English-canonical). '
  'Translations live in question_i18n[lang].answer_options. Null/empty = free text only.';
