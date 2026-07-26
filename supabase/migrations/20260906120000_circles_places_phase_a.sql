-- Lana · Circles + Place Profile · Phase A (additive schema only)
-- Source specs: LANA_CIRCLES_ZIP_MASTER_v1 (Appendix A) + Place Profile extension (§2, §8 Phase A),
-- adapted to this repo: place_ref-native from day one (no legacy inline place columns on
-- circle_affiliations — that table is net-new here), events.place_id text is the only real
-- legacy and is backfilled below. Embeddings are 768-dim (text-embedding-005), matching
-- user_identity_claims. No behavior change in this migration; nothing reads these yet.
--
-- Repo deltas vs the spec, deliberate:
--   · spec's `alter table zips` → net-new public.zip_unlock (no zips table exists;
--     zip_centroids is a seeded market registry and home_zip may be any ZIP5).
--   · spec's `alter table waitlist_invites` → net-new public.circle_invites (+ redemptions);
--     no waitlist_invites table exists.
--   · places.place_type is nullable/advisory (open question O6: canonical type on the place,
--     the user's own framing stays on their affiliation's circle_type).
--   · place_features.sub_group is not null default '' so the (place_id, key, sub_group)
--     uniqueness is a plain constraint (expressions are not allowed in table constraints).
--   · grounded is a generated column (place_ref is not null) — cannot drift.
-- All new tables: RLS enabled with NO client policies. Access is worker/service-role or
-- security-definer RPCs only — this is the server-side disclosure guarantee (§F): PostgREST
-- must never hand place names to clients directly.

