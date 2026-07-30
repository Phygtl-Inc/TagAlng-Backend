-- ============================================================================
-- PR8 · Display-name + handle ("nickname") system                   (→ Asjid)
-- migration filename on push: supabase/migrations/20260917130000_nickname_system.sql
-- ----------------------------------------------------------------------------
-- PROBLEM (standup 2026-07-30):
--   Lana asks "tell me your name" → first name only → written to users.nickname.
--   There is no separate real-name field in use (users.full_name exists but is
--   populated on 0 of 31 rows), no uniqueness of any kind, and no addressable
--   identifier. Two people called "Maria" in the same feed are indistinguishable.
--   Tommaso: "this is a network, it wouldn't look good if we have a cryptic
--   name" + "it must be UNIQUE". Those two pull in opposite directions if you
--   put them on ONE column — so this PR splits them onto two.
--
-- DESIGN (full rationale in PR8_nickname_system.md):
--   users.nickname     = DISPLAY NAME. Warm, human, user's actual name.
--                        NOT unique. Never generated, never suffixed.
--                        (existing column · ~36 read sites unchanged)
--   users.handle       = NEW. Globally unique (case-insensitive), lowercase,
--                        addressable (@maria.oakst). Auto-derived from the
--                        display name. Secondary in every UI. NULLABLE.
--   users.full_name    = existing, unused. Real name. Private. Optional.
--                        Only ever used for a last initial + host trust.
--   users.about_you    = NEW. The "about you" paragraph. Lana drafts it from
--                        public identity claims; the user owns the final text.
--
--   Uniqueness scope = GLOBAL for handle, NONE for display name.
--   Same-name collisions inside one surface are solved at RENDER time by
--   public.disambiguate_display_names() ("Maria" / "Maria K."), never by
--   mutating a stored name into "Maria47".
--
-- REAL SCHEMA (verified against PROD kmetmatfxdkrialwrnzj on 2026-07-30):
--   users (26 cols): id, phone, nickname, home_block_id, created_at, updated_at,
--     profile_photo_url, phone_verified_at, founder_role, locale,
--     home_location_visibility, home_zip, full_name, consent_to_receive_intros,
--     notification_prefs, email, email_verified_at, referred_by, kids_count,
--     lang_nudge_at, voice_autoplay, founding_area, founding_earned_at,
--     invited_by, role, grammatical_gender
--   Indexes on users: users_pkey(id), users_home_block_id_idx,
--     users_locale_idx, users_email_lower_idx (UNIQUE lower(email)),
--     users_referred_by_idx, users_invited_by_idx.
--     → NO unique index on nickname today.
--   RLS on users: users_select_own (id = auth.uid()),
--                 users_update_own (id = auth.uid()) — i.e. a client can PATCH
--                 nickname directly today with zero validation.
--   NO rpc writes nickname (handle_new_user inserts id/phone/email/referred_by
--     only). The lana-worker writes users.nickname with the service role.
--   blocks (id, cluster_id, state, display_name, created_at, updated_at,
--     centroid) — display_name e.g. 'Foster City (94404)', 'Lake Nona — Area A'.
--   Extensions present: pg_stat_statements, pgcrypto, plpgsql, postgis,
--     supabase_vault, uuid-ossp, vector.
--     → NO citext, NO unaccent, NO pg_trgm. This PR adds none; transliteration
--       is done with an IMMUTABLE translate() so nothing new is installed.
--
-- SAFETY: purely additive + CREATE OR REPLACE. No column is dropped, renamed,
--   retyped or nulled. Every DDL is IF NOT EXISTS / guarded. Re-runnable.
--   The nickname CHECK is added NOT VALID (enforced on new writes, never
--   fails on legacy rows). Full ROLLBACK at the bottom.
-- VERIFIED: executed against PROD inside begin; ... rollback; on 2026-07-30.
-- ============================================================================

begin;

-- ─── 1) COLUMNS ─────────────────────────────────────────────────────────────

alter table public.users
  add column if not exists handle              text,
  add column if not exists handle_set_by       text,
  add column if not exists handle_set_at       timestamptz,
  add column if not exists display_name_source text,
  add column if not exists about_you           text,
  add column if not exists about_you_source    text,
  add column if not exists about_you_updated_at timestamptz,
  add column if not exists about_you_draft     text,
  add column if not exists about_you_draft_at  timestamptz;

comment on column public.users.nickname is
  'DISPLAY NAME. Warm, human, the name this person actually goes by. NOT unique — never auto-suffix it. Shown everywhere from Nudge tier up. See users.handle for the unique identifier.';
comment on column public.users.handle is
  'Globally unique, case-insensitive, lowercase addressable identifier (@maria.oakst). Derived from nickname; NULL is legal (non-Latin scripts, pre-migration rows) and simply means "not @-addressable yet". Never the primary label in UI.';
comment on column public.users.handle_set_by is
  'auto = generated by suggest_handles(); user = explicitly chosen/edited in profile.';
comment on column public.users.display_name_source is
  'lana = captured in conversation; user = edited in profile; migrated = backfilled by PR8.';
comment on column public.users.full_name is
  'Real name. PRIVATE by default: never returned by get_peer_profile / get_profile_summary. Used only for (a) a disambiguating last initial and (b) future host trust signals.';
