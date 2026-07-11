-- User language preference (i18n) — reuses the existing users.locale column
-- (single source of truth; it already drives event title/description
-- translations and the profile dashboard).
--
-- locale: the language Lana OPENS conversations in (ISO 639-1 short code,
-- optional region subtag). Was check-constrained to en/pt/es; Lana now speaks
-- any language (AI-rendered replies), so the check relaxes to code shape.
-- Live per-turn detection still follows whatever language the user actually
-- writes — the preference only seeds the session and the greeting. Everything
-- Lana STORES (identity claims, drafts) stays English-canonical regardless.
--
-- lang_nudge_at: when Lana last offered "want me to make X your default?"
-- (the observed-vs-preferred divergence nudge). Cross-session cooldown so the
-- offer is never nagging; per-session once-ness lives in the session context.
--
-- Users update their own locale via the existing users_update_own policy
-- (the PWA settings drawer writes it directly).
alter table public.users
  drop constraint if exists users_locale_check;

alter table public.users
  add constraint users_locale_code
  check (locale ~ '^[a-z]{2,3}(-[a-z0-9]{2,8})?$');

alter table public.users
  add column if not exists lang_nudge_at timestamptz;
