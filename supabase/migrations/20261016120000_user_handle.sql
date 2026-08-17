-- Public handle: the auto-generated alphanumeric name neighbours see ("rosegold22"),
-- editable by the user.
--
-- WHY A NEW COLUMN AND NOT users.nickname
--   nickname is the real first name the user gave Lana ("what should neighbors call
--   you?"), and it is the string every prompt, greeting and reply reads. get_my_profile
--   already returned a 'handle' key — but aliased to nickname, so the "@handle" the UI
--   showed WAS the real name. Repurposing nickname would rename the user inside Lana's
--   own voice; a separate column leaves that untouched.
--
-- NOT IN THIS MIGRATION (deliberate)
--   Peer-facing surfaces (get_peer_profile, get_cluster_peers, community members, event
--   host_name, chat threads) still read nickname, so neighbours still see the real name.
--   Swapping them — and gating the real-name reveal on the relationship tier — is a
--   separate step: the reveal threshold (mutual nudge vs. the already-built unmask flow)
--   is still an open product call.

alter table public.users add column if not exists handle text;

comment on column public.users.handle is
  'Public alphanumeric name shown to other neighbours ("rosegold22"). Auto-filled on '
  'insert, lowercase, unique, user-editable via set_my_handle. Distinct from nickname, '
  'which is the real first name Lana uses when speaking to the user.';

alter table public.users drop constraint if exists users_handle_format;
alter table public.users add constraint users_handle_format
  check (handle is null or handle ~ '^[a-z0-9]{3,20}$');

create unique index if not exists users_handle_uniq on public.users (handle);

-- ---------------------------------------------------------------------------
-- Generator
-- ---------------------------------------------------------------------------

create or replace function public.generate_handle()
returns text
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  -- Two halves that read like a name rather than a serial number, matching the
  -- mocks (coral88, mapleluz, sunnyfern, wildlune).
  v_first text[] := array[
    'rose', 'coral', 'maple', 'sunny', 'wild', 'amber', 'olive', 'indigo', 'hazel',
    'ember', 'dusk', 'sage', 'plum', 'cedar', 'luna', 'misty', 'river', 'clover',
    'honey', 'juniper'
  ];
  v_second text[] := array[
    'gold', 'fern', 'lune', 'luz', 'sky', 'moss', 'wren', 'dawn', 'vale', 'tide',
    'wood', 'stone', 'brook', 'finch', 'cove', 'birch', 'reed', 'flint', 'haven', 'peak'
  ];
  v_try text;
begin
  for i in 1..40 loop
    v_try := v_first[1 + floor(random() * array_length(v_first, 1))::int]
          || v_second[1 + floor(random() * array_length(v_second, 1))::int]
          -- Digits only once the clean pairs start colliding, so early users get
          -- "mapleluz" and only later ones get "maplemoss42".
          || case when i <= 4 then '' else (10 + floor(random() * 90))::int::text end;
    exit when not exists (select 1 from public.users u where u.handle = v_try);
    v_try := null;
  end loop;
  -- ponytail: 40 tries then a random tail. A real collision-proof scheme (counter
  -- table, base32 of a sequence) only earns its keep at millions of users.
  return coalesce(v_try, 'n' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 11));
end;
$$;

comment on function public.generate_handle() is
  'One unused public handle. Word pair first, digits appended only on collision.';

-- ---------------------------------------------------------------------------
-- Never null, always lowercase — on every insert path (auth trigger, worker,
-- guest promotion) and on every write, so the format check can stay strict.
-- ---------------------------------------------------------------------------

create or replace function public.users_normalize_handle()
returns trigger
language plpgsql
set search_path = pg_catalog, public
as $$
begin
  new.handle := nullif(lower(btrim(coalesce(new.handle, ''))), '');
  if new.handle is null then
    new.handle := public.generate_handle();
  end if;
  return new;
end;
$$;

drop trigger if exists users_handle_default on public.users;
create trigger users_handle_default
before insert or update of handle on public.users
for each row execute function public.users_normalize_handle();

-- Row-at-a-time so each generated handle sees the ones before it; a single UPDATE
-- would evaluate generate_handle() against a snapshot and collide with itself.
do $$
declare r record;
begin
  for r in select id from public.users where handle is null loop
    update public.users set handle = public.generate_handle() where id = r.id;
  end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Reads: get_my_profile stops aliasing handle to the real name.
-- Faithful copy of the 20260825 definition + the one changed line.
-- ---------------------------------------------------------------------------

create or replace function public.get_my_profile()
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select jsonb_build_object(
    'id', u.id,
    'full_name', u.full_name,
    'nickname', u.nickname,
    'handle', u.handle,
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
$$;

comment on function public.get_my_profile() is
  'Own profile header. handle = the public name neighbours see; nickname = the real '
  'first name Lana uses. kids_count = stated child count (matching only). '
  'voice_autoplay = client TTS preference.';

grant execute on function public.get_my_profile() to authenticated;

-- ---------------------------------------------------------------------------
-- Write: the pencil next to the handle pill.
-- ---------------------------------------------------------------------------

create or replace function public.set_my_handle(p_handle text)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_handle text := lower(btrim(coalesce(p_handle, '')));
begin
  if auth.uid() is null then
    raise exception 'not_authenticated' using errcode = 'P0001';
  end if;
  if v_handle !~ '^[a-z0-9]{3,20}$' then
    raise exception 'handle_invalid' using errcode = 'P0001';
  end if;
  -- Cheap pre-check for a clean error; the unique index is the real guard, and the
  -- RLS select policy may hide the row that owns the name, hence the catch below.
  if exists (select 1 from public.users u where u.handle = v_handle and u.id <> auth.uid()) then
    raise exception 'handle_taken' using errcode = 'P0001';
  end if;

  update public.users set handle = v_handle where id = auth.uid();
  return jsonb_build_object('handle', v_handle);
exception
  when unique_violation then
    raise exception 'handle_taken' using errcode = 'P0001';
end;
$$;

comment on function public.set_my_handle(text) is
  'Rename the caller''s public handle. 3-20 lowercase alphanumerics, unique. Raises '
  'handle_invalid / handle_taken. RLS (users_update_own) guards the row.';

revoke all on function public.set_my_handle(text) from public, anon;
grant execute on function public.set_my_handle(text) to authenticated;

-- ---------------------------------------------------------------------------
-- Self-check: fails the push if the generator or the backfill is wrong.
-- ---------------------------------------------------------------------------

do $$
declare v text;
begin
  for i in 1..25 loop
    v := public.generate_handle();
    if v !~ '^[a-z0-9]{3,20}$' then
      raise exception 'generate_handle produced an invalid handle: %', v;
    end if;
  end loop;
  if exists (select 1 from public.users where handle is null) then
    raise exception 'handle backfill left null rows';
  end if;
end;
$$;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.set_my_handle(text);
--   drop trigger if exists users_handle_default on public.users;
--   drop function if exists public.users_normalize_handle();
--   drop function if exists public.generate_handle();
--   drop index if exists public.users_handle_uniq;
--   alter table public.users drop constraint if exists users_handle_format;
--   alter table public.users drop column if exists handle;
--   -- plus: re-run the 20260825 get_my_profile ('handle', u.nickname).
-- ============================================================================