comment on column public.users.about_you is
  'Short first-person blurb shown on the profile. Lana drafts it into about_you_draft; this column is only ever written with the user''s consent (about_you_source tells you which).';

-- ─── 2) FORMAT CONSTRAINTS ──────────────────────────────────────────────────
-- handle: 3–24 chars, starts with a letter, ends alphanumeric, single . or _
-- separators. Column is 100% NULL right now, so this can be added VALID.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'users_handle_format_chk') then
    alter table public.users
      add constraint users_handle_format_chk
      check (handle is null or handle ~ '^[a-z][a-z0-9]*([._][a-z0-9]+)*$'
             and length(handle) between 3 and 24);
  end if;

  if not exists (select 1 from pg_constraint where conname = 'users_handle_set_by_chk') then
    alter table public.users
      add constraint users_handle_set_by_chk
      check (handle_set_by is null or handle_set_by in ('auto','user'));
  end if;

  if not exists (select 1 from pg_constraint where conname = 'users_display_name_source_chk') then
    alter table public.users
      add constraint users_display_name_source_chk
      check (display_name_source is null or display_name_source in ('lana','user','migrated'));
  end if;

  if not exists (select 1 from pg_constraint where conname = 'users_about_you_source_chk') then
    alter table public.users
      add constraint users_about_you_source_chk
      check (about_you_source is null or about_you_source in ('lana','user'));
  end if;

  if not exists (select 1 from pg_constraint where conname = 'users_about_you_len_chk') then
    alter table public.users
      add constraint users_about_you_len_chk
      check (about_you is null or char_length(about_you) <= 280) not valid;
  end if;

  -- Display name: permissive on purpose (any script, spaces, apostrophes,
  -- hyphens allowed). Only blocks blanks, control chars and novel-length input.
  -- NOT VALID: enforced on every new write, never trips on the 7 legacy rows.
  if not exists (select 1 from pg_constraint where conname = 'users_nickname_shape_chk') then
    alter table public.users
      add constraint users_nickname_shape_chk
      check (
        nickname is null or (
          char_length(btrim(nickname)) between 1 and 40
          and nickname = btrim(nickname)
          and nickname !~ '[[:cntrl:]]'
        )
      ) not valid;
  end if;
end $$;

-- THE uniqueness guarantee. Partial so unlimited rows may sit at NULL.
create unique index if not exists users_handle_lower_uidx
  on public.users (lower(handle))
  where handle is not null;

-- ─── 3) RESERVED HANDLES ────────────────────────────────────────────────────
create table if not exists public.handle_reserved (
  handle     text primary key,
  reason     text not null default 'system',
  created_at timestamptz not null default now()
);

alter table public.handle_reserved enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='handle_reserved'
      and policyname='handle_reserved_read'
  ) then
    create policy handle_reserved_read on public.handle_reserved
      for select to authenticated, anon using (true);
  end if;
end $$;

insert into public.handle_reserved (handle, reason) values
  ('lana','brand'), ('tagalng','brand'), ('tagalong','brand'), ('phygtl','brand'),
  ('admin','system'), ('administrator','system'), ('root','system'), ('system','system'),
  ('support','system'), ('help','system'), ('staff','system'), ('team','system'),
  ('mod','system'), ('moderator','system'), ('official','system'), ('security','system'),
  ('api','route'), ('chat','route'), ('meet','route'), ('invite','route'),
  ('profile','route'), ('settings','route'), ('signin','route'), ('signup','route'),
  ('login','route'), ('logout','route'), ('new','route'), ('edit','route'),
  ('events','route'), ('event','route'), ('block','route'), ('blocks','route'),
  ('report','safety'), ('safety','safety'), ('abuse','safety'), ('privacy','legal'),
  ('terms','legal'), ('legal','legal'), ('billing','legal'), ('payment','legal'),
  ('everyone','impersonation'), ('here','impersonation'), ('all','impersonation'),
  ('anonymous','impersonation'), ('guest','impersonation'), ('neighbor','impersonation'),
  ('neighbour','impersonation'), ('null','impersonation'), ('undefined','impersonation'),
  ('notifications','route'), ('me','route'), ('you','route')
on conflict (handle) do nothing;

-- ─── 4) PURE HELPERS ────────────────────────────────────────────────────────

-- Transliterate → lowercase → strip to [a-z0-9]. Unmapped scripts collapse to
-- '' and that is a FEATURE: we leave handle NULL rather than invent something
-- cryptic for a person whose name is written in another script.
create or replace function public.lana_slugify(p_text text)
returns text
language sql
immutable
set search_path to 'pg_catalog'
as $fn$
  select nullif(
    regexp_replace(
      regexp_replace(
        translate(
          replace(replace(replace(replace(replace(
            lower(coalesce(p_text, '')),
            'ß', 'ss'), 'æ', 'ae'), 'œ', 'oe'), 'ø', 'o'), 'đ', 'd'),
          'àáâãäåèéêëìíîïòóôõöùúûüñçýÿšžłńśźżčřůěğı',
          'aaaaaaeeeeiiiiooooouuuuncyyszlnszzcruegi'),
        '[^a-z0-9]+', '', 'g'),
      '^[0-9]+', '', ''),
    '');
$fn$;

