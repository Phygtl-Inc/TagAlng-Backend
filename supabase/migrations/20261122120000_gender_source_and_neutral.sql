-- Pronoun/gender model, part 2 — provenance and a neutral value.
--
-- 20260909120000 added users.grammatical_gender with the right INTENT ("inferred
-- from the user's own self-reference only; never default feminine") but two gaps
-- made it unsafe to rely on:
--
-- 1) NO PROVENANCE. persist_role_gender() treated every write the same, so a
--    later INFERENCE could silently overwrite what the user EXPLICITLY told us.
--    A person who says "call me she" must never be re-gendered by a stray
--    "estou animado" a month later. gender_source makes 'stated' immutable
--    except by another 'stated'.
--
-- 2) NO NEUTRAL. The check allowed only feminine/masculine, so a user who states
--    they/them had nowhere to be stored — their explicit statement fell through
--    to NULL, which is the *unknown* case, and the unknown case is permitted to
--    pick masculine when a language forces a choice. Stating your pronouns and
--    being guessed at anyway is worse than not being asked.
--
-- 'neutral' is a real stored value with its own prompt rule (app/context.py
-- _GENDER_GUIDANCE): never a gendered form in any language, rephrase so agreement
-- never arises, and never coin a -e/-x neologism the user did not use first.
--
-- Names are deliberately NOT a source. The extractor is instructed never to infer
-- gender from a name and that stays true: Andrea, Kim, Alex, Sasha, Noor and Jordan
-- all flip across the languages this product serves, and a misgendered greeting in
-- the second sentence costs more than staying neutral ever does.

alter table public.users
  drop constraint if exists users_grammatical_gender_check;

alter table public.users
  add constraint users_grammatical_gender_check
    check (grammatical_gender is null or grammatical_gender in
      ('feminine', 'masculine', 'neutral'));

alter table public.users
  add column if not exists gender_source text
    check (gender_source is null or gender_source in ('stated', 'inferred'));

-- Every gender written before this migration came from the extractor's inference
-- path, so backfill it as such — leaving it NULL would let the first inference
-- after deploy overwrite a value a user may well have stated in chat.
update public.users
   set gender_source = 'inferred'
 where grammatical_gender is not null
   and gender_source is null;

comment on column public.users.grammatical_gender is
  'Grammatical gender for gendered-language agreement (es/pt): feminine | masculine | '
  'neutral. Inferred from the user''s own self-reference, or stated outright ("call me '
  'she"). NEVER inferred from a name. null = unknown; rephrase to avoid agreement and '
  'fall back to masculine only when the language forces a choice — never feminine.';
comment on column public.users.gender_source is
  'Provenance of grammatical_gender. ''stated'' = the user said so explicitly and it is '
  'immutable except by another explicit statement; ''inferred'' = derived from their own '
  'gendered self-reference and may be refined. Guards against an inference overwriting a '
  'user''s stated pronouns (app/claims_persist.persist_role_gender).';
