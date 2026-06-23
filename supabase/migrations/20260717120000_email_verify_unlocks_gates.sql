-- Email verification must satisfy the legacy phone_verified_at gates.
--
-- The app migrated phone -> email auth in 20260716120000_email_auth.sql, which added
-- public.users.email_verified_at ("the auth gate flag") and a sync trigger — but it
-- never updated the RLS policies / RPCs that gate verified actions. Those still check
-- public.users.phone_verified_at, which an email-only user NEVER has. Result: an
-- email-verified host hits `create_event` and Postgres denies the INSERT via
-- "events_insert_self_phone_verified" (42501 -> HTTP 403). The same dead gate blocks
-- event_requests inserts, propose_intro (social_graph_lana_tools), the
-- auth_is_phone_verified() helper, and spares email users from the anonymous reaper
-- only by accident.
--
-- Rather than rewrite every policy + large RPC body (and risk discovering the misses
-- one 403 at a time), we bridge at the data layer: a confirmed email stamps
-- phone_verified_at. Nothing in the email-auth world sets phone_verified_at, so this
-- has no conflict — it simply makes "verified by any supported method" true for every
-- existing gate at once. phone_verified_at now reads as "verified" (phone OR email).

-- 1) Going forward: when an email is confirmed, also stamp phone_verified_at.
create or replace function public.sync_email_verified()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, auth
as $$
begin
  if new.email is distinct from old.email
     or new.email_confirmed_at is distinct from old.email_confirmed_at then
    update public.users
    set email = nullif(trim(lower(coalesce(new.email::text, ''))), ''),
        email_verified_at = new.email_confirmed_at,
        -- Legacy verification gates (events insert, intros, reaper, etc.) still read
        -- phone_verified_at; a confirmed email counts as verified. coalesce so we never
        -- clear an existing phone verification when email is merely changed.
        phone_verified_at = coalesce(phone_verified_at, new.email_confirmed_at),
        updated_at = now()
    where id = new.id;
  end if;
  return new;
end;
$$;
revoke execute on function public.sync_email_verified() from public, anon, authenticated;

-- 2) Backfill existing email-verified users who confirmed before this change.
update public.users
set phone_verified_at = email_verified_at,
    updated_at = now()
where phone_verified_at is null
  and email_verified_at is not null;
