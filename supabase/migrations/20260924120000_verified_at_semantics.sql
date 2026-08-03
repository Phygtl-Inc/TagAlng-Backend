-- ============================================================================
-- users.phone_verified_at MEANS "VERIFIED BY ANY METHOD" — say so in the schema
--
-- Comments only. No column, policy, view or function changes; nothing behaves
-- differently after this migration.
--
-- WHY
--   20260717120000_email_verify_unlocks_gates.sql made a confirmed email stamp
--   phone_verified_at, deliberately: every gate (event insert, event join,
--   propose_intro, the guest boundary, the ZIP unlock count) read that column,
--   and email-only users were getting 42501 -> HTTP 403 on create_event. The
--   bridge fixed a live outage and is working as designed.
--
--   What it left behind is a name that lies. Verified on prod 2026-08-03: of 37
--   users, 11 have phone_verified_at set and 11 have email_verified_at set, and
--   ZERO have email without phone -- i.e. every "phone verified" user on the
--   platform was verified by EMAIL. Two separate readers (an engineering audit
--   and this session) independently mis-read the column as evidence of phone
--   possession. A comment is cheaper than a third misreading.
--
-- WHAT THIS DOES NOT MEAN
--   The investor_metrics view is CORRECT. Its columns are labelled "verified"
--   (M04 "verified moms per LIVE block", M09 "verified users who were
--   referred") and the bridged column is exactly that. Only the phrasing of two
--   inline comments in the already-applied metric migrations is stale; those
--   files are history and are deliberately left untouched.
--
-- IF YOU EVER NEED REAL PHONE POSSESSION
--   auth.users.phone_confirmed_at is untouched by the bridge and still holds
--   the truth. Read it directly -- do not re-purpose this column.
-- ============================================================================

comment on column public.users.phone_verified_at is
  'Verified by ANY method — phone OR email. Despite the name, a confirmed email '
  'stamps this column (trigger sync_email_verified, 20260717120000) because every '
  'verification gate reads it. On prod today it is 100% email-derived. It is the '
  'right column for "is this a real, verified member?" and the WRONG column for '
  '"does this person possess a phone" — for that read auth.users.phone_confirmed_at. '
  'Rename to verified_at when something else is already touching these ~20 call sites.';

comment on column public.users.email_verified_at is
  'Mirror of auth.users.email_confirmed_at (trigger sync_email_verified). Email is '
  'the live auth method; phone_verified_at is stamped alongside it for the legacy '
  'gates. Not a gate itself — the gates read phone_verified_at.';

comment on view public.investor_metrics is
  'Investor/TPR metrics. "Verified" throughout means verified by any method '
  '(see users.phone_verified_at) — that is the intended meaning of every '
  'verified_* figure here, NOT phone possession. Two inline comments in the '
  'source migrations still say "phone_verified_at timestamp"; the column comment '
  'is authoritative.';


-- ============================================================================
-- ROLLBACK
--   comment on column public.users.phone_verified_at is null;
--   comment on column public.users.email_verified_at is null;
--   comment on view public.investor_metrics is null;
-- ============================================================================