-- "Maria" + "Maria Kowalski" → 'MK'; no full_name → 'M'; nothing → NULL.
-- This is what the Stranger tier renders instead of a name.
create or replace function public.lana_initials(p_nickname text, p_full_name text default null)
returns text
language sql
immutable
set search_path to 'pg_catalog'
as $fn$
  select nullif(
    upper(
      coalesce(substr(btrim(coalesce(p_nickname, p_full_name, '')), 1, 1), '')
      ||
      coalesce(
        case
          when p_full_name is null then ''
          when array_length(regexp_split_to_array(btrim(p_full_name), '\s+'), 1) < 2 then ''
          else substr(
                 (regexp_split_to_array(btrim(p_full_name), '\s+'))
                   [array_length(regexp_split_to_array(btrim(p_full_name), '\s+'), 1)],
                 1, 1)
        end, '')
    ), '');
$fn$;

-- Last initial with a period, for render-time disambiguation ("Maria K.").
create or replace function public.lana_last_initial(p_full_name text)
returns text
language sql
immutable
set search_path to 'pg_catalog'
as $fn$
  select case
    when p_full_name is null then null
    when array_length(regexp_split_to_array(btrim(p_full_name), '\s+'), 1) < 2 then null
    else upper(substr(
      (regexp_split_to_array(btrim(p_full_name), '\s+'))
        [array_length(regexp_split_to_array(btrim(p_full_name), '\s+'), 1)], 1, 1))
  end;
$fn$;

-- 'Foster City (94404)' → 'fostercity' · 'Lake Nona — Area A' → 'lakenonaareaa'
-- (trimmed to 12 so 'maria.fostercity' stays sayable).
create or replace function public.lana_place_token(p_block_display_name text)
returns text
language sql
immutable
set search_path to 'pg_catalog'
as $fn$
  select nullif(
    substr(public.lana_slugify(regexp_replace(coalesce(p_block_display_name,''), '\(.*\)', '', 'g')), 1, 12),
    '');
$fn$;

create or replace function public.handle_is_valid(p_handle text)
returns boolean
language sql
immutable
set search_path to 'pg_catalog'
as $fn$
  select p_handle is not null
     and p_handle ~ '^[a-z][a-z0-9]*([._][a-z0-9]+)*$'
     and char_length(p_handle) between 3 and 24;
$fn$;

-- ─── 5) HANDLE SUGGESTION ENGINE ────────────────────────────────────────────
-- Ordered strategies, most human first. A digit suffix is the LAST resort,
-- reached only after we have tried a real last initial, the person's actual
-- neighbourhood and their ZIP. This is the "never Maria47" guarantee.
create or replace function public.suggest_handles(
  p_seed      text,
  p_full_name text default null,
  p_block_id  text default null,
  p_zip       text default null,
  p_limit     int  default 3,
  p_exclude_user uuid default null,
  p_allow_numeric boolean default true    -- false = auto path: never invent a number
)
returns text[]
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
declare
  v_base      text;
  v_full      text;
  v_last      text;
  v_place     text;
  v_zip5      text;
  v_cands     text[] := '{}';
  v_out       text[] := '{}';
  c           text;
  i           int;
begin
  v_base  := substr(coalesce(public.lana_slugify(p_seed), ''), 1, 20);
  v_full  := coalesce(public.lana_slugify(p_full_name), '');
  v_last  := lower(coalesce(public.lana_last_initial(p_full_name), ''));
  v_place := (select public.lana_place_token(b.display_name) from public.blocks b where b.id = p_block_id);
  v_zip5  := substring(coalesce(p_zip, '') from '[0-9]{5}');

  if v_base is null or char_length(v_base) = 0 then
    return '{}';                      -- non-Latin script: caller must ask.
  end if;

  -- 1–2 char names ("Jo", "Al") must clear the 3-char floor WITHOUT a filler
  -- letter. Prefer the real full name, then the real neighbourhood, else give
  -- up and let the profile ask. We never emit 'ax2'.
  if char_length(v_base) < 3 then
    if char_length(v_full) >= 3 then
      v_base := substr(v_full, 1, 20);
    elsif v_place is not null then
      v_base := v_base || '.' || v_place;
    else
      return '{}';
    end if;
  end if;

  v_cands := array_append(v_cands, v_base);
  if v_last <> '' then
    v_cands := array_append(v_cands, v_base || '.' || v_last);
  end if;
  if v_full <> '' and v_full <> v_base then
    v_cands := array_append(v_cands, substr(v_full, 1, 24));
  end if;
  -- (guard: v_base may already END with the place token when a 1–2 char seed
  --  was padded with it above — don't emit 'a.fostercity.fostercity')
  if v_place is not null and v_base not like ('%' || v_place) then
    v_cands := array_append(v_cands, substr(v_base || '.' || v_place, 1, 24));
  end if;
  if v_zip5 is not null then
    v_cands := array_append(v_cands, substr(v_base, 1, 18) || '.' || v_zip5);
  end if;
  -- LAST resort, and only when a human is watching (interactive availability
  -- check). The auto path passes p_allow_numeric := false and would rather
  -- leave handle NULL than hand someone "maria47".
  if p_allow_numeric then
    for i in 2..99 loop
      v_cands := array_append(v_cands, substr(v_base, 1, 22) || i::text);
    end loop;
  end if;

  foreach c in array v_cands loop
    if public.handle_is_valid(c)
       and c <> all(v_out)
       and not exists (select 1 from public.handle_reserved r where r.handle = c)
       and not exists (
             select 1 from public.users u
             where lower(u.handle) = c
               and (p_exclude_user is null or u.id <> p_exclude_user))
    then
      v_out := array_append(v_out, c);
      exit when array_length(v_out, 1) >= greatest(1, least(coalesce(p_limit, 3), 5));
    end if;
  end loop;

  return v_out;
end;
$fn$;

-- ─── 6) AVAILABILITY CHECK (profile editor, live as you type) ───────────────
create or replace function public.check_handle_available(p_handle text)
returns jsonb
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
declare
  v_caller uuid := auth.uid();
  v_norm   text := lower(btrim(coalesce(p_handle, '')));
  v_reason text := null;
  v_ok     boolean := false;
  v_owner  uuid;
