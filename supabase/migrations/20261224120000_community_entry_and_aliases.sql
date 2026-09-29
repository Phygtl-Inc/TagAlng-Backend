-- Community entry context · handle aliases · blurb as identity
--
-- WHY (2026-09-29)
--
--   All 11 creator communities in production have exactly ONE affiliation: the creator.
--   Nobody has ever arrived through a creator link and stayed. The flow has never
--   completed end to end, once.
--
--   Three things are missing for it to work, and this migration adds all three.
--
--   1. WE DO NOT RECORD WHERE SOMEONE CAME FROM.
--      The frontend already joins and sets the search scope. But when a visitor bounces
--      before joining, or the join returns no affiliation, the arrival leaves no trace at
--      all. A creator therefore cannot be shown "40 people opened your link, 3 joined" —
--      and that ratio is the only number that tells them whether the link is working.
--
--   2. A RENAMED HANDLE BREAKS EVERY LINK ALREADY IN A BIO.
--      Eight handles are live and wrong-shaped (`zenaidyndb-community` is Etiqueta do
--      Reino; `test-7`; `asjid-test-6`). They have to be fixable, and a creator who has
--      already put a link in their profile must not have it die when we fix it.
--
--   3. A COMMUNITY'S IDENTITY DEPENDS ON IT ALREADY HAVING MEMBERS.
--      discover_communities_semantic scores a community by its MEMBERS' identity claims.
--      Etiqueta do Reino has one public claim in total. So a new community is
--      undiscoverable at exactly the moment the creator needs it found. `blurb` already
--      describes each community well ("a spot focused on social and dining etiquette")
--      and is derived from the community itself — it just has no embedding.

-- ── 1 · blurb becomes a first-class, matchable identity ─────────────────────

alter table public.places
  add column if not exists blurb_embedding extensions.vector(768),
  -- Set when `name` changes. The blurb is DERIVED from the name, so renaming a community
  -- and leaving the old blurb in place means Lana's first question describes a community
  -- that no longer exists under that name.
  add column if not exists blurb_stale boolean not null default false,
  -- One free rename (§6.3 of the contract). Non-null means it has been used.
  add column if not exists handle_renamed_at timestamptz;

comment on column public.places.blurb_embedding is
  'Embedding of blurb. Lets a community be discovered on its OWN description rather than '
  'on its members'' claims — which is the only thing that works on day one, when it has '
  'no members. Refreshed by the worker whenever blurb_stale is true.';

comment on column public.places.blurb_stale is
  'blurb no longer matches the place. Set by trigger on name change; cleared by the worker '
  'after regeneration. A stale blurb is worse than none: it grounds Lana''s first question.';

create index if not exists places_blurb_embedding_hnsw
  on public.places using hnsw (blurb_embedding extensions.vector_cosine_ops)
  where blurb_embedding is not null;

create index if not exists places_blurb_stale_idx
  on public.places (blurb_stale) where blurb_stale;

-- Renaming a community invalidates its description. One trigger, so no caller can forget.
create or replace function public.places_mark_blurb_stale()
returns trigger
language plpgsql
as $$
begin
  if new.name is distinct from old.name then
    new.blurb_stale := true;
  end if;
  return new;
end;
$$;

drop trigger if exists places_blurb_stale_on_rename on public.places;
create trigger places_blurb_stale_on_rename
  before update of name on public.places
  for each row execute function public.places_mark_blurb_stale();

-- ── 2 · handle aliases · a rename must not kill a link already in a bio ─────

create table if not exists public.place_handle_aliases (
  handle      text primary key
                check (handle ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
                       and length(handle) between 3 and 48),
  place_id    uuid not null references public.places(id) on delete cascade,
  retired_at  timestamptz not null default now(),
  retired_by  uuid references public.users(id) on delete set null
);

create index if not exists place_handle_aliases_place_idx
  on public.place_handle_aliases(place_id);

comment on table public.place_handle_aliases is
  'Handles a place used to have. resolve_place_handle falls through to here, so every '
  'link already printed, posted or put in a bio keeps working forever. An alias is never '
  'reissued to a different place — that would silently redirect somebody else''s audience.';

-- An alias must never collide with a live handle, in either direction.
create or replace function public.place_handle_alias_not_live()
returns trigger
language plpgsql
as $$
begin
  if exists (select 1 from public.places p where p.handle = new.handle) then
    raise exception 'handle_in_use' using hint = 'That handle is live on a place.';
  end if;
  return new;
end;
$$;

drop trigger if exists place_handle_aliases_not_live on public.place_handle_aliases;
create trigger place_handle_aliases_not_live
  before insert or update on public.place_handle_aliases
  for each row execute function public.place_handle_alias_not_live();

alter table public.place_handle_aliases enable row level security;
-- Deliberately no policy. Everything reaches this table through security definer
-- functions, matching places / place_claims / circle_affiliations.

-- ── 3 · entry events · who arrived, from where, and did they convert ────────

