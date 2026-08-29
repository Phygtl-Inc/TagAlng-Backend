-- ============================================================================
-- PR 2 · Conversational memory hardening  (→ Asjid to review & push)
-- ----------------------------------------------------------------------------
-- WHY: (a) the policy needs to address people correctly without ever saying
--      "mom" — that requires a role + grammatical-gender on the user
--      (LANA_LINGO §3/§4); (b) latent_signals.turn_id is nullable with NO FK,
--      so a signal can't be traced to the message it came from (breaks evals +
--      debugging); (c) lana_sessions.context is untyped jsonb with no contract
--      for the rolling-summary that fights context-rot.
-- SCOPE: additive columns + a NOT-VALID FK + a column comment. Non-destructive.
-- REAL SCHEMA USED: users, latent_signals(turn_id), lana_messages(id),
--      lana_sessions(context). (Note: users already has founder_role, locale.)
-- ============================================================================

begin;

-- (a) role + grammatical gender — inferred by the parser (same pass), neutral defaults.
--     Kept separate from the existing users.founder_role (that's the Founding honorific).
alter table users
  add column if not exists role text
      check (role in ('parent','expecting','grandparent','caregiver',
                      'guardian','relative','family_friend','other','unspecified')),
  add column if not exists role_confidence real,
  add column if not exists grammatical_gender text
      check (grammatical_gender in ('feminine','masculine','unknown')),
  add column if not exists grammatical_gender_confidence real;

update users
   set role = coalesce(role, 'unspecified'),
       grammatical_gender = coalesce(grammatical_gender, 'unknown');

alter table users
  alter column role set default 'unspecified',
  alter column grammatical_gender set default 'unknown';

-- (b) trace every latent signal to its turn. NOT VALID = won't fail on existing rows;
--     validate off-peak afterward.
alter table latent_signals
  add constraint latent_signals_turn_fk
  foreign key (turn_id) references lana_messages(id) on delete set null not valid;
-- later, non-blocking:
--   alter table latent_signals validate constraint latent_signals_turn_fk;

-- (c) contract for the working-context jsonb (rolling summary + deferred goals).
comment on column lana_sessions.context is
  'Working context. Typed by convention: {'
  '"rolling_summary": text,        -- older turns summarized (context-rot mitigation), '
  '"working_slots": jsonb,         -- in-flight task slots (e.g. event draft), '
  '"last_intent": text, '
  '"deferred_goal_ids": uuid[]     -- goals captured mid-task, to resurface (CAPTURE_DEFER)'
  '}. See LANA_CONVERSATIONAL_POLICY_ENGINEERING_v1 §D.';

commit;

-- ============================================================================
-- ROLLBACK:
--   alter table latent_signals drop constraint if exists latent_signals_turn_fk;
--   alter table users drop column if exists role, drop column if exists role_confidence,
--     drop column if exists grammatical_gender, drop column if exists grammatical_gender_confidence;
--   comment on column lana_sessions.context is null;
-- ============================================================================