begin
  if not public.handle_is_valid(v_norm) then
    v_reason := 'invalid_format';
  elsif exists (select 1 from public.handle_reserved r where r.handle = v_norm) then
    v_reason := 'reserved';
  else
    select u.id into v_owner from public.users u where lower(u.handle) = v_norm;
    if v_owner is null or v_owner = v_caller then
      v_ok := true;
    else
      v_reason := 'taken';
    end if;
  end if;

  return jsonb_build_object(
    'handle', v_norm,
    'available', v_ok,
    'reason', v_reason,
    'suggestions', case
      when v_ok then '[]'::jsonb
      else to_jsonb(public.suggest_handles(
             coalesce(nullif(v_norm,''), (select nickname from public.users where id = v_caller)),
             (select full_name     from public.users where id = v_caller),
             (select home_block_id from public.users where id = v_caller),
             (select home_zip      from public.users where id = v_caller),
             3, v_caller, true))
    end
  );
end;
$fn$;

-- ─── 7) WRITE PATHS ─────────────────────────────────────────────────────────
-- Guard: today users_update_own lets any client PATCH nickname/handle straight
-- through PostgREST with no validation. Force handle writes through the RPC.
create or replace function public.users_guard_handle()
returns trigger
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
begin
  if new.handle is distinct from old.handle
     and coalesce(current_setting('lana.handle_write', true), '') <> '1'
     and coalesce(auth.role(), '') = 'authenticated'
  then
    raise exception 'handle_write_requires_rpc'
      using errcode = 'P0001',
            hint = 'call public.set_my_handle() / set_my_display_name() instead of updating users.handle directly';
  end if;
  return new;
end;
$fn$;

drop trigger if exists users_guard_handle_trg on public.users;
create trigger users_guard_handle_trg
  before update of handle on public.users
  for each row execute function public.users_guard_handle();

-- Single warm write. Display name is required; handle is optional and is
-- auto-derived from the display name when the caller does not supply one.
create or replace function public.set_my_display_name(
  p_nickname  text,
  p_handle    text default null,
  p_full_name text default null,
  p_source    text default 'user'
)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
declare
  v_caller uuid := auth.uid();
  v_nick   text := btrim(coalesce(p_nickname, ''));
  v_handle text := nullif(lower(btrim(coalesce(p_handle, ''))), '');
  v_full   text := nullif(btrim(coalesce(p_full_name, '')), '');
  v_src    text := case when p_source in ('lana','user','migrated') then p_source else 'user' end;
  v_auto   boolean := false;
  v_sugg   text[];
  v_me     record;
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if char_length(v_nick) < 1 or char_length(v_nick) > 40 or v_nick ~ '[[:cntrl:]]' then
    raise exception 'invalid_display_name' using errcode = 'P0001';
  end if;

  select u.handle, u.full_name, u.home_block_id, u.home_zip
    into v_me from public.users u where u.id = v_caller;

  if v_handle is null then
    if v_me.handle is not null then
      v_handle := v_me.handle;                    -- renaming display ≠ rehandle
    else
      -- p_allow_numeric := false → we would rather leave handle NULL (and let
      -- the profile ask) than auto-assign "maria47" to a real person.
      v_sugg := public.suggest_handles(
                  v_nick, coalesce(v_full, v_me.full_name),
                  v_me.home_block_id, v_me.home_zip, 1, v_caller, false);
      v_handle := v_sugg[1];                      -- may legitimately be NULL
      v_auto := true;
    end if;
  else
    if not public.handle_is_valid(v_handle) then
      raise exception 'invalid_handle' using errcode = 'P0001';
    end if;
    if exists (select 1 from public.handle_reserved r where r.handle = v_handle) then
      raise exception 'reserved_handle' using errcode = 'P0001';
    end if;
    if exists (select 1 from public.users u where lower(u.handle) = v_handle and u.id <> v_caller) then
      raise exception 'handle_taken' using errcode = 'P0001';
    end if;
  end if;

  perform set_config('lana.handle_write', '1', true);

  update public.users u
     set nickname            = v_nick,
         display_name_source = v_src,
         full_name           = coalesce(v_full, u.full_name),
         handle              = coalesce(v_handle, u.handle),
         handle_set_by       = case
                                 when v_handle is null then u.handle_set_by
                                 when v_handle is distinct from u.handle
                                   then case when v_auto then 'auto' else 'user' end
                                 else u.handle_set_by
                               end,
         handle_set_at       = case when v_handle is distinct from u.handle then now() else u.handle_set_at end,
         updated_at          = now()
   where u.id = v_caller;

  perform set_config('lana.handle_write', '0', true);

  return public.get_my_profile();
