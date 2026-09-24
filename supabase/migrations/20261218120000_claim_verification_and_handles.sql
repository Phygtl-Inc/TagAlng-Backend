-- Verification methods, and who may hold a single-word handle.
--
-- Three things, all on the claim path:
--   1. profile_backlink  — the creator method. The Lana link in their own profile.
--   2. A guard on domain_email so a free-mail address can never auto-verify a place.
--   3. Single-token handles, allowed only with a matching verified external identity.

-- ---------------------------------------------------------------------------
-- 1. Verification methods
--
-- profile_backlink: the creators page already instructs "Add Lana to Instagram, Reddit,
-- or your existing Linktree". That instruction IS the proof of control — only the account
-- owner can edit that profile — so verification costs the creator no extra step, and a
-- creator who will not put the link up has no funnel to verify anyway.
--
-- manual_founder: retroactive only. Closed-beta verifications made before any policy
-- existed (20261219120000 backfills them). New claims must never use it.
-- ---------------------------------------------------------------------------
alter table public.place_claims
  drop constraint if exists place_claims_verification_method_check;

alter table public.place_claims
  add constraint place_claims_verification_method_check check (
    verification_method is null or verification_method in (
      'domain_email',      -- places: double opt-in at the entity's own domain
      'profile_backlink',  -- creators: our link, in their profile, fetched anonymously
      'platform_oauth',    -- reserved; not implemented
      'admin_approval',
      'manual_review',
      'manual_founder'     -- RETROACTIVE ONLY. See 20261219120000
    )
  );

-- ---------------------------------------------------------------------------
-- 2. Free-mail domains never auto-verify
--
-- domain_email works because control of mail at an organisation's own domain implies
-- authority over that organisation. gmail.com implies nothing: anyone can hold an address
-- there, so "verified by email domain" on a free provider is verification of nothing.
-- Those claims are still accepted — they route to manual_review instead of auto-verifying.
-- ---------------------------------------------------------------------------
create table if not exists public.non_verifying_email_domains (
  domain     text primary key,
  reason     text not null default 'free_provider',
  created_at timestamptz not null default now()
);

insert into public.non_verifying_email_domains (domain) values
  ('gmail.com'),('googlemail.com'),('yahoo.com'),('yahoo.co.uk'),('ymail.com'),
  ('hotmail.com'),('hotmail.co.uk'),('outlook.com'),('live.com'),('msn.com'),
  ('icloud.com'),('me.com'),('mac.com'),('aol.com'),('protonmail.com'),('proton.me'),
  ('gmx.com'),('gmx.de'),('mail.com'),('zoho.com'),('yandex.com'),('yandex.ru'),
  ('tutanota.com'),('fastmail.com'),('hey.com'),('pm.me'),('duck.com'),
  ('uol.com.br'),('bol.com.br'),('terra.com.br'),('libero.it'),('virgilio.it')
on conflict (domain) do nothing;

comment on table public.non_verifying_email_domains is
  'Email domains that can never auto-verify a claim. Control of mail at a free provider '
  'implies nothing about authority over an organisation. A claim from one of these is '
  'accepted but routed to manual_review — it is not rejected.';

create or replace function public.email_domain_can_verify(p_domain text)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select p_domain is not null
     and length(btrim(p_domain)) > 0
     and not exists (
           select 1 from public.non_verifying_email_domains d
           where d.domain = lower(btrim(p_domain))
         );
$$;

revoke all on function public.email_domain_can_verify(text) from public, anon;
grant execute on function public.email_domain_can_verify(text) to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- 3. Single-token handles — option C
--
-- places_handle_format required at least one hyphen: '^[a-z0-9]+(-[a-z0-9]+)+$'. Good for
-- places ("a name and its place" — stmarks-orlando) and wrong for creators, whose own
-- page promises "one memorable handle".
--
-- Relaxing the format alone would put every short, valuable word in play at once, which
-- is the land rush. Option C instead: a single token is allowed ONLY when this community
-- holds a VERIFIED external identity whose username is that same token. Correspondence
-- does the gating — you may be `maya` if you can prove you are `maya` somewhere else.
--
-- protected_handles still applies on top, unconditionally. Proving you run an Instagram
-- called `nike` does not get you `nike`.
--
-- A CHECK cannot do this (cross-table), so it is a trigger.
-- ---------------------------------------------------------------------------
alter table public.places drop constraint if exists places_handle_format;
alter table public.places add constraint places_handle_format check (
  handle is null
  or (handle ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
      and length(handle) >= 3
      and length(handle) <= 48)
);

create or replace function public.places_single_token_handle_guard()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if new.handle is null then
    return new;
  end if;

  -- Compound handles are unchanged: always allowed.
  if position('-' in new.handle) > 0 then
    return new;
  end if;

  -- Single token: a verified external identity must carry the same username.
  if not exists (
    select 1
    from public.external_community_identities e
    where e.place_id = new.id
      and e.ownership_verified_at is not null
      and lower(e.username) = new.handle
  ) then
    raise exception 'single_token_handle_requires_matching_identity'
      using errcode = 'P0001',
            hint = 'Use a compound handle (run-with-maya), or verify an external account with this exact username first.';
  end if;

  return new;
end;
$$;

drop trigger if exists places_single_token_handle_guard_trg on public.places;
create trigger places_single_token_handle_guard_trg
  before insert or update of handle on public.places
  for each row
  when (new.handle is not null)
  execute function public.places_single_token_handle_guard();

comment on function public.places_single_token_handle_guard() is
  'Option C. A hyphen-free handle is allowed only when the community holds a verified '
  'external identity with that exact username. protected_handles applies on top and is '
  'not overridden by identity ownership.';

-- ---------------------------------------------------------------------------
-- 4. Provisional handles
--
-- A verified handle is not a permanent grant. It goes live provisionally; activation is
-- decided later by observed community activity, whose threshold is a CONFIG VALUE set
-- after the first cohort — deliberately not encoded here.
-- ---------------------------------------------------------------------------
alter table public.places
  add column if not exists handle_provisional_until timestamptz,
  add column if not exists handle_activated_at      timestamptz;

comment on column public.places.handle_provisional_until is
  'Verification grants a handle provisionally. Past this date with handle_activated_at '
  'still null, the handle reverts and the public route goes dark — the community, its '
  'members and its content are untouched. NO THRESHOLD IS ENCODED IN THE SCHEMA: what '
  'counts as activation is a config value set after the first cohort.';

-- ============================================================================
-- ROLLBACK
--   drop trigger if exists places_single_token_handle_guard_trg on public.places;
--   drop function if exists public.places_single_token_handle_guard();
--   alter table public.places drop constraint if exists places_handle_format;
--   alter table public.places add constraint places_handle_format check (
--     handle is null or (handle ~ '^[a-z0-9]+(-[a-z0-9]+)+$'
--                        and length(handle) between 3 and 48));
--     -- NOTE: only safe if no single-token handle has been issued.
--   alter table public.places drop column if exists handle_activated_at,
--                             drop column if exists handle_provisional_until;
--   drop function if exists public.email_domain_can_verify(text);
--   drop table if exists public.non_verifying_email_domains;
--   -- and restore the previous verification_method CHECK.
-- ============================================================================