create table if not exists public.community_entry_events (
  id           uuid primary key default gen_random_uuid(),
  place_id     uuid not null references public.places(id) on delete cascade,
  user_id      uuid references public.users(id) on delete set null,
  -- How they got here. 'creator_link' is the only one that changes Lana's behaviour, and
  -- it is precisely the one currently thrown away.
  entry_kind   text not null default 'creator_link'
                 check (entry_kind in ('creator_link','discovery','switch','geo','invite')),
  handle_used  text,
  session_id   uuid references public.lana_sessions(id) on delete set null,
  was_anonymous boolean not null default true,
  -- Set when they actually joined. Null forever = a visit that never converted, which is
  -- the number a creator most needs and the one we cannot currently produce.
  joined_at    timestamptz,
  created_at   timestamptz not null default now()
);

create index if not exists community_entry_events_place_idx
  on public.community_entry_events(place_id, created_at desc);
create index if not exists community_entry_events_user_idx
  on public.community_entry_events(user_id) where user_id is not null;
-- The conversion funnel query: arrivals that never joined.
create index if not exists community_entry_events_unconverted_idx
  on public.community_entry_events(place_id) where joined_at is null;

comment on table public.community_entry_events is
  'One row per arrival at a community, whether or not it became a membership. This is the '
  'creator''s real dashboard: opens vs joins. Separate from circle_affiliations on purpose '
  '— an affiliation is a relationship, an entry is an event, and conflating them is why '
  'a bounce currently leaves no trace.';

alter table public.community_entry_events enable row level security;

-- ── 4 · resolve_place_handle · follow aliases, carry identity ───────────────
--
-- CHANGES, all additive to the payload:
--   · falls through to place_handle_aliases, returning the CURRENT handle so the client
--     can canonicalise the URL
--   · returns blurb + hqCity so the landing page and Lana's first question read from one
--     source instead of a second round trip
--   · returns the creator block the client is already wired for but never receives
--     (frontend backend-asks §56b: PlaceCommunity.creatorName / creatorAvatarUrl exist
--     client-side and are always null today)

create or replace function public.resolve_place_handle(p_handle text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
declare
  v_in      text := public.normalize_place_handle(p_handle);
  v_place   record;
  v_alias   boolean := false;
  v_creator record;
begin
  select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
         p.governance_state, p.blurb, p.hq_city, p.claimed_by
    into v_place
    from public.places p
   where p.handle = v_in
     and p.governance_state = 'operator_verified';

  -- Retired handle: same place, canonical handle returned so the caller can redirect.
  if v_place.id is null then
    select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
           p.governance_state, p.blurb, p.hq_city, p.claimed_by
      into v_place
      from public.place_handle_aliases a
      join public.places p on p.id = a.place_id
     where a.handle = v_in
       and p.governance_state = 'operator_verified';
    v_alias := v_place.id is not null;
  end if;

  if v_place.id is null then
    return null;
  end if;

  -- The operator, for the card head. Only for creator communities: on a gym or a church
  -- the operator is staff, and naming them turns a place page into a personal profile.
  if v_place.place_type = 'creator' then
    select u.id, u.nickname, u.profile_photo_url
      into v_creator
      from public.place_managers m
      join public.users u on u.id = m.user_id
     where m.place_id = v_place.id
       and m.role = 'operator'
       and m.removed_at is null
     order by m.created_at asc
     limit 1;
  end if;

  return jsonb_build_object(
    'placeId',          v_place.id,
    'handle',           v_place.handle,
    'displayName',      v_place.name,
    'placeType',        v_place.place_type,
    'zip',              v_place.zip,
    'firstAction',      v_place.first_action,
    'governanceState',  v_place.governance_state,
    'operatorVerified', true,
    'blurb',            v_place.blurb,
    'hqCity',           v_place.hq_city,
    -- True when the caller arrived on a retired handle. The client should replace the URL
    -- with `handle` above so the old one stops spreading.
    'viaAlias',         v_alias,
    'creator',          case
                          when v_creator.id is null then null
                          else jsonb_build_object(
                                 'displayName', v_creator.nickname,
                                 'avatarUrl',   v_creator.profile_photo_url)
                        end);
end;
$$;

comment on function public.resolve_place_handle(text) is
  'Public head for a handle URL. Publication stays gated on governance_state = '
  '''operator_verified'' — an unverified place must never resolve, because an answer '
  'engine that ingests a wrong place page cannot be made to un-ingest it. Now also '
  'follows retired handles and carries blurb + operator so the landing page and Lana''s '
  'opening read the same row.';

-- ============================================================================
-- ROLLBACK
--   drop trigger if exists place_handle_aliases_not_live on public.place_handle_aliases;
--   drop trigger if exists places_blurb_stale_on_rename on public.places;
--   drop function if exists public.place_handle_alias_not_live();
--   drop function if exists public.places_mark_blurb_stale();
--   drop table if exists public.community_entry_events;
--   drop table if exists public.place_handle_aliases;
--   alter table public.places drop column if exists blurb_embedding,
--     drop column if exists blurb_stale, drop column if exists handle_renamed_at;
--   -- resolve_place_handle: restore the prior body from git history.
-- ============================================================================
