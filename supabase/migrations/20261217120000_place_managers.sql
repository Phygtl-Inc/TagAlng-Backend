-- Who may act for a community — plural, and survivable.
--
-- places.claimed_by is one uuid. So the pastor who claimed St Mark's IS St Mark's, and
-- when he leaves there is no one who can hand it over and no record that anyone else was
-- ever involved. A community is not a username; it has people in it, and they must not
-- lose it because one person moved away.
--
-- This is cheap now and effectively un-retrofittable later: once claims exist, deciding
-- retroactively who else "was really" an operator is a judgement call per row.
--
-- claimed_by STAYS. It is read in several places and is a useful pointer to the primary
-- operator. It is not dropped here; deprecate it once every reader goes through this table.

create table if not exists public.place_managers (
  id                  uuid primary key default gen_random_uuid(),
  place_id            uuid not null references public.places(id) on delete cascade,
  user_id             uuid not null references public.users(id) on delete cascade,

  -- operator: verified authority for the entity. manager: delegated by an operator.
  -- A manager may act; only an operator may add or remove other managers.
  role                text not null default 'operator'
                        check (role in ('operator','manager')),

  verification_method text,
  verified_at         timestamptz,
  added_by            uuid references public.users(id),
  removed_at          timestamptz,
  created_at          timestamptz not null default now(),

  unique (place_id, user_id)
);

create index if not exists place_managers_place_idx
  on public.place_managers(place_id) where removed_at is null;
create index if not exists place_managers_user_idx
  on public.place_managers(user_id) where removed_at is null;

comment on table public.place_managers is
  'People who may act for a community. Replaces the single places.claimed_by pointer for '
  'authority questions. INVARIANT: a place that has ever had an operator must always have '
  'at least one live operator — removing the last one is rejected (trigger below), because '
  'a community with members and no one who can act for it cannot be recovered without '
  'support intervention.';

-- ---------------------------------------------------------------------------
-- Backfill from the existing single pointer. Idempotent.
-- ---------------------------------------------------------------------------
insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
select p.id, p.claimed_by, 'operator', 'backfill_claimed_by', p.claimed_at, p.claimed_by
from public.places p
where p.claimed_by is not null
on conflict (place_id, user_id) do nothing;

-- ---------------------------------------------------------------------------
-- The last operator cannot be removed.
--
-- Fires on the UPDATE that sets removed_at, and on DELETE. It deliberately does NOT fire
-- when the place itself is being deleted (cascade) — that is not an orphaning, it is a
-- removal of the whole thing.
-- ---------------------------------------------------------------------------
create or replace function public.place_managers_keep_one_operator()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_place uuid := coalesce(new.place_id, old.place_id);
  v_left  int;
begin
  -- Place is gone: nothing to orphan.
  if not exists (select 1 from public.places where id = v_place) then
    return coalesce(new, old);
  end if;

  select count(*) into v_left
  from public.place_managers m
  where m.place_id = v_place
    and m.role = 'operator'
    and m.removed_at is null
    and m.id <> coalesce(new.id, old.id);

  if v_left = 0 then
    raise exception 'last_operator_cannot_be_removed'
      using errcode = 'P0001',
            hint = 'Add another operator first, then remove this one.';
  end if;

  return coalesce(new, old);
end;
$$;

drop trigger if exists place_managers_keep_one_operator_upd on public.place_managers;
create trigger place_managers_keep_one_operator_upd
  before update of removed_at on public.place_managers
  for each row
  when (old.removed_at is null and new.removed_at is not null and old.role = 'operator')
  execute function public.place_managers_keep_one_operator();

drop trigger if exists place_managers_keep_one_operator_del on public.place_managers;
create trigger place_managers_keep_one_operator_del
  before delete on public.place_managers
  for each row
  when (old.role = 'operator' and old.removed_at is null)
  execute function public.place_managers_keep_one_operator();

-- ---------------------------------------------------------------------------
-- RLS: a manager may see the roster of a place they manage. Writes are service-role only
-- (the claim path and the operator-invite path both run in the worker).
-- ---------------------------------------------------------------------------
alter table public.place_managers enable row level security;

drop policy if exists place_managers_self_read on public.place_managers;
create policy place_managers_self_read on public.place_managers
  for select to authenticated
  using (
    user_id = auth.uid()
    or exists (
      select 1 from public.place_managers m2
      where m2.place_id = place_managers.place_id
        and m2.user_id = auth.uid()
        and m2.removed_at is null
    )
  );

-- ============================================================================
-- ROLLBACK
--   drop trigger if exists place_managers_keep_one_operator_del on public.place_managers;
--   drop trigger if exists place_managers_keep_one_operator_upd on public.place_managers;
--   drop function if exists public.place_managers_keep_one_operator();
--   drop table if exists public.place_managers;
--   claimed_by was never modified, so nothing else has to be undone.
-- ============================================================================
