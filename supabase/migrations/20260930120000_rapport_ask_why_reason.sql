-- Rapport tile: the ⓘ line under a question must say WHY Lana is asking.
--
-- `why_frame` is a 2-5 word teaser ("about FIT 407 Lake Nona…") — it names the topic
-- and tells the user nothing about what answering does for them. It stays as the
-- collapsed tile's hook; `why_reason` is the AI-authored one-liner the opened card
-- shows instead ("So I can point you to neighbors who train there at the same hours").
--
-- Nullable and self-healing on purpose: the reason is composed off the turn
-- (app/rapport_reasons.py) and the ranker kicks a render on a miss, so a row that
-- predates this column — or whose compose call failed — simply falls back to the teaser.

alter table public.rapport_gaps
  add column if not exists why_reason text;

comment on column public.rapport_gaps.why_reason is
  'AI-authored reason shown on the ask card: what answering lets Lana do for this user. '
  'English-canonical; translations live in question_i18n[lang].why_reason. Null until '
  'composed (app/rapport_reasons.py, background) — readers fall back to why_frame.';

comment on column public.rapport_gaps.question_i18n is
  'AI-rendered translations of question / why_frame / why_reason keyed by ISO lang code; '
  'English canonical text stays in the question / why_frame / why_reason columns.';
