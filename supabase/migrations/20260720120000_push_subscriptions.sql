-- Web Push subscriptions: one row per browser/device the user enabled notifications on.
-- The FE subscribes via the service worker's PushManager and upserts the subscription
-- here; the lana-worker reads them (service role) and sends pushes with pywebpush + VAPID.

create table if not exists public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users (id) on delete cascade,
  endpoint text not null unique,           -- the push service URL (identifies the device)
  p256dh text not null,                     -- client public key (payload encryption)
  auth text not null,                       -- client auth secret (payload encryption)
  user_agent text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_push_subscriptions_user
  on public.push_subscriptions (user_id);

alter table public.push_subscriptions enable row level security;

create policy push_subscriptions_select_own on public.push_subscriptions
  for select to authenticated using (user_id = auth.uid());
create policy push_subscriptions_insert_own on public.push_subscriptions
  for insert to authenticated with check (user_id = auth.uid());
create policy push_subscriptions_update_own on public.push_subscriptions
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy push_subscriptions_delete_own on public.push_subscriptions
  for delete to authenticated using (user_id = auth.uid());

-- Upsert by endpoint (the same device re-subscribing rotates its keys, not a new row).
create or replace function public.upsert_push_subscription(
  p_endpoint text,
  p_p256dh text,
  p_auth text,
  p_user_agent text default null
)
returns void
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  insert into public.push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
  values (auth.uid(), p_endpoint, p_p256dh, p_auth, p_user_agent)
  on conflict (endpoint) do update
    set user_id = excluded.user_id,
        p256dh = excluded.p256dh,
        auth = excluded.auth,
        user_agent = excluded.user_agent,
        updated_at = now();
end;
$$;

revoke execute on function public.upsert_push_subscription(text, text, text, text) from public, anon;
grant execute on function public.upsert_push_subscription(text, text, text, text) to authenticated;

-- Drop a subscription (FE calls on unsubscribe / 410 Gone cleanup).
create or replace function public.delete_push_subscription(p_endpoint text)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  delete from public.push_subscriptions
  where endpoint = p_endpoint and user_id = auth.uid();
$$;

revoke execute on function public.delete_push_subscription(text) from public, anon;
grant execute on function public.delete_push_subscription(text) to authenticated;
