-- ============================================================================
-- An invite candidate is keyed on the INVITE, and carries the joiner's
-- membership answer until she pins her place (backend asks §28(c), §23).
--
-- §28(c) — the bug
--   add_circle keyed the self-confirm candidate on the KIND alone
--   (circle_key = circle_type when no detail is sent, and the invite path never
--   sends one). A joiner who accepted one fitness invite, pinned her gym, then
--   accepted a DIFFERENT owner's fitness invite got the same affiliation back,
--   with the ack claiming grounded:false for a grounded row; grounding it again
--   re-pointed the community she already had at a different gym.
--
--   invite_id names the circle_invites row a candidate came from, so the worker
--   dedupes on (user, invite) instead of (user, kind). circle_key keeps its
--   CHECK (^[a-z][a-z0-9_]{1,63}$) and its job as the user's own words: a
--   second candidate of the same kind takes the next free key the Join path
--   already uses ("fitness", "fitness_2", …) — never an id, which ground_options
--   would otherwise feed into the place search as words.
--
-- §23 — membership before a place exists
--   "Just curious" on /i/<token> used to have nothing to land on until the
--   candidate was grounded. membership_intent holds the answer on the ungrounded
--   row; grounding then writes status = 'curious' instead of 'confirmed'. It is
--   read only at grounding time — once the row has a place, status is the truth
--   and /lana/circles/membership changes that.
--
-- Additive only: two nullable columns + one partial unique index. No existing
-- row changes. DEPLOY ORDER: this migration, then the worker (the worker
-- degrades to the old per-kind behaviour while these columns are absent).
--
-- ROLLBACK
--   drop index if exists public.circle_affiliations_user_invite_active_idx;
--   alter table public.circle_affiliations
--     drop column if exists membership_intent,
--     drop column if exists invite_id;
-- ============================================================================

alter table public.circle_affiliations
  add column if not exists invite_id uuid
    references public.circle_invites (id) on delete set null,
  add column if not exists membership_intent text
    check (membership_intent is null or membership_intent in ('member', 'curious'));

comment on column public.circle_affiliations.invite_id is
  'The invite (circle_invites.id) an invite_confirmed candidate was self-confirmed '
  'from. The self-confirm dedupe key: one live candidate per (user, invite), so two '
  'owners'' invites of the same kind make two rows. Null for every other source.';

comment on column public.circle_affiliations.membership_intent is
  '"member" | "curious" answered on an invite BEFORE the candidate had a place '
  '(§23). Grounding writes status=''curious'' when this is ''curious'', else '
  '''confirmed''. Not read once the row is grounded — status is the truth then.';

-- One live candidate per invite per person; also closes the read-then-insert race
-- in add_circle when the accept is double-tapped.
create unique index if not exists circle_affiliations_user_invite_active_idx
  on public.circle_affiliations (user_id, invite_id)
  where dismissed_at is null and invite_id is not null;
