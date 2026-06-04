-- Fix: Dashboard email/password user creation fails with "Database error creating new user"
-- Cause: handle_new_user assumed phone auth; email-only admins have no phone on auth.users.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_phone text;
begin
  v_phone := nullif(trim(coalesce(new.phone::text, '')), '');

  insert into public.users (id, phone)
  values (new.id, v_phone)
  on conflict (id) do update
    set updated_at = now();

  return new;
end;
$$;
comment on function public.handle_new_user() is
  'After auth.users insert: ensure public.users row. Works for phone OTP and email/password admins.';