exception
  when unique_violation then
    perform set_config('lana.handle_write', '0', true);
    raise exception 'handle_taken' using errcode = 'P0001';
end;
$fn$;

create or replace function public.set_my_handle(p_handle text)
returns jsonb
language sql
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
  select public.set_my_display_name(
    (select nickname from public.users where id = auth.uid()),
    p_handle, null, 'user');
$fn$;

-- ─── 8) "ABOUT YOU" ─────────────────────────────────────────────────────────
-- Deterministic fallback draft, assembled ONLY from claims the user already
-- made public. No inference, no invention. The worker's LLM writes the nicer
-- version into about_you_draft via set_about_you_draft(); this exists so the
-- section is never empty and so we can diff LLM output against ground truth.
create or replace function public.generate_about_you_draft(p_user_id uuid)
returns text
language sql
stable
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
  with labels as (
    select distinct on (lower(c.label)) c.label, c.confidence
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
      and coalesce(btrim(c.label), '') <> ''
    order by lower(c.label), c.confidence desc
  ), top as (
    select label from labels order by confidence desc limit 4
  )
  -- Deliberately a TAG LIST, not prose: raw claim labels ('14-month-old',
  -- 'Paulista') do not survive being glued into a sentence, and a fallback that
  -- reads wrong is worse than one that reads plain. The worker writes prose.
  select case when count(*) = 0 then null
              else 'Around here: ' || array_to_string(array_agg(label), ' · ')
         end
  from top;
$fn$;

-- Worker-side write of the LLM draft. Never touches the published about_you.
create or replace function public.set_about_you_draft(p_user_id uuid, p_draft text)
returns void
language sql
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
  update public.users
     set about_you_draft    = nullif(btrim(coalesce(p_draft, '')), ''),
         about_you_draft_at = now(),
         updated_at         = now()
   where id = p_user_id;
$fn$;

-- The user publishes (or clears) their own blurb. Consent lives here.
create or replace function public.set_my_about_you(p_about text, p_source text default 'user')
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
declare
  v_caller uuid := auth.uid();
  v_text   text := nullif(btrim(coalesce(p_about, '')), '');
begin
  if v_caller is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if v_text is not null and char_length(v_text) > 280 then
    raise exception 'about_you_too_long' using errcode = 'P0001';
  end if;

  update public.users
     set about_you            = v_text,
         about_you_source     = case when v_text is null then null
                                     when p_source = 'lana' then 'lana' else 'user' end,
         about_you_updated_at = case when v_text is null then null else now() end,
         updated_at           = now()
   where id = v_caller;

  return public.get_my_profile();
end;
$fn$;

-- ─── 9) RENDER-TIME DISAMBIGUATION ──────────────────────────────────────────
-- Feed the user_ids that are about to appear together on ONE surface (an event
-- participant list, a peer-match carousel). Only the people who actually clash
-- get a suffix, and the suffix is human: last initial → neighbourhood → @handle.
create or replace function public.disambiguate_display_names(p_user_ids uuid[])
returns table(user_id uuid, display_label text, initials text, needs_disambiguation boolean)
language sql
stable
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
  with base as (
    select u.id,
           coalesce(u.nickname, '') as nick,
           public.lana_last_initial(u.full_name) as last_i,
           public.lana_initials(u.nickname, u.full_name) as inits,
           u.handle,
           (select b.display_name from public.blocks b where b.id = u.home_block_id) as block_name,
           u.home_location_visibility
    from public.users u
    where u.id = any(coalesce(p_user_ids, '{}'::uuid[]))
  ), dup as (
    select id, nick, last_i, inits, handle, block_name, home_location_visibility,
           count(*) over (partition by lower(nick)) as n
    from base
  )
  select
    d.id,
    case
      when d.n <= 1 or d.nick = '' then nullif(d.nick, '')
      when d.last_i is not null    then d.nick || ' ' || d.last_i || '.'
      when d.home_location_visibility = 'block' and d.block_name is not null
                                   then d.nick || ' · ' || regexp_replace(d.block_name, '\s*\(.*\)$', '')
      when d.handle is not null    then d.nick || ' · @' || d.handle
      else nullif(d.nick, '')
    end,
    d.inits,
    (d.n > 1)
  from dup d;
$fn$;

