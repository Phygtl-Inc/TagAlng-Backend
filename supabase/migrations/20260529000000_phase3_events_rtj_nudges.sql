-- TagAlng Phase 3: events + request-to-join + nudges + thread activity

-- 0011: users/cohorts locale extensions
alter table public.users
  add column if not exists profile_photo_url text,
  add column if not exists phone_verified_at timestamptz,
  add column if not exists founder_role text check (founder_role in ('internal', 'founding_member')),
  add column if not exists locale text not null default 'en'
    check (locale in ('en', 'pt', 'es'));

create index if not exists users_locale_idx on public.users (locale);

alter table public.cohorts
  add column if not exists label_pt text,
  add column if not exists label_es text;

create or replace function public.sync_phone_verified()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public, auth
as $$
begin
  if new.phone_confirmed_at is distinct from old.phone_confirmed_at then
    update public.users
    set phone_verified_at = new.phone_confirmed_at
    where id = new.id;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_sync_phone_verified on auth.users;
create trigger trg_sync_phone_verified
after update on auth.users
for each row execute function public.sync_phone_verified();

-- Allow self-edit for identity claim editor RPCs (worker still uses service_role)
drop policy if exists "identity_claims_update_own" on public.user_identity_claims;
create policy "identity_claims_update_own"
  on public.user_identity_claims for update
  to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- 0012: events
