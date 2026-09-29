-- Community settings · the first write path an operator has ever had
--
-- STATE BEFORE THIS MIGRATION
--
--   `places` has RLS ENABLED with ZERO policies. Same for place_claims and
--   circle_affiliations. Nothing is readable or writable by anon or authenticated — every
--   access goes through a security definer function.
--
--   That is the correct posture and this migration does not weaken it. There is no
--   `grant update on places`, and no policy is added. An operator gets exactly two verbs,
--   both narrow, both authorised the same way.
--
--   Do not "simplify" this later by adding an UPDATE policy on places. The public place
--   page is our answer-engine surface; a broad write path to it is a broad write path to
--   something we cannot retract once ingested.
--
-- WHO MAY EDIT
--   place_managers.role = 'operator', removed_at is null. NOT places.claimed_by —
--   claimed_by is a single uuid and is precisely what place_managers replaced.

-- ── authorisation · one definition, used by every settings verb ─────────────

create or replace function public.is_community_operator(p_place_id uuid, p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select exists (
    select 1 from public.place_managers m
     where m.place_id = p_place_id
       and m.user_id  = p_user_id
       and m.role     = 'operator'
       and m.removed_at is null);
$$;

revoke all on function public.is_community_operator(uuid, uuid) from public, anon;
grant execute on function public.is_community_operator(uuid, uuid) to authenticated, service_role;

-- ── settings · name · purpose · description ─────────────────────────────────
--
-- Null means "leave alone", so a caller can send one field without having to read and
-- resend the others. Empty string means "clear it" — distinguishable, and the difference
-- matters for first_action, where cleared and never-set behave differently downstream.

create or replace function public.update_community_settings(
  p_place_id     uuid,
  p_name         text default null,
  p_first_action text default null,
  p_blurb        text default null
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid   uuid := auth.uid();
  v_place record;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;
  if not public.is_community_operator(p_place_id, v_uid) then
    raise exception 'not_operator' using hint = 'Only a verified operator may edit this community.';
  end if;

  if p_name is not null and length(btrim(p_name)) < 2 then
    raise exception 'name_too_short';
  end if;
  -- Long enough to say something real, short enough that Lana's first question stays a
  -- question rather than a recital.
  if p_first_action is not null and length(p_first_action) > 280 then
    raise exception 'first_action_too_long';
  end if;
  if p_blurb is not null and length(p_blurb) > 600 then
    raise exception 'blurb_too_long';
  end if;

  update public.places p
     set name         = coalesce(nullif(btrim(p_name), ''), p.name),
         first_action = case when p_first_action is null then p.first_action
                             when btrim(p_first_action) = '' then null
                             else btrim(p_first_action) end,
         blurb        = case when p_blurb is null then p.blurb
                             when btrim(p_blurb) = '' then null
                             else btrim(p_blurb) end,
         -- An operator who writes their own description owns it. Clearing it hands the
         -- job back to the worker.
         blurb_stale  = case when p_blurb is null then p.blurb_stale
                             when btrim(p_blurb) = '' then true
                             else false end,
         updated_at   = now()
   where p.id = p_place_id
  returning p.id, p.name, p.first_action, p.blurb, p.handle, p.blurb_stale
       into v_place;

  if v_place.id is null then
    raise exception 'place_not_found';
  end if;

  -- The name trigger may also have set blurb_stale; re-read rather than guess.
  return jsonb_build_object(
    'placeId',     v_place.id,
    'name',        v_place.name,
    'firstAction', v_place.first_action,
    'blurb',       v_place.blurb,
    'handle',      v_place.handle,
    'blurbStale',  v_place.blurb_stale);
end;
$$;

revoke all on function public.update_community_settings(uuid, text, text, text) from public, anon;
grant execute on function public.update_community_settings(uuid, text, text, text)
  to authenticated, service_role;

comment on function public.update_community_settings(uuid, text, text, text) is
  'Operator edit of name, purpose (first_action) and description (blurb). Null = leave '
  'alone, empty string = clear. Changing the name marks the blurb stale via trigger, '
  'because the blurb is derived from the name and a stale blurb grounds Lana''s first '
  'question in a community that no longer exists under that name.';

-- ── handle rename · once, then locked, old handle kept alive ────────────────
--
-- Eight handles are live and wrong-shaped. `zenaidyndb-community` is Etiqueta do Reino.
-- A creator will not send traffic to that, so it has to be fixable — but a handle already
-- sitting in a bio must keep working, so the old one is retired into an alias rather than
-- released.
--
-- ONE rename. handle_renamed_at is the record of it being spent. After that it takes
-- support, deliberately: a handle that churns is not an identity.
--
-- NOTE ON SHAPE. The DB accepts a single token (`^[a-z0-9]+(-[a-z0-9]+)*$`), but the PWA
-- treats a single-word URL as an alias for `{word}-community` before it ever calls this.
-- A single-token handle stored here would therefore be unreachable from the short link.
-- So this function requires the hyphenated form. The two rules must move together.

create or replace function public.rename_community_handle(
  p_place_id   uuid,
  p_new_handle text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid uuid := auth.uid();
  v_new text := public.normalize_place_handle(p_new_handle);
  v_old text;
  v_renamed timestamptz;
  v_state text;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;
  if not public.is_community_operator(p_place_id, v_uid) then
    raise exception 'not_operator';
  end if;

  select p.handle, p.handle_renamed_at, p.governance_state
    into v_old, v_renamed, v_state
    from public.places p where p.id = p_place_id;

  if v_state is null then
    raise exception 'place_not_found';
  end if;
  if v_renamed is not null then
    raise exception 'rename_already_used'
      using hint = 'A community may change its handle once. Contact support.';
  end if;
  if v_new is null or v_new !~ '^[a-z0-9]+(-[a-z0-9]+)+$' then
    -- Hyphen mandatory: see the note above about the short-link alias.
    raise exception 'handle_shape' using hint = 'Use at least two words, like acme-orlando.';
  end if;
  if length(v_new) < 3 or length(v_new) > 48 then
    raise exception 'handle_length';
  end if;
  if v_new = v_old then
    raise exception 'handle_unchanged';
  end if;
  if exists (select 1 from public.protected_handles h
              where h.normalized_handle = v_new and h.active) then
    raise exception 'handle_protected';
  end if;
  if public._place_handle_taken(v_new) then
    raise exception 'handle_taken';
  end if;
  -- Never hand one audience's retired link to somebody else.
  if exists (select 1 from public.place_handle_aliases a where a.handle = v_new) then
    raise exception 'handle_retired_elsewhere';
  end if;

  update public.places
     set handle = v_new, handle_renamed_at = now(), updated_at = now()
   where id = p_place_id;

  -- Retire the old one AFTER the new one is live, so the not-live trigger sees the truth.
  if v_old is not null then
    insert into public.place_handle_aliases (handle, place_id, retired_by)
    values (v_old, p_place_id, v_uid)
    on conflict (handle) do nothing;
  end if;

  return jsonb_build_object(
    'placeId', p_place_id,
    'handle',  v_new,
    'previousHandle', v_old,
    'renamesRemaining', 0);
end;
$$;

revoke all on function public.rename_community_handle(uuid, text) from public, anon;
grant execute on function public.rename_community_handle(uuid, text) to authenticated, service_role;

comment on function public.rename_community_handle(uuid, text) is
  'One rename per community, ever. The old handle is retired into place_handle_aliases '
  'and keeps resolving forever, so a link already in somebody''s bio does not die when we '
  'fix a bad handle. Requires the hyphenated form because the PWA reads a single word as '
  'an alias for {word}-community.';

-- ── settings read ───────────────────────────────────────────────────────────

create or replace function public.community_settings(p_place_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid uuid := auth.uid();
  v_p   record;
begin
  if v_uid is null or not public.is_community_operator(p_place_id, v_uid) then
    raise exception 'not_operator';
  end if;

  select p.id, p.name, p.first_action, p.blurb, p.handle,
         p.handle_renamed_at, p.blurb_stale, p.place_type, p.hq_city,
         p.governance_state, p.handle_provisional_until
    into v_p
    from public.places p where p.id = p_place_id;

  if v_p.id is null then
    raise exception 'place_not_found';
  end if;

  return jsonb_build_object(
    'placeId',          v_p.id,
    'name',             v_p.name,
    'firstAction',      v_p.first_action,
    'blurb',            v_p.blurb,
    'blurbStale',       v_p.blurb_stale,
    'handle',           v_p.handle,
    'canRenameHandle',  v_p.handle_renamed_at is null,
    'placeType',        v_p.place_type,
    'hqCity',           v_p.hq_city,
    'governanceState',  v_p.governance_state,
    'handleProvisionalUntil', v_p.handle_provisional_until);
end;
$$;

revoke all on function public.community_settings(uuid) from public, anon;
grant execute on function public.community_settings(uuid) to authenticated, service_role;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.community_settings(uuid);
--   drop function if exists public.rename_community_handle(uuid, text);
--   drop function if exists public.update_community_settings(uuid, text, text, text);
--   drop function if exists public.is_community_operator(uuid, uuid);
--   Additive. No existing read or write path changes behaviour.
-- ============================================================================