-- ─── 10) PROFILE RPCs — additive key changes only ───────────────────────────
-- get_my_profile: 'handle' now returns the REAL handle column (it used to
-- alias u.nickname). 'display_name' is the new canonical key for the visible
-- name; 'nickname' is kept verbatim for back-compat with the shipped FE.
create or replace function public.get_my_profile()
returns jsonb
language sql
stable
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
  select jsonb_build_object(
    'id', u.id,
    'full_name', u.full_name,
    'nickname', u.nickname,
    'display_name', u.nickname,
    'display_name_source', u.display_name_source,
    'handle', u.handle,
    'handle_set_by', u.handle_set_by,
    'initials', public.lana_initials(u.nickname, u.full_name),
    'about_you', u.about_you,
    'about_you_source', u.about_you_source,
    'about_you_draft', u.about_you_draft,
    'phone', u.phone,
    'phone_verified_at', u.phone_verified_at,
    'profile_photo_url', u.profile_photo_url,
    'home_block_id', u.home_block_id,
    'home_zip', u.home_zip,
    'block_display_name', b.display_name,
    'block_state', b.state,
    'cluster_id', b.cluster_id,
    'home_location_visibility', u.home_location_visibility::text,
    'locale', u.locale,
    'kids_count', u.kids_count,
    'voice_autoplay', u.voice_autoplay,
    'created_at', u.created_at
  )
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = auth.uid();
$fn$;

-- get_peer_profile: adds handle / display_name / initials / about_you.
-- full_name is NEVER exposed here — only the derived last initial, and only
-- inside disambiguate_display_names(). Anonymous branch still returns nulls,
-- now with initials so the Stranger tier has something to render.
create or replace function public.get_peer_profile(p_user_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
declare
  caller uuid := auth.uid();
  peer record;
  is_matched boolean;
  location_label text;
  location_precision text;
  result jsonb;
begin
  select u.id, u.nickname, u.handle, u.about_you, u.full_name, u.profile_photo_url,
         u.home_block_id, u.home_location_visibility, b.display_name, b.cluster_id
  into peer
  from public.users u
  left join public.blocks b on b.id = u.home_block_id
  where u.id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  -- Anonymous visitor: blurred profile. Initials only — no name, no handle.
  if caller is null then
    return jsonb_build_object(
      'user_id', null,
      'nickname', null,
      'display_name', null,
      'handle', null,
      'initials', public.lana_initials(peer.nickname, null),
      'about_you', null,
      'avatar_url', null,
      'is_blurred', true,
      'is_matched', false,
      'public_claims', '[]'::jsonb,
      'mutual_claims', '[]'::jsonb,
      'shared_claim_count', 0,
      'location_label', null,
      'location_precision', null,
      'block_name', null,
      'upcoming_shared_events', '[]'::jsonb
    );
  end if;

  location_label := case peer.home_location_visibility
    when 'block' then peer.display_name
    when 'cluster' then peer.cluster_id
  end;
  location_precision := peer.home_location_visibility::text;

  is_matched := public.are_users_matched(caller, p_user_id);

  result := jsonb_build_object(
    'user_id', peer.id,
    'nickname', peer.nickname,
    'display_name', peer.nickname,
    'handle', peer.handle,
    'initials', public.lana_initials(peer.nickname, peer.full_name),
    'about_you', peer.about_you,
    'avatar_url', peer.profile_photo_url,
    'is_blurred', false,
    'is_matched', is_matched,
    'public_claims', coalesce((
      select jsonb_agg(jsonb_build_object(
        'concept', c.concept,
        'label', c.label,
        'tone', c.tone,
        'confidence', c.confidence
      ) order by c.confidence desc)
      from public.user_identity_claims c
      where c.user_id = peer.id
        and c.dismissed_at is null
        and c.disclosure = 'public'
    ), '[]'::jsonb),
    'mutual_claims', case
      when is_matched then
        coalesce((
          select jsonb_agg(jsonb_build_object(
            'concept', c.concept,
            'label', c.label,
            'tone', c.tone,
            'confidence', c.confidence
          ) order by c.confidence desc)
          from public.user_identity_claims c
          where c.user_id = peer.id
            and c.dismissed_at is null
            and c.disclosure = 'mutual'
        ), '[]'::jsonb)
      else '[]'::jsonb
    end,
    'shared_claim_count', (
      select count(*)::int
      from public.user_identity_claims c1
      join public.user_identity_claims c2
        on c1.concept = c2.concept
      where c1.user_id = caller
        and c2.user_id = peer.id
        and c1.dismissed_at is null
        and c2.dismissed_at is null
        and c1.disclosure = 'public'
        and c2.disclosure = 'public'
    ),
    'location_label', location_label,
    'location_precision', location_precision,
    'block_name', case when peer.home_location_visibility = 'block' then peer.display_name else null end,
    'upcoming_shared_events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', e.id,
        'title', e.title,
        'starts_at', e.starts_at
      ) order by e.starts_at asc)
      from public.events e
      where e.status = 'open'
        and e.starts_at > now()
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = caller
            and r.status in ('approved', 'attended')
        )
        and exists (
          select 1 from public.event_requests r
          where r.event_id = e.id
            and r.requester_id = peer.id
            and r.status in ('approved', 'attended')
        )
    ), '[]'::jsonb)
  );

  return result;
end;
$fn$;

-- get_profile_summary (Stranger tier / anon preview): still no name, no handle,
-- but now returns initials + the about blurb so the card isn't a grey blob.
create or replace function public.get_profile_summary(p_user_id uuid)
returns jsonb
language plpgsql
security definer
set search_path to 'pg_catalog', 'public'
as $fn$
declare
  v_user record;
  v_public_claims jsonb;
