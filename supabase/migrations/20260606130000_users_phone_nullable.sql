-- Email/password admins (and any user without phone) need public.users.phone nullable.
-- Remote tagalng-dev had phone NOT NULL, which broke Dashboard user creation.

alter table public.users
  alter column phone drop not null;
comment on column public.users.phone is
  'From auth phone OTP when present; null for email-only accounts (e.g. ops admin).';
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
