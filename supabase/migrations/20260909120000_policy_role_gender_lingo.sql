-- Conversational Policy prerequisites (LANA_CONVERSATIONAL_POLICY_v1 PART 4 + §D).
--
-- 1) users.role + users.grammatical_gender — the lingo constitution's role-aware
--    address ("your grandkids", never "mom") and gendered-language agreement
--    ("bienvenido/bienvenida", never default feminine) reference a USER CONTEXT
--    nothing populated until now. The claim extractor writes these (worker,
--    service role); both are private profile facts, never claims, never shown.
--
-- 2) capability_index lingo scrub — four seeded rows still said "mom". The
--    decide_turn policy pastes capability names/descriptions into its prompt,
--    so leaving them teaches the model the banned word from our own registry.

alter table public.users
  add column if not exists role text
    check (role is null or role in
      ('parent','expecting','grandparent','caregiver','guardian','relative')),
  add column if not exists grammatical_gender text
    check (grammatical_gender is null or grammatical_gender in
      ('feminine','masculine'));

comment on column public.users.role is
  'Inferred household role (parent/expecting/grandparent/caregiver/guardian/relative) '
  'for role-aware address (lingo constitution). Written by the worker''s claim '
  'extractor; never a claim, never user-facing as a label.';
comment on column public.users.grammatical_gender is
  'Grammatical gender for gendered-language agreement (es/pt). Inferred from the '
  'user''s own self-reference only; null = rephrase neutrally, never default feminine.';

-- Lexicon scrub (constitution rule 1): registry text must be paste-safe into
-- any user-copy-authoring prompt.
update public.capability_index
   set description = 'Find a meet, playgroup, or activity group with people at a similar life stage'
 where capability_id = 'looking.meet';

update public.capability_index
   set capability_name = 'Find a neighbor-tested recommendation',
       description = 'Find a neighbor-tested recommendation for a service, professional, or place'
 where capability_id = 'looking.tip';

update public.capability_index
   set description = 'Share a tried-and-true tip or recommendation with neighbors'
 where capability_id = 'sharing.tip';

update public.capability_index
   set description = 'Find nearby people with a similar life stage, kids, or interests'
 where capability_id = 'discovery.find_peers';
