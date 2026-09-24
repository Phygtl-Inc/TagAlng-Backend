-- The external audience a community brings with it.
--
-- The second kind of evidence from 20261214120000. Geography says a community is
-- somewhere; this says it already has people elsewhere. Either, both, or neither.
--
-- NEVER IDENTIFY AN ACCOUNT BY ITS URL OR USERNAME. Both change, and both change
-- silently — a creator rebrands, and the row we hold now points at whoever took the old
-- handle. provider_account_id is the immutable id the platform issues; it is nullable
-- only because the backlink method (20261218120000) can prove control of a profile
-- without the platform telling us its internal id.

create table if not exists public.external_community_identities (
  id                    uuid primary key default gen_random_uuid(),
  place_id              uuid not null references public.places(id) on delete cascade,

  provider              text not null check (provider in (
                          'instagram','youtube','tiktok','reddit','x','twitch',
                          'linktree','newsletter','podcast','website','other')),
  -- Immutable where the provider gives us one (OAuth). Null when ownership was proven
  -- by backlink, which proves control of a PROFILE without revealing an internal id.
  provider_account_id   text,
  username              text not null,
  canonical_url         text not null,

  ownership_method      text not null check (ownership_method in (
                          'profile_backlink','platform_oauth','admin_approval')),
  ownership_verified_at timestamptz,
  -- What we actually fetched and matched. A claim that we "saw the link" with nothing
  -- retained is not evidence, and this is the column an audit will ask for first.
  backlink_url_seen     text,
  -- Exists so scheduled re-checking can be added later without a migration. Nothing
  -- reads it yet; that is deliberate (revalidation is out of scope — see the PR).
  last_checked_at       timestamptz,

  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),

  constraint eci_verified_has_method check (
    ownership_verified_at is null or ownership_method is not null)
);

-- One external account belongs to exactly one community. Two partial indexes rather than
-- one, because the identifying column differs by method: OAuth gives an immutable id and
-- that is authoritative; backlink has only the username, which is weaker but is still the
-- thing that must not be claimed twice.
create unique index if not exists eci_provider_account_uniq
  on public.external_community_identities(provider, provider_account_id)
  where provider_account_id is not null;

create unique index if not exists eci_provider_username_uniq
  on public.external_community_identities(provider, lower(username))
  where provider_account_id is null;

create index if not exists eci_place_idx
  on public.external_community_identities(place_id);

comment on table public.external_community_identities is
  'External surfaces a community controls (Instagram, YouTube, a newsletter, a site). '
  'The audience half of a community''s evidence — geography is the other half. A row here '
  'never implies popularity and carries no metrics: it asserts only that this community '
  'demonstrably controls that account.';

comment on column public.external_community_identities.provider_account_id is
  'The platform''s immutable account id. Null when ownership was proven by backlink. '
  'NEVER identify an account by username or URL — both change silently.';

comment on column public.external_community_identities.backlink_url_seen is
  'The exact matched string from the fetched profile. Retained as evidence: "we saw it" '
  'without a record is not a verification.';

-- Nothing client-side writes here; the worker holds service role.
alter table public.external_community_identities enable row level security;

drop policy if exists eci_owner_read on public.external_community_identities;
create policy eci_owner_read on public.external_community_identities
  for select to authenticated
  using (
    exists (
      select 1 from public.places p
      where p.id = external_community_identities.place_id
        and p.claimed_by = auth.uid()
    )
  );

-- ============================================================================
-- APPLICATION RULE (not a CHECK — a CHECK cannot count siblings)
--   Maximum 5 identities per place_id. Enforce at the call site in claim_backlink.py.
--   A cap exists so one claimant cannot farm handles by attaching twenty accounts.
--
-- ROLLBACK
--   drop table if exists public.external_community_identities;
-- ============================================================================
