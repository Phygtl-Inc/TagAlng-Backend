-- Fix: handle_new_user for email-only auth users (no phone on auth.users).
-- If creation still fails, apply 20260606130000_users_phone_nullable.sql (phone was NOT NULL on dev).

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
