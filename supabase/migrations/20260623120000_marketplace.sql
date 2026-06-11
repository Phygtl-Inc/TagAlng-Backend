-- Marketplace (Path 3): list free/swap items -> inquiry chat -> handoff -> swap.
-- v0.1: NO selling (free/swap only). Completion never auto-promotes tier; the only
-- marketplace->ladder path is the mutual "Stay in touch" opt-in (ATPR invariant 16),
-- which promotes a stranger pair to 'acquaintance' and opens a fresh shielded chat.
-- Inquiry chats reuse chat_threads (kind='inquiry'); 1:1 block rules already apply.

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

create type public.item_intent as enum ('free', 'swap');  -- no 'sell' in v0.1
create type public.marketplace_item_status as enum ('active', 'reserved', 'sold', 'removed', 'expired');
create type public.inquiry_status as enum ('open', 'committed', 'completed', 'closed', 'expired');

-- ---------------------------------------------------------------------------
-- marketplace_items
-- ---------------------------------------------------------------------------

create table if not exists public.marketplace_items (
  id uuid primary key default gen_random_uuid(),
  seller uuid not null references public.users (id) on delete cascade,
  block_id text references public.blocks (id),
  title text not null check (char_length(title) between 1 and 80),
  description text check (description is null or char_length(description) <= 500),
  category text,
  intent_type public.item_intent not null default 'free',
  photos jsonb not null default '[]'::jsonb,
  status public.marketplace_item_status not null default 'active',
  reserved_for uuid references public.users (id) on delete set null,
  expires_at timestamptz not null default (now() + interval '30 days'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.marketplace_items is
  'Free/swap listings (no selling in v0.1). Block-scoped neighbor marketplace.';

create index if not exists marketplace_items_block_status_idx
  on public.marketplace_items (block_id, status, created_at desc);
create index if not exists marketplace_items_seller_idx
  on public.marketplace_items (seller, created_at desc);

create trigger marketplace_items_updated_at
  before update on public.marketplace_items
  for each row execute function public.set_updated_at();

alter table public.marketplace_items enable row level security;

create policy "marketplace_items_select_active_or_own"
  on public.marketplace_items for select
  to authenticated
  using (status = 'active' or seller = auth.uid());

create policy "marketplace_items_no_client_write"
  on public.marketplace_items for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- inquiries (+ link to the chat thread)
-- ---------------------------------------------------------------------------

create table if not exists public.inquiries (
  id uuid primary key default gen_random_uuid(),
  item_id uuid not null references public.marketplace_items (id) on delete cascade,
  inquirer uuid not null references public.users (id) on delete cascade,
  seller uuid not null references public.users (id) on delete cascade,
  thread_id uuid references public.chat_threads (id) on delete set null,
  status public.inquiry_status not null default 'open',
  opening_text text,
  handoff_when timestamptz,
  handoff_where text,
  handoff_lat double precision,
  handoff_lng double precision,
  committed_at timestamptz,
  completed_at timestamptz,
  closed_at timestamptz,
  closed_by uuid references public.users (id) on delete set null,
  expires_at timestamptz not null default (now() + interval '14 days'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint inquiries_distinct check (inquirer <> seller)
);

comment on table public.inquiries is
  'A buyer/swapper conversation about an item. Reuses a chat_threads(kind=inquiry) thread.';

-- One open inquiry per (inquirer, item).
create unique index if not exists inquiries_open_uniq
  on public.inquiries (inquirer, item_id)
  where status = 'open';

create index if not exists inquiries_seller_idx on public.inquiries (seller, created_at desc);
create index if not exists inquiries_inquirer_idx on public.inquiries (inquirer, created_at desc);
create index if not exists inquiries_expiry_idx on public.inquiries (expires_at)
  where status in ('open', 'committed');

create trigger inquiries_updated_at
  before update on public.inquiries
  for each row execute function public.set_updated_at();

alter table public.inquiries enable row level security;

create policy "inquiries_select_parties"
  on public.inquiries for select
  to authenticated
  using (inquirer = auth.uid() or seller = auth.uid());

create policy "inquiries_no_client_write"
  on public.inquiries for all
  to authenticated
  using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Bilateral sub-tables
-- ---------------------------------------------------------------------------

create table if not exists public.inquiry_handoff_confirmations (
  inquiry_id uuid not null references public.inquiries (id) on delete cascade,
  user_id uuid not null references public.users (id) on delete cascade,
  handoff_when timestamptz,
  handoff_where text,
  handoff_lat double precision,
  handoff_lng double precision,
  confirmed_at timestamptz not null default now(),
  primary key (inquiry_id, user_id)
);

create table if not exists public.inquiry_completion_confirmations (
  inquiry_id uuid not null references public.inquiries (id) on delete cascade,
  user_id uuid not null references public.users (id) on delete cascade,
  confirmed_at timestamptz not null default now(),
  primary key (inquiry_id, user_id)
);

create table if not exists public.inquiry_tier_promotions (
  inquiry_id uuid not null references public.inquiries (id) on delete cascade,
  user_id uuid not null references public.users (id) on delete cascade,
  consented_at timestamptz not null default now(),
  primary key (inquiry_id, user_id)
);

alter table public.inquiry_handoff_confirmations enable row level security;
alter table public.inquiry_completion_confirmations enable row level security;
alter table public.inquiry_tier_promotions enable row level security;

-- Parties of the inquiry may read the sub-tables; no client writes.
create policy "ihc_select_parties" on public.inquiry_handoff_confirmations for select to authenticated
  using (exists (select 1 from public.inquiries i where i.id = inquiry_id and (i.inquirer = auth.uid() or i.seller = auth.uid())));
create policy "ihc_no_client_write" on public.inquiry_handoff_confirmations for all to authenticated using (false) with check (false);

create policy "icc_select_parties" on public.inquiry_completion_confirmations for select to authenticated
  using (exists (select 1 from public.inquiries i where i.id = inquiry_id and (i.inquirer = auth.uid() or i.seller = auth.uid())));
create policy "icc_no_client_write" on public.inquiry_completion_confirmations for all to authenticated using (false) with check (false);

create policy "itp_select_parties" on public.inquiry_tier_promotions for select to authenticated
  using (exists (select 1 from public.inquiries i where i.id = inquiry_id and (i.inquirer = auth.uid() or i.seller = auth.uid())));
create policy "itp_no_client_write" on public.inquiry_tier_promotions for all to authenticated using (false) with check (false);

-- ---------------------------------------------------------------------------
-- Extend promote_relationship_tier: inquiry_mutual_opt_in -> acquaintance.
-- (Faithful copy of the 20260619 definition + the new trigger/target case.)
-- Per invariant 16, this is the ONLY inquiry_* trigger accepted.
-- ---------------------------------------------------------------------------

create or replace function public.promote_relationship_tier(
  p_other_user_id uuid,
  p_trigger text,
  p_proof_id uuid default null
)
returns public.relationship_tier
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_low uuid;
  v_high uuid;
  v_current public.relationship_tier := 'stranger';
  v_target public.relationship_tier;
  v_new public.relationship_tier;
begin
  if v_me is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if p_other_user_id is null or p_other_user_id = v_me then
    raise exception 'invalid_other_user' using errcode = 'P0001';
  end if;

  if p_trigger not in (
    'nudge_sent', 'nudge_accepted', 'intro_accepted', 'rsvp_attended_same_event',
    'unmask_accepted', 'inquiry_mutual_opt_in'
  ) then
    raise exception 'invalid_tier_trigger' using errcode = 'P0001';
  end if;

  v_target := case p_trigger
    when 'nudge_sent' then 'nudge'
    when 'nudge_accepted' then 'acquaintance'
    when 'intro_accepted' then 'acquaintance'
    when 'rsvp_attended_same_event' then 'acquaintance'
    when 'unmask_accepted' then 'direct'
    when 'inquiry_mutual_opt_in' then 'acquaintance'
  end;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, p_other_user_id);

  select ur.tier into v_current
  from public.user_relationships ur
  where ur.user_low = v_low and ur.user_high = v_high;

  v_current := coalesce(v_current, 'stranger');
  v_new := public._tier_max(v_current, v_target);

  if v_new = v_current then
    return v_current;
  end if;

  insert into public.user_relationships (user_low, user_high, tier, last_transition_at, last_trigger)
  values (v_low, v_high, v_new, now(), p_trigger)
  on conflict (user_low, user_high) do update
  set tier = excluded.tier,
      last_transition_at = excluded.last_transition_at,
      last_trigger = excluded.last_trigger;

  insert into public.relationship_tier_events (
    user_low, user_high, viewer_user_id, from_tier, to_tier, trigger_event, proof_id
  )
  values (v_low, v_high, v_me, v_current, v_new, p_trigger, p_proof_id);

  return v_new;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: list_marketplace_item
-- ---------------------------------------------------------------------------

create or replace function public.list_marketplace_item(
  p_title text,
  p_description text default null,
  p_intent_type text default 'free',
  p_category text default null,
  p_photos jsonb default '[]'::jsonb,
  p_block_id text default null
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_block text;
  v_id uuid;
begin
  perform public._require_verified_neighbor_comms();

  if p_title is null or char_length(trim(p_title)) < 1 then
    raise exception 'title_required' using errcode = 'P0001';
  end if;
  if p_intent_type not in ('free', 'swap') then
    raise exception 'selling_not_allowed_v01' using errcode = 'P0001';  -- no 'sell'
  end if;

  v_block := coalesce(p_block_id, (select home_block_id from public.users where id = v_me));

  insert into public.marketplace_items (seller, block_id, title, description, category, intent_type, photos)
  values (v_me, v_block, trim(p_title), p_description, p_category, p_intent_type::public.item_intent, coalesce(p_photos, '[]'::jsonb))
  returning id into v_id;

  return v_id;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: get_marketplace_items (browse active listings on a block)
-- ---------------------------------------------------------------------------

create or replace function public.get_marketplace_items(
  p_block_id text default null,
  p_limit int default 50
)
returns table (
  item_id uuid,
  seller_id uuid,
  seller_nickname text,
  title text,
  description text,
  category text,
  intent_type public.item_intent,
  photos jsonb,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    i.id, i.seller, u.nickname, i.title, i.description, i.category, i.intent_type, i.photos, i.created_at
  from public.marketplace_items i
  join public.users u on u.id = i.seller
  where i.status = 'active'
    and i.block_id = coalesce(p_block_id, (select home_block_id from public.users where id = auth.uid()))
    and not public.lana_is_blocked(auth.uid(), i.seller)
  order by i.created_at desc
  limit greatest(1, least(coalesce(p_limit, 50), 100));
$$;

-- ---------------------------------------------------------------------------
-- RPC: create_inquiry (opens the inquiry + a chat thread + opening message)
-- ---------------------------------------------------------------------------

create or replace function public.create_inquiry(
  p_item_id uuid,
  p_opening_text text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_seller uuid;
  v_status public.marketplace_item_status;
  v_low uuid;
  v_high uuid;
  v_thread uuid;
  v_inquiry uuid;
begin
  perform public._require_verified_neighbor_comms();

  select seller, status into v_seller, v_status
  from public.marketplace_items where id = p_item_id;

  if v_seller is null then
    raise exception 'item_not_found' using errcode = 'P0001';
  end if;
  if v_status <> 'active' then
    raise exception 'item_not_active' using errcode = 'P0001';
  end if;
  if v_seller = v_me then
    raise exception 'cannot_inquire_own_item' using errcode = 'P0001';
  end if;
  if public.lana_is_blocked(v_me, v_seller) then
    raise exception 'blocked' using errcode = 'P0001';
  end if;

  -- One open inquiry per (inquirer, item) — friendly check before creating a thread.
  if exists (
    select 1 from public.inquiries
    where inquirer = v_me and item_id = p_item_id and status = 'open'
  ) then
    raise exception 'inquiry_already_open' using errcode = 'P0001';
  end if;

  select user_low, user_high into v_low, v_high
  from public._relationship_pair(v_me, v_seller);

  -- New inquiry chat thread (multiple inquiry threads per pair are allowed).
  insert into public.chat_threads (kind, user_low, user_high, created_by)
  values ('inquiry', v_low, v_high, v_me)
  returning id into v_thread;

  insert into public.chat_thread_members (thread_id, user_id)
  values (v_thread, v_me), (v_thread, v_seller)
  on conflict (thread_id, user_id) do nothing;

  -- The unique-open index raises on a duplicate open inquiry for the pair+item.
  insert into public.inquiries (item_id, inquirer, seller, thread_id, status, opening_text)
  values (p_item_id, v_me, v_seller, v_thread, 'open', nullif(trim(p_opening_text), ''))
  returning id into v_inquiry;

  -- Inquirer's opening message + a Lana mediation note.
  if p_opening_text is not null and char_length(trim(p_opening_text)) > 0 then
    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_thread, v_me, 'text', trim(p_opening_text));
  end if;

  insert into public.messages (thread_id, sender_id, kind, content)
  values (v_thread, null, 'lana',
    'You''re connected about this item. Agree on a time and place, then both tap "confirm handoff".');

  update public.chat_threads set last_message_at = now() where id = v_thread;

  return jsonb_build_object('inquiry_id', v_inquiry, 'thread_id', v_thread);
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: confirm_handoff (both parties agree on time + place)
-- ---------------------------------------------------------------------------

create or replace function public.confirm_handoff(
  p_inquiry_id uuid,
  p_when timestamptz,
  p_where text,
  p_lat double precision default null,
  p_lng double precision default null
)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_inq record;
  v_both boolean;
begin
  perform public._require_verified_neighbor_comms();

  select * into v_inq from public.inquiries where id = p_inquiry_id;
  if v_inq is null or (v_me <> v_inq.inquirer and v_me <> v_inq.seller) then
    raise exception 'inquiry_not_found' using errcode = 'P0001';
  end if;
  if v_inq.status <> 'open' then
    raise exception 'inquiry_not_open' using errcode = 'P0001';
  end if;

  insert into public.inquiry_handoff_confirmations (inquiry_id, user_id, handoff_when, handoff_where, handoff_lat, handoff_lng)
  values (p_inquiry_id, v_me, p_when, p_where, p_lat, p_lng)
  on conflict (inquiry_id, user_id) do update
  set handoff_when = excluded.handoff_when, handoff_where = excluded.handoff_where,
      handoff_lat = excluded.handoff_lat, handoff_lng = excluded.handoff_lng, confirmed_at = now();

  select count(distinct user_id) >= 2 into v_both
  from public.inquiry_handoff_confirmations where inquiry_id = p_inquiry_id;

  if v_both then
    update public.inquiries
    set status = 'committed', committed_at = now(),
        handoff_when = p_when, handoff_where = p_where, handoff_lat = p_lat, handoff_lng = p_lng
    where id = p_inquiry_id;

    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_inq.thread_id, null, 'lana', 'Handoff confirmed by both of you. See you there!');
    update public.chat_threads set last_message_at = now() where id = v_inq.thread_id;

    return 'committed';
  end if;

  return 'one_side';
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: confirm_completion (both confirm the swap happened -> item sold)
-- ---------------------------------------------------------------------------

create or replace function public.confirm_completion(p_inquiry_id uuid)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_inq record;
  v_both boolean;
begin
  perform public._require_verified_neighbor_comms();

  select * into v_inq from public.inquiries where id = p_inquiry_id;
  if v_inq is null or (v_me <> v_inq.inquirer and v_me <> v_inq.seller) then
    raise exception 'inquiry_not_found' using errcode = 'P0001';
  end if;
  if v_inq.status <> 'committed' then
    raise exception 'inquiry_not_committed' using errcode = 'P0001';
  end if;

  insert into public.inquiry_completion_confirmations (inquiry_id, user_id)
  values (p_inquiry_id, v_me)
  on conflict (inquiry_id, user_id) do nothing;

  select count(distinct user_id) >= 2 into v_both
  from public.inquiry_completion_confirmations where inquiry_id = p_inquiry_id;

  if v_both then
    update public.inquiries set status = 'completed', completed_at = now() where id = p_inquiry_id;
    update public.marketplace_items set status = 'sold' where id = v_inq.item_id;

    insert into public.messages (thread_id, sender_id, kind, content)
    values (v_inq.thread_id, null, 'lana',
      'Swap complete! If you''d both like to stay in touch, tap "Stay in touch".');
    update public.chat_threads set last_message_at = now() where id = v_inq.thread_id;

    return 'completed';
  end if;

  return 'one_side';
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: mark_acquaintance_from_inquiry (the "Stay in touch" mutual opt-in)
-- ---------------------------------------------------------------------------

create or replace function public.mark_acquaintance_from_inquiry(
  p_inquiry_id uuid,
  p_consent boolean default true
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_inq record;
  v_other uuid;
  v_both boolean;
  v_tier public.relationship_tier;
  v_new_chat uuid;
begin
  perform public._require_verified_neighbor_comms();

  select * into v_inq from public.inquiries where id = p_inquiry_id;
  if v_inq is null or (v_me <> v_inq.inquirer and v_me <> v_inq.seller) then
    raise exception 'inquiry_not_found' using errcode = 'P0001';
  end if;
  if v_inq.status <> 'completed' then
    raise exception 'inquiry_not_completed' using errcode = 'P0001';
  end if;

  if not p_consent then
    return jsonb_build_object('status', 'skipped');
  end if;

  v_other := case when v_me = v_inq.inquirer then v_inq.seller else v_inq.inquirer end;

  insert into public.inquiry_tier_promotions (inquiry_id, user_id)
  values (p_inquiry_id, v_me)
  on conflict (inquiry_id, user_id) do nothing;

  select count(distinct user_id) >= 2 into v_both
  from public.inquiry_tier_promotions where inquiry_id = p_inquiry_id;

  if not v_both then
    return jsonb_build_object('status', 'recorded_waiting_other');
  end if;

  -- Both consented. Only promote if they are still strangers (invariant 7/16);
  -- if already connected, do not change tier or open a new chat.
  v_tier := public.get_relationship_tier(v_other);
  if v_tier <> 'stranger' then
    return jsonb_build_object('status', 'already_connected');
  end if;

  perform public.promote_relationship_tier(v_other, 'inquiry_mutual_opt_in', p_inquiry_id);
  v_new_chat := public._open_relationship_thread(v_me, v_other);

  return jsonb_build_object('status', 'both_consented_promoted', 'new_chat_id', v_new_chat);
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: close_inquiry (either party)
-- ---------------------------------------------------------------------------

create or replace function public.close_inquiry(p_inquiry_id uuid, p_reason text default null)
returns void
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_me uuid := auth.uid();
  v_inq record;
begin
  perform public._require_verified_neighbor_comms();

  select * into v_inq from public.inquiries where id = p_inquiry_id;
  if v_inq is null or (v_me <> v_inq.inquirer and v_me <> v_inq.seller) then
    raise exception 'inquiry_not_found' using errcode = 'P0001';
  end if;
  if v_inq.status in ('completed', 'closed', 'expired') then
    raise exception 'inquiry_already_finalized' using errcode = 'P0001';
  end if;

  update public.inquiries
  set status = 'closed', closed_at = now(), closed_by = v_me
  where id = p_inquiry_id;

  if v_inq.thread_id is not null then
    update public.chat_threads set archived_at = now() where id = v_inq.thread_id;
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- RPC: get_my_inquiries (as buyer or seller)
-- ---------------------------------------------------------------------------

create or replace function public.get_my_inquiries()
returns table (
  inquiry_id uuid,
  thread_id uuid,
  item_id uuid,
  item_title text,
  role text,
  other_user_id uuid,
  other_nickname text,
  status public.inquiry_status,
  last_message_at timestamptz,
  created_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    i.id, i.thread_id, i.item_id, mi.title,
    case when i.inquirer = auth.uid() then 'buyer' else 'seller' end,
    case when i.inquirer = auth.uid() then i.seller else i.inquirer end,
    ou.nickname,
    i.status, ct.last_message_at, i.created_at
  from public.inquiries i
  join public.marketplace_items mi on mi.id = i.item_id
  left join public.chat_threads ct on ct.id = i.thread_id
  join public.users ou on ou.id = case when i.inquirer = auth.uid() then i.seller else i.inquirer end
  where (i.inquirer = auth.uid() or i.seller = auth.uid())
    and not public.lana_is_blocked(auth.uid(), case when i.inquirer = auth.uid() then i.seller else i.inquirer end)
  order by ct.last_message_at desc nulls last, i.created_at desc;
$$;

-- ---------------------------------------------------------------------------
-- Cron: expire stale inquiries (schedule daily; service_role)
-- ---------------------------------------------------------------------------

create or replace function public.expire_inquiries()
returns int
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_count int;
begin
  update public.inquiries
  set status = 'expired'
  where status in ('open', 'committed')
    and expires_at < now();
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- Realtime: parties observe inquiry status changes (guarded + idempotent).
-- ---------------------------------------------------------------------------

do $$
declare
  v_tbl text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach v_tbl in array array['marketplace_items', 'inquiries'] loop
      if not exists (
        select 1 from pg_publication_tables
        where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = v_tbl
      ) then
        execute format('alter publication supabase_realtime add table public.%I', v_tbl);
      end if;
    end loop;
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------

revoke all on function public.promote_relationship_tier(uuid, text, uuid) from public, anon;
grant execute on function public.promote_relationship_tier(uuid, text, uuid) to authenticated;

revoke all on function public.list_marketplace_item(text, text, text, text, jsonb, text) from public, anon;
grant execute on function public.list_marketplace_item(text, text, text, text, jsonb, text) to authenticated;

revoke all on function public.get_marketplace_items(text, int) from public, anon;
grant execute on function public.get_marketplace_items(text, int) to authenticated;

revoke all on function public.create_inquiry(uuid, text) from public, anon;
grant execute on function public.create_inquiry(uuid, text) to authenticated;

revoke all on function public.confirm_handoff(uuid, timestamptz, text, double precision, double precision) from public, anon;
grant execute on function public.confirm_handoff(uuid, timestamptz, text, double precision, double precision) to authenticated;

revoke all on function public.confirm_completion(uuid) from public, anon;
grant execute on function public.confirm_completion(uuid) to authenticated;

revoke all on function public.mark_acquaintance_from_inquiry(uuid, boolean) from public, anon;
grant execute on function public.mark_acquaintance_from_inquiry(uuid, boolean) to authenticated;

revoke all on function public.close_inquiry(uuid, text) from public, anon;
grant execute on function public.close_inquiry(uuid, text) to authenticated;

revoke all on function public.get_my_inquiries() from public, anon;
grant execute on function public.get_my_inquiries() to authenticated;

revoke all on function public.expire_inquiries() from public, anon, authenticated;
grant execute on function public.expire_inquiries() to service_role;