-- ── places · canonical place entity (one row per Google place) ─────────────────
create table if not exists public.places (
  id               uuid primary key default gen_random_uuid(),
  google_place_id  text not null unique,
  name             text not null,
  place_type       text check (place_type is null or place_type in
                     ('school','faith','fitness','kids_activity','neighborhood',
                      'hobby','support','heritage','friends','other')),
  lat              double precision,
  lng              double precision,
  address          text,
  zip              text check (zip is null or zip ~ '^\d{5}$'),
  h3               text,
  features_jsonb   jsonb not null default '{}'::jsonb,
  claimed_by       uuid references public.users (id),
  claimed_at       timestamptz,
  source           text not null default 'user_grounded'
                     check (source in ('user_grounded','owner_claimed','import')),
  created_by       uuid references public.users (id),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
comment on table public.places is
  'Canonical place profile keyed by Google place id. Affiliations, events, and (Phase 2) an '
  'owner point at it. Name/address/geo are tier-gated (§F) — never expose below Direct; '
  'clients get places data only through worker serializers, never PostgREST.';
comment on column public.places.place_type is
  'Advisory canonical type (O6). The user''s own framing lives on circle_affiliations.circle_type.';
comment on column public.places.claimed_by is
  'Phase 2 owner-claim. Invariant for Phase 1: always null, no endpoint writes it.';

create index if not exists places_place_type_idx on public.places (place_type);
create index if not exists places_zip_idx on public.places (zip);
create index if not exists places_claimed_by_idx on public.places (claimed_by)
  where claimed_by is not null;

create trigger places_updated_at
before update on public.places
for each row execute function public.set_updated_at();

alter table public.places enable row level security;

-- ── place_features · learned, shared attributes of a place ─────────────────────
create table if not exists public.place_features (
  id             uuid primary key default gen_random_uuid(),
  place_id       uuid not null references public.places (id) on delete cascade,
  key            text not null check (key ~ '^[a-z][a-z0-9_]{1,63}$'),
  value          text,
  sub_group      text not null default '',
  confidence     real not null default 0.6 check (confidence >= 0 and confidence <= 1),
  source         text not null default 'rapport'
                   check (source in ('rapport','owner','import','inferred')),
  contributed_by uuid references public.users (id),
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (place_id, key, sub_group)
);
comment on table public.place_features is
  'Objective place attributes (has_pool, has_childcare, …) volunteered in conversation and '
  'reused for every member. Write policy: overwrite only when new confidence >= stored; '
  'source=''owner'' rows are never overwritten by ''rapport'' writes (enforced in worker).';
comment on column public.place_features.sub_group is
  'Program/room inside the place (aerobics, toddler swim). '''' = the place itself; a '
  'sub-group is never its own places row (O1).';

create index if not exists place_features_place_id_idx on public.place_features (place_id);
create index if not exists place_features_key_idx on public.place_features (key);

create trigger place_features_updated_at
before update on public.place_features
for each row execute function public.set_updated_at();

alter table public.place_features enable row level security;

-- ── circle_affiliations · a user's real-world communities (the spine) ──────────
create table if not exists public.circle_affiliations (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references public.users (id) on delete cascade,
  circle_type  text not null check (circle_type in
                 ('school','faith','fitness','kids_activity','neighborhood',
                  'hobby','support','heritage','friends','other')),
  circle_key   text not null check (circle_key ~ '^[a-z][a-z0-9_]{1,63}$'),
  place_ref    uuid references public.places (id),
  grounded     boolean generated always as (place_ref is not null) stored,
  status       text not null default 'suggested'
                 check (status in ('suggested','confirmed')),
  detail       text,
  source       text not null
                 check (source in ('chat_extraction','invite_confirmed','profile_add')),
  invited_by   uuid references public.users (id),
  confidence   real not null default 0.6 check (confidence >= 0 and confidence <= 1),
  embedding    extensions.vector(768),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  dismissed_at timestamptz
);
comment on table public.circle_affiliations is
  'A user''s communities, optionally grounded to a canonical place. Only status=''confirmed'' '
  'and dismissed_at is null rows are ever matched on (§C). Removal is soft-delete via '
  'dismissed_at (§G staleness is user-curated).';
comment on column public.circle_affiliations.circle_type is
  'The user''s own framing of the community — may differ from places.place_type (O6).';

create unique index if not exists circle_affiliations_user_key_active_idx
  on public.circle_affiliations (user_id, circle_key)
  where dismissed_at is null;
create index if not exists circle_affiliations_user_id_idx
  on public.circle_affiliations (user_id)
  where dismissed_at is null;
create index if not exists circle_affiliations_circle_type_idx
  on public.circle_affiliations (circle_type);
-- the hot member_count / onion read: confirmed, active members of a place
create index if not exists circle_affiliations_place_confirmed_idx
  on public.circle_affiliations (place_ref)
  where status = 'confirmed' and dismissed_at is null and place_ref is not null;
create index if not exists circle_affiliations_embedding_hnsw_idx
  on public.circle_affiliations
  using hnsw (embedding extensions.vector_cosine_ops)
  where dismissed_at is null and embedding is not null;

create trigger circle_affiliations_updated_at
before update on public.circle_affiliations
for each row execute function public.set_updated_at();

alter table public.circle_affiliations enable row level security;

-- ── user_identity_claims · place-tagged personal affinities (§4.3) ─────────────
-- "What do you enjoy most at {place}?" → the answer is a normal affinity claim,
-- attributable to the place. Shape of the onion's shared_affinities input is unchanged.
alter table public.user_identity_claims
  add column if not exists place_ref uuid references public.places (id);
comment on column public.user_identity_claims.place_ref is
  'Optional: the canonical place this affinity is about (§4.3 personal place-affinity). '
  'Place name stays tier-gated even when the affinity itself informs matching.';
create index if not exists user_identity_claims_place_ref_idx
  on public.user_identity_claims (place_ref)
  where place_ref is not null and dismissed_at is null;

-- ── rapport_gaps · place-tagged enrichment questions (§4.3) ────────────────────
-- The post-grounding "What do you enjoy most at {place}?" gap carries the place it
-- is about, so the claim made from its answer can be stamped with the same tag.
alter table public.rapport_gaps
  add column if not exists place_ref uuid references public.places (id);

-- ── events · optional anchor to a canonical place ──────────────────────────────
alter table public.events
  add column if not exists place_ref uuid references public.places (id);
comment on column public.events.place_ref is
  'Canonical place anchor. Optional — place-free events keep freeform venue fields. '
  'Dual-written with legacy events.place_id (text) for one release; reads prefer place_ref.';
create index if not exists events_place_ref_idx
  on public.events (place_ref)
  where place_ref is not null;

-- Backfill: seed places from existing event venues (the repo's only legacy place data),
-- then stamp place_ref on those events. Freshest event wins the imported name/coords.
insert into public.places (google_place_id, name, lat, lng, address, source, created_by)
select distinct on (e.place_id)
       e.place_id,
       coalesce(nullif(e.venue_name, ''), 'Unnamed place'),
       case when e.location is not null then extensions.st_y(e.location::extensions.geometry) end,
       case when e.location is not null then extensions.st_x(e.location::extensions.geometry) end,
       nullif(e.venue_address, ''),
       'import',
       e.host_id
  from public.events e
 where e.place_id is not null and e.place_id <> ''
 order by e.place_id, e.created_at desc
on conflict (google_place_id) do nothing;

update public.events e
   set place_ref = p.id
  from public.places p
 where p.google_place_id = e.place_id
   and e.place_ref is null;

-- ── zip_unlock · ZIP-level unlock state machine (§D) · net-new ──────────────────
-- Rows are created on demand (first verified user in a ZIP). Gates consumption of
-- others' supply only — never creation (§D.2 capability matrix).
create table if not exists public.zip_unlock (
  zip5                  text primary key check (zip5 ~ '^\d{5}$'),
  unlock_state          text not null default 'closed'
                          check (unlock_state in ('closed','warming','open')),
  verified_active_count int not null default 0,
  unlock_threshold      int not null default 10,   -- U1 · tune per market
  opened_at             timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
comment on table public.zip_unlock is
  'closed → warming → open per ZIP5, driven by verified_active_count (phone-verified + '
  'active users only, §I anti-gaming). Recount/transition logic lands with the invites PR; '
  'this migration is structure only.';

create trigger zip_unlock_updated_at
before update on public.zip_unlock
for each row execute function public.set_updated_at();

alter table public.zip_unlock enable row level security;

-- ── users · founding recognition (§E.2) + growth attribution ───────────────────
alter table public.users
  add column if not exists founding_area      text,
  add column if not exists founding_earned_at timestamptz,
  add column if not exists invited_by         uuid references public.users (id);
comment on column public.users.founding_area is
  'Founding member recognition (§E) — earned by quality (verified + >=1 confirmed circle/'
  'intro during warming), never invite volume. Copy is persona-neutral: "Founding member".';
comment on column public.users.invited_by is
  'Growth attribution set once at invite redemption (§A.2) — unconditional, and deliberately '
  'NOT circle membership. Contribution count derives from it.';
create index if not exists users_invited_by_idx on public.users (invited_by)
  where invited_by is not null;

-- ── circle_invites + redemptions · labeled invite links (§A.2) · net-new ───────
create table if not exists public.circle_invites (
  id            uuid primary key default gen_random_uuid(),
  token         text not null unique,
  owner_user_id uuid not null references public.users (id) on delete cascade,
  circle_type   text check (circle_type is null or circle_type in
                  ('school','faith','fitness','kids_activity','neighborhood',
                   'hobby','support','heritage','friends','other')),
  circle_key    text,
  place_ref     uuid references public.places (id),
  created_at    timestamptz not null default now(),
  revoked_at    timestamptz
);
comment on table public.circle_invites is
  'Labeled invite links (/i/<token>). The circle hint drives only the GENERIC self-confirm '
  'prompt ("part of a faith community nearby?") — it is never shown to the joiner and never '
  'auto-writes membership (§A.2 invite != membership; forwarded links stay harmless).';

create index if not exists circle_invites_owner_idx
  on public.circle_invites (owner_user_id);

alter table public.circle_invites enable row level security;

create table if not exists public.circle_invite_redemptions (
  id         uuid primary key default gen_random_uuid(),
  invite_id  uuid not null references public.circle_invites (id) on delete cascade,
  user_id    uuid not null references public.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  unique (invite_id, user_id)
);
comment on table public.circle_invite_redemptions is
  'One row per (invite, joiner) — the growth edge + rate-limit substrate (§I.1). '
  'Redemption sets users.invited_by if not already set; membership requires self-grounding.';

create index if not exists circle_invite_redemptions_invite_idx
  on public.circle_invite_redemptions (invite_id);

alter table public.circle_invite_redemptions enable row level security;

-- Recount + transition (§D.4), atomic in SQL. Worker-called (service role only) from
-- the redeem and area-progress paths — read-repair, no cron needed at pilot scale.
-- "Verified active" (U2): verified by any method (email verify stamps phone_verified_at,
-- see 20260717) + a Lana session in 30 days + ≥1 confirmed thing (circle or accepted intro).
create or replace function public.recount_zip_unlock(p_zip5 text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_zip text;
  v_count int;
  v_threshold int;
  v_old text;
  v_new text;
begin
  v_zip := public.normalize_zip5(p_zip5);
  if v_zip is null then
    raise exception 'invalid_zip' using errcode = 'P0001';
  end if;

  select count(*) into v_count
  from public.users u
  where u.home_zip = v_zip
    and u.phone_verified_at is not null
    and exists (
      select 1 from public.lana_sessions s
      where s.user_id = u.id
        and s.created_at > now() - interval '30 days'
    )
    and (
      exists (
        select 1 from public.circle_affiliations a
        where a.user_id = u.id and a.status = 'confirmed' and a.dismissed_at is null
      )
      or exists (
        select 1 from public.intros i
        where (i.initiator_id = u.id or i.candidate_id = u.id)
          and i.status = 'accepted'
      )
    );

  insert into public.zip_unlock (zip5, verified_active_count)
  values (v_zip, v_count)
  on conflict (zip5) do nothing;

  select unlock_state, unlock_threshold into v_old, v_threshold
  from public.zip_unlock where zip5 = v_zip for update;

  v_new := case
    when v_count = 0 then 'closed'
    when v_count < v_threshold then 'warming'
    else 'open'
  end;

  update public.zip_unlock
     set verified_active_count = v_count,
         unlock_state = v_new,
         opened_at = case when v_new = 'open' and v_old <> 'open' then now()
                          else opened_at end
   where zip5 = v_zip;

  if v_new = 'open' and v_old <> 'open' then
    -- Founding recognition (§E.2 / U4): stamped once, earned by quality (verified +
    -- ≥1 confirmed thing), never by invite volume. Persona-neutral copy owns the label.
    update public.users u
       set founding_area = v_zip,
           founding_earned_at = now()
     where u.home_zip = v_zip
       and u.founding_area is null
       and u.phone_verified_at is not null
       and (
         exists (
           select 1 from public.circle_affiliations a
           where a.user_id = u.id and a.status = 'confirmed' and a.dismissed_at is null
         )
         or exists (
           select 1 from public.intros i
           where (i.initiator_id = u.id or i.candidate_id = u.id)
             and i.status = 'accepted'
         )
       );
  end if;

  return jsonb_build_object(
    'zip5', v_zip,
    'state', v_new,
    'previous_state', v_old,
    'count', v_count,
    'threshold', v_threshold,
    'opened', (v_new = 'open' and v_old <> 'open')
  );
end;
$$;
revoke all on function public.recount_zip_unlock(text) from public, anon, authenticated;