create table if not exists public.events (
  id uuid primary key default gen_random_uuid(),
  host_id uuid not null references public.users(id),
  cluster_id text not null default 'lake-nona',
  block_id text references public.blocks(id),
  title text not null check (char_length(title) between 1 and 80),
  description text check (description is null or char_length(description) <= 500),
  title_translations jsonb,
  description_translations jsonb,
  starts_at timestamptz not null,
  ends_at timestamptz,
  location extensions.geography(point, 4326),
  venue_name text,
  cohort_tags text[] not null default '{}',
  max_attendees integer check (max_attendees is null or (max_attendees > 0 and max_attendees <= 200)),
  auto_approve boolean not null default false,
  cover_image_url text,
  status text not null default 'open' check (status in ('open', 'cancelled', 'completed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists events_cluster_starts_idx on public.events (cluster_id, starts_at);
create index if not exists events_host_idx on public.events (host_id);
create index if not exists events_block_idx on public.events (block_id);
create index if not exists events_location_gix on public.events using gist (location);

alter table public.events enable row level security;

drop policy if exists "events_select_open_anyone" on public.events;
create policy "events_select_open_anyone"
  on public.events for select
  using (status = 'open');

drop policy if exists "events_select_host_all_status" on public.events;
create policy "events_select_host_all_status"
  on public.events for select
  to authenticated
  using (host_id = auth.uid());

-- attendee select policy is created after event_requests exists

drop policy if exists "events_insert_self_phone_verified" on public.events;
create policy "events_insert_self_phone_verified"
  on public.events for insert
  to authenticated
  with check (
    host_id = auth.uid()
    and exists (
      select 1
      from public.users u
      where u.id = auth.uid()
        and u.phone_verified_at is not null
    )
  );

drop policy if exists "events_update_host_only" on public.events;
create policy "events_update_host_only"
  on public.events for update
  to authenticated
  using (host_id = auth.uid())
  with check (host_id = auth.uid());

drop policy if exists "events_delete_host_only" on public.events;
create policy "events_delete_host_only"
  on public.events for delete
  to authenticated
  using (host_id = auth.uid());

drop trigger if exists trg_events_updated_at on public.events;
create trigger trg_events_updated_at
before update on public.events
for each row execute function public.set_updated_at();

-- 0015 (created early): thread events table used by event_requests triggers
create table if not exists public.thread_events (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.events(id) on delete cascade,
  actor_id uuid references public.users(id),
  event_type text not null check (event_type in (
    'request_sent', 'request_approved', 'request_declined', 'request_cancelled',
    'request_attended', 'request_changed',
    'host_update', 'event_updated', 'event_cancelled', 'check_in', 'reminder'
  )),
  payload jsonb not null default '{}',
  created_at timestamptz not null default now()
);

create index if not exists thread_events_event_time_idx
  on public.thread_events (event_id, created_at desc);

alter table public.thread_events enable row level security;

-- 0013: event_requests
create table if not exists public.event_requests (
  id uuid primary key default gen_random_uuid(),
  event_id uuid not null references public.events(id) on delete cascade,
  requester_id uuid not null references public.users(id) on delete cascade,
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'declined', 'cancelled', 'attended')),
  message text check (message is null or char_length(message) between 1 and 200),
  created_at timestamptz not null default now(),
  decided_at timestamptz,
  unique (event_id, requester_id)
);

create index if not exists event_requests_event_status_idx
  on public.event_requests (event_id, status);
create index if not exists event_requests_requester_idx
  on public.event_requests (requester_id);

alter table public.event_requests enable row level security;

drop policy if exists "er_select_self_or_host" on public.event_requests;
create policy "er_select_self_or_host"
  on public.event_requests for select
  to authenticated
  using (
    requester_id = auth.uid()
    or exists (
      select 1 from public.events e
      where e.id = event_id and e.host_id = auth.uid()
    )
  );

drop policy if exists "er_insert_self" on public.event_requests;
create policy "er_insert_self"
  on public.event_requests for insert
  to authenticated
  with check (
    requester_id = auth.uid()
    and exists (
      select 1
      from public.users u
      where u.id = auth.uid()
        and u.phone_verified_at is not null
    )
    and exists (
      select 1
      from public.events e
      where e.id = event_id
        and e.status = 'open'
    )
  );

drop policy if exists "er_update_host_or_self_cancel" on public.event_requests;
create policy "er_update_host_or_self_cancel"
  on public.event_requests for update
  to authenticated
  using (
    exists (
      select 1
      from public.events e
      where e.id = event_id
        and e.host_id = auth.uid()
    )
    or requester_id = auth.uid()
  )
  with check (
    exists (
      select 1
      from public.events e
      where e.id = event_id
        and e.host_id = auth.uid()
    )
    or (requester_id = auth.uid() and status = 'cancelled')
  );

drop policy if exists "events_select_approved_attendee" on public.events;
create policy "events_select_approved_attendee"
  on public.events for select
  to authenticated
  using (
    exists (
      select 1
      from public.event_requests r
      where r.event_id = events.id
        and r.requester_id = auth.uid()
        and r.status in ('approved', 'attended')
    )
  );

drop policy if exists "te_select_member" on public.thread_events;
create policy "te_select_member"
  on public.thread_events for select
  to authenticated
  using (
    exists (
      select 1
      from public.events e
      where e.id = event_id
        and e.host_id = auth.uid()
    )
    or exists (
      select 1
      from public.event_requests r
      where r.event_id = thread_events.event_id
        and r.requester_id = auth.uid()
        and r.status in ('approved', 'attended')
    )
  );

create or replace function public.set_event_request_decided_at()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  if old.status is distinct from new.status
     and new.status in ('approved', 'declined')
     and old.decided_at is null then
    new.decided_at := now();
  end if;
  return new;
end;
$$;

drop trigger if exists trg_event_request_set_decided_at on public.event_requests;
create trigger trg_event_request_set_decided_at
before update on public.event_requests
for each row execute function public.set_event_request_decided_at();

create or replace function public.log_event_request_change()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.thread_events (event_id, actor_id, event_type, payload)
    values (
      new.event_id,
      new.requester_id,
      'request_sent',
      jsonb_build_object('requester_id', new.requester_id, 'message', new.message)
    );
  elsif tg_op = 'UPDATE' and old.status is distinct from new.status then
    insert into public.thread_events (event_id, actor_id, event_type, payload)
    values (
      new.event_id,
      auth.uid(),
      case new.status
        when 'approved' then 'request_approved'
        when 'declined' then 'request_declined'
        when 'cancelled' then 'request_cancelled'
        when 'attended' then 'request_attended'
        else 'request_changed'
      end,
      jsonb_build_object('requester_id', new.requester_id, 'from', old.status, 'to', new.status)
    );
  end if;
  return coalesce(new, old);
end;
$$;

drop trigger if exists trg_event_request_change on public.event_requests;
create trigger trg_event_request_change
after insert or update on public.event_requests
for each row execute function public.log_event_request_change();

-- 0014: nudges
create table if not exists public.nudges (
  id uuid primary key default gen_random_uuid(),
  sender_id uuid not null references public.users(id) on delete cascade,
  recipient_id uuid not null references public.users(id) on delete cascade,
  sent_at timestamptz not null default now(),
  check (sender_id <> recipient_id)
);

create index if not exists nudges_recipient_time_idx
  on public.nudges (recipient_id, sent_at desc);
create index if not exists nudges_sender_time_idx
  on public.nudges (sender_id, sent_at desc);

alter table public.nudges enable row level security;

drop policy if exists "nudges_select_self" on public.nudges;
create policy "nudges_select_self"
  on public.nudges for select
  to authenticated
  using (sender_id = auth.uid() or recipient_id = auth.uid());

create or replace function public.enforce_nudge_limits()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
declare
  daily_count int;
  pair_recent int;
begin
  select count(*) into daily_count
  from public.nudges
  where sender_id = new.sender_id
    and sent_at > now() - interval '1 day';

  if daily_count >= 5 then
    raise exception 'nudge_rate_limit_daily' using errcode = 'P0001';
  end if;

  select count(*) into pair_recent
  from public.nudges
  where sender_id = new.sender_id
    and recipient_id = new.recipient_id
    and sent_at > now() - interval '7 days';

  if pair_recent > 0 then
    raise exception 'nudge_cooldown_pair' using errcode = 'P0001';
  end if;

  return new;
end;
$$;

drop trigger if exists trg_enforce_nudge_limits on public.nudges;
create trigger trg_enforce_nudge_limits
before insert on public.nudges
for each row execute function public.enforce_nudge_limits();

-- Core RPCs for v0.1 loop
create or replace function public.update_identity_claim_label(
  p_claim_id uuid,
  p_label text,
  p_synonyms text[] default null
)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.user_identity_claims
  set label = p_label,
      synonyms = coalesce(p_synonyms, synonyms),
      updated_at = now()
  where id = p_claim_id
    and user_id = auth.uid()
    and dismissed_at is null;
$$;

create or replace function public.update_identity_claim_disclosure(
  p_claim_id uuid,
  p_disclosure public.claim_disclosure
)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.user_identity_claims
  set disclosure = p_disclosure,
      updated_at = now()
  where id = p_claim_id
    and user_id = auth.uid()
    and dismissed_at is null;
$$;

create or replace function public.dismiss_identity_claim(p_claim_id uuid)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.user_identity_claims
  set dismissed_at = now(),
      updated_at = now()
  where id = p_claim_id
    and user_id = auth.uid()
    and dismissed_at is null;
$$;

create or replace function public.get_cluster_events(
  p_cluster_id text,
  p_window interval default '14 days',
  p_locale text default 'en'
)
returns table (
  id uuid,
  host_id uuid,
  title text,
  description text,
  starts_at timestamptz,
  ends_at timestamptz,
  location extensions.geography,
  venue_name text,
  cohort_tags text[],
  max_attendees integer,
  status text
)
language sql
security definer
set search_path = pg_catalog, public, extensions
stable
as $$
  select
    e.id,
    e.host_id,
    coalesce(e.title_translations->>p_locale, e.title) as title,
    coalesce(e.description_translations->>p_locale, e.description) as description,
    e.starts_at,
    e.ends_at,
    e.location,
    e.venue_name,
    e.cohort_tags,
    e.max_attendees,
    e.status
  from public.events e
  where e.cluster_id = p_cluster_id
    and e.status = 'open'
    and e.starts_at between now() and now() + p_window
  order by e.starts_at asc;
$$;

create or replace function public.request_to_join_event(
  p_event_id uuid,
  p_message text default null
)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  req_id uuid;
  host_uid uuid;
begin
  select e.host_id
  into host_uid
  from public.events e
  where e.id = p_event_id
    and e.status = 'open';

  if host_uid is null then
    raise exception 'event_not_open' using errcode = 'P0001';
  end if;

  if host_uid = auth.uid() then
    raise exception 'host_cannot_request_own_event' using errcode = 'P0001';
  end if;

  insert into public.event_requests (event_id, requester_id, message)
  values (p_event_id, auth.uid(), p_message)
  on conflict (event_id, requester_id) do nothing
  returning id into req_id;

  if req_id is null then
    raise exception 'request_already_exists' using errcode = 'P0001';
  end if;

  return req_id;
end;
$$;

create or replace function public.decide_event_request(
  p_request_id uuid,
  p_decision text
)
returns void
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if p_decision not in ('approved', 'declined') then
    raise exception 'invalid_decision' using errcode = 'P0001';
  end if;

  update public.event_requests
  set status = p_decision
  where id = p_request_id;

  if not found then
    raise exception 'request_not_found' using errcode = 'P0001';
  end if;
end;
$$;

create or replace function public.cancel_event_request(p_request_id uuid)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  update public.event_requests
  set status = 'cancelled'
  where id = p_request_id
    and requester_id = auth.uid();
$$;

create or replace function public.send_nudge(p_recipient_id uuid)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  nudge_id uuid;
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;

  insert into public.nudges (sender_id, recipient_id)
  values (auth.uid(), p_recipient_id)
  returning id into nudge_id;

  return nudge_id;
end;
$$;

create or replace function public.get_thread_events(p_event_id uuid)
returns setof public.thread_events
language sql
security invoker
set search_path = pg_catalog, public
stable
as $$
  select te.*
  from public.thread_events te
  where te.event_id = p_event_id
  order by te.created_at desc;
$$;

-- Grants
revoke execute on function public.update_identity_claim_label(uuid, text, text[]) from public, anon;
grant execute on function public.update_identity_claim_label(uuid, text, text[]) to authenticated;

revoke execute on function public.update_identity_claim_disclosure(uuid, public.claim_disclosure) from public, anon;
grant execute on function public.update_identity_claim_disclosure(uuid, public.claim_disclosure) to authenticated;

revoke execute on function public.dismiss_identity_claim(uuid) from public, anon;
grant execute on function public.dismiss_identity_claim(uuid) to authenticated;

revoke execute on function public.get_cluster_events(text, interval, text) from public;
grant execute on function public.get_cluster_events(text, interval, text) to anon, authenticated;

revoke execute on function public.request_to_join_event(uuid, text) from public, anon;
grant execute on function public.request_to_join_event(uuid, text) to authenticated;

revoke execute on function public.decide_event_request(uuid, text) from public, anon;
grant execute on function public.decide_event_request(uuid, text) to authenticated;

revoke execute on function public.cancel_event_request(uuid) from public, anon;
grant execute on function public.cancel_event_request(uuid) to authenticated;

revoke execute on function public.send_nudge(uuid) from public, anon;
grant execute on function public.send_nudge(uuid) to authenticated;

revoke execute on function public.get_thread_events(uuid) from public, anon;
grant execute on function public.get_thread_events(uuid) to authenticated;