begin
  select id, created_at, nickname, full_name, about_you
  into v_user
  from public.users
  where id = p_user_id;

  if not found then
    raise exception 'peer_not_found' using errcode = 'P0001';
  end if;

  select coalesce(jsonb_agg(sub.label order by sub.confidence desc), '[]'::jsonb)
  into v_public_claims
  from (
    select distinct c.label, c.confidence
    from public.user_identity_claims c
    where c.user_id = p_user_id
      and c.dismissed_at is null
      and c.disclosure = 'public'
    order by c.confidence desc
  ) sub;

  return jsonb_build_object(
    'user_id', null,
    'nickname', null,
    'display_name', null,
    'handle', null,
    'initials', public.lana_initials(v_user.nickname, null),
    'about_you', v_user.about_you,
    'avatar_url', null,
    'is_blurred', true,
    'is_authenticated', false,
    'is_matched', false,
    'event_count', coalesce((select count(*) from public.event_requests er where er.requester_id = p_user_id and er.status in ('approved', 'attended')), 0),
    'weeks_here', floor(extract(epoch from now() - v_user.created_at) / 604800)::int,
    'affinity_match_count', jsonb_array_length(v_public_claims),
    'about_tags', v_public_claims,
    'common_interest_tags', '[]'::jsonb,
    'shared_event_count', 0
  );
end;
$fn$;

-- ─── 11) GRANTS ─────────────────────────────────────────────────────────────
grant execute on function public.lana_slugify(text)                       to authenticated, anon, service_role;
grant execute on function public.lana_initials(text, text)                to authenticated, anon, service_role;
grant execute on function public.lana_last_initial(text)                  to authenticated, anon, service_role;
grant execute on function public.lana_place_token(text)                   to authenticated, anon, service_role;
grant execute on function public.handle_is_valid(text)                    to authenticated, anon, service_role;
grant execute on function public.suggest_handles(text, text, text, text, int, uuid, boolean) to authenticated, service_role;
grant execute on function public.check_handle_available(text)             to authenticated, service_role;
grant execute on function public.set_my_display_name(text, text, text, text) to authenticated, service_role;
grant execute on function public.set_my_handle(text)                      to authenticated, service_role;
grant execute on function public.set_my_about_you(text, text)             to authenticated, service_role;
grant execute on function public.generate_about_you_draft(uuid)           to authenticated, service_role;
grant execute on function public.set_about_you_draft(uuid, text)          to service_role;
grant execute on function public.disambiguate_display_names(uuid[])       to authenticated, service_role;
-- functions are EXECUTE-to-PUBLIC by default; lock the worker-only write down.
revoke execute on function public.set_about_you_draft(uuid, text)         from public;
revoke execute on function public.set_about_you_draft(uuid, text)         from authenticated, anon;
grant  execute on function public.set_about_you_draft(uuid, text)         to service_role;

-- ─── 12) BACKFILL (the 31 existing users) ───────────────────────────────────
-- Ordered by created_at ASC so the FIRST person to have joined keeps the clean
-- handle; a later namesake is the one who gets the suffix. Nobody's visible
-- display name is touched. Idempotent: only fills NULL handles.
do $$
declare
  r      record;
  v_sugg text[];
begin
  perform set_config('lana.handle_write', '1', true);
  for r in
    select id, nickname, full_name, home_block_id, home_zip
    from public.users
    where handle is null and coalesce(btrim(nickname), '') <> ''
    order by created_at asc, id asc
  loop
    v_sugg := public.suggest_handles(r.nickname, r.full_name, r.home_block_id, r.home_zip, 1, r.id, false);
    if array_length(v_sugg, 1) >= 1 then
      update public.users
         set handle              = v_sugg[1],
             handle_set_by       = 'auto',
             handle_set_at       = now(),
             display_name_source = coalesce(display_name_source, 'migrated'),
             updated_at          = now()
       where id = r.id;
    end if;
  end loop;
  perform set_config('lana.handle_write', '0', true);
end $$;

commit;

