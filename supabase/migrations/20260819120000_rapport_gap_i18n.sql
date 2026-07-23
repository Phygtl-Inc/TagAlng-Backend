-- Rapport tile i18n: questions are stored English-canonical (like every other
-- user-facing fact) and rendered into the user's preferred language at WRITE
-- time — when the gap is opened and again whenever users.locale changes. The
-- home-screen next-ask read path then serves a saved string instead of waiting
-- on an LLM translation.
--
-- Shape: {"pt": {"question": "...", "why_frame": "..."}, "ur": {...}} — one key
-- per language the user has actually used; English lives in the canonical
-- question / why_frame columns and needs no entry.

alter table public.rapport_gaps
  add column if not exists question_i18n jsonb;

comment on column public.rapport_gaps.question_i18n is
  'AI-rendered translations of question/why_frame keyed by ISO lang code; English canonical text stays in question/why_frame.';
