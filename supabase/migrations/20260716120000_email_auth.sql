-- Email OTP auth: collect an email (instead of phone) at the Lana verify gate.
-- Adds the email columns + the auth.users → public.users sync, mirroring the
-- existing phone_verified plumbing (20260529000000). Phone is already nullable
-- (20260606130000), so nothing to relax here.

-- 1. Columns
alter table public.users
  add column if not exists email text,
  add column if not exists email_verified_at timestamptz;

comment on column public.users.email is
  'From auth email OTP / email-change confirmation; lower-cased. Null for phone-only accounts.';
comment on column public.users.email_verified_at is
  'Mirror of auth.users.email_confirmed_at — set by trg_sync_email_verified. The auth gate flag.';

-- 2. One verified account per email (case-insensitive). Partial: phone-only rows
--    leave email null and are exempt.
create unique index if not exists users_email_lower_idx
  on public.users (lower(email))
  where email is not null;

-- 3. Capture email on the initial auth.users insert (email/password admins,
--    future email-first signups). Anonymous users have no email yet — synced on
--    confirmation by the trigger below.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_phone text;
  v_email text;
begin
  v_phone := nullif(trim(coalesce(new.phone::text, '')), '');
  v_email := nullif(trim(lower(coalesce(new.email::text, ''))), '');

  insert into public.users (id, phone, email)
  values (new.id, v_phone, v_email)
  on conflict (id) do update
    set updated_at = now();

  return new;
end;
$$;
comment on function public.handle_new_user() is
  'After auth.users insert: ensure public.users row. Works for phone OTP, email OTP, and email/password admins.';

-- 4. Sync email + verified timestamp when the auth user confirms / changes email.
--    This is what flips a guest (anonymous → permanent) to verified after the
--    email-change OTP, and keeps public.users.email in step for login lookups.
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
        updated_at = now()
    where id = new.id;
  end if;
  return new;
end;
$$;
revoke execute on function public.sync_email_verified() from public, anon, authenticated;

drop trigger if exists trg_sync_email_verified on auth.users;
create trigger trg_sync_email_verified
after update on auth.users
for each row execute function public.sync_email_verified();