-- ============================================================================
-- TEST PLAN (run after apply)
-- ----------------------------------------------------------------------------
-- 1. Slug + initials are sane:
--    select public.lana_slugify('María-José Ñuñez'),   -- mariajosenunez
--           public.lana_slugify('京子'),                -- NULL (by design)
--           public.lana_initials('Maria','Maria Kowalski'),  -- MK
--           public.lana_last_initial('Maria Kowalski'),      -- K
--           public.lana_place_token('Foster City (94404)');  -- fostercity
--
-- 2. Every existing user got a human, non-numeric handle:
--    select nickname, handle, handle_set_by from public.users where handle is not null order by created_at;
--    -- OBSERVED (7 of 31 rows have a nickname): maria | tommaso | asjid | rust |
--    --   natasha | sofia | daniel   → 7 backfilled, 0 dupes, 0 numeric tails.
--
-- 3. Uniqueness actually holds:
--    select lower(handle), count(*) from public.users where handle is not null group by 1 having count(*) > 1;  -- 0 rows
--    -- OBSERVED rejections (all inside a rolled-back tx on prod):
--    --   duplicate 'maria'  → 23505 unique_violation      PASS
--    --   'MARIA'            → 23514 check_violation        PASS (uppercase blocked first)
--    --   'x'                → 23514 check_violation        PASS (min length 3)
--    --   '1maria'           → 23514 check_violation        PASS (must start alpha)
--    --   'maria kowalski'   → 23514 check_violation        PASS (no spaces)
--    --   'maria.k'          → accepted                     PASS
--
-- 4. Collision ladder never reaches a digit while human options remain.
--    OBSERVED on prod 2026-07-30 (rolled-back tx):
--    suggest_handles('Aurelia','Aurelia Costa','zip-94404','94404',5)
--      → {aurelia, aurelia.c, aureliacosta, aurelia.fostercity, aurelia.94404}
--    suggest_handles('Maria','Maria Kowalski','zip-94404','94404',5)  -- maria TAKEN
--      → {maria.k, mariakowalski, maria.fostercity, maria.94404, maria2}
--    suggest_handles('Jo','Jo Ann','zip-94404','94404',4)
--      → {joann, joann.a, joann.fostercity, joann.94404}     -- no filler letters
--    suggest_handles('京子', ...)                → {}         -- ask, never invent
--    suggest_handles('A', null, null, null, 3)  → {}         -- ask, never 'ax2'
--    suggest_handles('Lana', ..., p_allow_numeric => false) → {}   -- never 'lana2'
--    suggest_handles('Lana', ..., p_allow_numeric => true)  → {lana2, lana3, lana4}
--
-- 5. Second-person-pays: with 'maria' already taken, a new Maria gets 'maria.k'
--    and the FIRST Maria's handle is unchanged.  OBSERVED: PASS.
--
-- 6. Render-time disambiguation only fires on a real clash.
--    OBSERVED with three users forced to nickname='Maria':
--      'Maria K.'  [MK] dup=true      (has full_name)
--      'Maria B.'  [MB] dup=true      (has full_name)
--      'Maria · Lake Nona — Area A' [M] dup=true   (no full_name → block name)
--      'Natasha'   [N]  dup=false     (untouched)
--
-- 7. Reserved + format rejection:
--    select public.check_handle_available('lana');   -- available:false reason:reserved + suggestions
--    select public.check_handle_available('a');      -- available:false reason:invalid_format
--    select public.check_handle_available('Maria');  -- normalises to 'maria'
--
-- 8. Direct PATCH of users.handle from the client is blocked:
--    -- as an authenticated JWT: update public.users set handle='x' where id=auth.uid();
--    -- expect P0001 handle_write_requires_rpc
--
-- 9. Profile contract:
--    select public.get_my_profile() -> 'handle';        -- real handle, not the nickname
--    select public.get_my_profile() -> 'display_name';  -- the nickname
--    select public.get_profile_summary('<uid>') -> 'initials';  -- 'M', nickname still null
--
-- 10. about_you draft is claim-grounded and never invented:
--    select public.generate_about_you_draft('<uid>');
--    -- OBSERVED: 'Around here: Paulista · 14-month-old · faith'
--    -- expect NULL for a user with no public claims (never a hallucinated blurb)
--
-- ----------------------------------------------------------------------------
-- ROLLBACK (full, in this order)
-- ----------------------------------------------------------------------------
-- begin;
--   drop trigger if exists users_guard_handle_trg on public.users;
--   drop function if exists public.users_guard_handle();
--   drop function if exists public.disambiguate_display_names(uuid[]);
--   drop function if exists public.set_my_about_you(text, text);
--   drop function if exists public.set_about_you_draft(uuid, text);
--   drop function if exists public.generate_about_you_draft(uuid);
--   drop function if exists public.set_my_handle(text);
--   drop function if exists public.set_my_display_name(text, text, text, text);
--   drop function if exists public.check_handle_available(text);
--   drop function if exists public.suggest_handles(text, text, text, text, int, uuid, boolean);
--   drop function if exists public.handle_is_valid(text);
--   drop function if exists public.lana_place_token(text);
--   drop function if exists public.lana_last_initial(text);
--   drop function if exists public.lana_initials(text, text);
--   drop function if exists public.lana_slugify(text);
--   drop index if exists public.users_handle_lower_uidx;
--   drop table if exists public.handle_reserved;
--   alter table public.users
--     drop constraint if exists users_handle_format_chk,
--     drop constraint if exists users_handle_set_by_chk,
--     drop constraint if exists users_display_name_source_chk,
--     drop constraint if exists users_about_you_source_chk,
--     drop constraint if exists users_about_you_len_chk,
--     drop constraint if exists users_nickname_shape_chk;
--   alter table public.users
--     drop column if exists handle,
--     drop column if exists handle_set_by,
--     drop column if exists handle_set_at,
--     drop column if exists display_name_source,
--     drop column if exists about_you,
--     drop column if exists about_you_source,
--     drop column if exists about_you_updated_at,
--     drop column if exists about_you_draft,
--     drop column if exists about_you_draft_at;
--   -- restore the three profile RPCs to their 2026-07-30 definitions:
--   --   get_my_profile      : 'handle' aliased to u.nickname, no about_you/initials keys
--   --   get_peer_profile    : select list without handle/about_you/full_name; no display_name/initials keys
--   --   get_profile_summary : select id, created_at only; no initials/about_you keys
--   -- (verbatim originals are pasted in PR8_nickname_system.md §Rollback)
-- commit;
--
-- NOTE: users.nickname is never modified by this PR, so a rollback loses
-- nothing a user typed. Only derived data (handle, about_you_draft) is lost.
-- ============================================================================
