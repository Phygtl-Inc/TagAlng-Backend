-- Creator communities get the handle they reserved, without "-community"
--
-- WHY (Tommaso, 2026-10-04: "The creator can't have that.")
--
--   places_single_token_handle_guard ("option C", 20261228120004) allows a hyphen-free
--   handle only when the place holds a verified external identity with that username.
--   Nothing verifies external identities yet, so lana.help stored every one-word creator
--   handle as "{word}-community" (claim-handler.cjs activateCreatorCommunityDirect) and the
--   PWA mapped get.lana.help/{word} back onto it. Etiqueta do Reino is zenaidyndb-community.
--
--   The anti-squatting rule exists so nobody takes "nike" without proving they are Nike.
--   A creator who reserved the handle on lana.help and confirmed the reservation by email
--   (place_claims.verification_method = 'reservation_email', 20270104120000) already went
--   through the gate that matters for a creator: the reservation check refused protected
--   and taken names at reserve time, and the email ties the reservation to them. So that
--   claim now counts as ownership of exactly the reserved string — and nothing else.
--
-- WHAT
--   1. The guard accepts a second proof: a verified reservation_email claim on this
--      creator place whose reservation is exactly this handle. The member-handle collision
--      check and protected_handles still apply on top, unconditionally.
--   2. rename_community_handle accepts single tokens (the guard decides who may hold one),
--      and its taken check is fixed: `if <text>` raised a boolean cast error instead of
--      handle_taken whenever the name WAS taken.
--   3. Backfill: every creator place on "{reserved}-community" moves to "{reserved}". The
--      old handle is retired into place_handle_aliases, so links already in a bio keep
--      resolving forever. handle_renamed_at is NOT stamped — this is our fix, not the
--      creator's one rename.
--
-- ORDER: safe before or after lana.help / PWA. The current PWA still rewrites
-- get.lana.help/{word} to {word}-community, which now resolves through the alias.

-- ── 1 · the guard ───────────────────────────────────────────────────────────

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

  -- A bare token is the same shape as a member handle; one string, one owner.
  if exists (select 1 from public.users u where u.handle = new.handle) then
    raise exception 'handle_taken_by_user'
      using errcode = 'P0001',
            hint = 'That handle already belongs to a member. Use a compound handle (run-with-maya).';
  end if;

  -- Ownership is never a way around a protected word.
  if exists (select 1 from public.protected_handles h
              where h.normalized_handle = new.handle and h.active) then
    raise exception 'handle_protected'
      using errcode = 'P0001',
            hint = 'That handle is reserved.';
  end if;

  -- Proof A: a verified external identity with this exact username.
  if exists (
    select 1
    from public.external_community_identities e
    where e.place_id = new.id
      and e.ownership_verified_at is not null
      and lower(e.username) = new.handle
  ) then
    return new;
  end if;

  -- Proof B: the creator reserved exactly this string and confirmed it by email.
  if new.place_type = 'creator' and exists (
    select 1
    from public.place_claims c
    join public.place_handle_reservations r on r.id = c.reservation_id
    where c.place_id = new.id
      and c.status = 'verified'
      and c.verification_method = 'reservation_email'
      and r.normalized_handle = new.handle
  ) then
    return new;
  end if;

  raise exception 'single_token_handle_requires_matching_identity'
    using errcode = 'P0001',
          hint = 'Use a compound handle (run-with-maya), verify an external account with this '
                 'exact username, or reserve it on lana.help and confirm by email.';
end;
$$;

comment on function public.places_single_token_handle_guard() is
  'Option C, widened 20270106. A hyphen-free handle needs proof of the name: a verified '
  'external identity with that username, or (creator places) a verified reservation_email '
  'claim whose reservation is exactly that handle. Member handles and protected_handles '
  'are refused regardless of proof.';

-- ── 2 · rename accepts a single token; the guard decides ────────────────────

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
  -- A single token is a valid shape. Whether THIS community may hold it is
  -- places_single_token_handle_guard's call, on the update below.
  if v_new is null or v_new !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then
    raise exception 'handle_shape' using hint = 'Use letters and numbers, like acme-orlando.';
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
  if exists (select 1 from public.places p where p.handle = v_new) then
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
  'and keeps resolving forever. A single-token target must pass '
  'places_single_token_handle_guard (proof of the name).';

-- ── 3 · backfill: {reserved}-community → {reserved} ─────────────────────────

do $$
declare
  r record;
  v_moved int := 0;
  v_skipped int := 0;
begin
  for r in
    select p.id, p.handle as old_handle, res.normalized_handle as new_handle
    from public.places p
    join public.place_claims c
      on c.place_id = p.id
     and c.status = 'verified'
     and c.verification_method = 'reservation_email'
    join public.place_handle_reservations res on res.id = c.reservation_id
    where p.place_type = 'creator'
      and position('-' in res.normalized_handle) = 0
      and p.handle = res.normalized_handle || '-community'
  loop
    if exists (select 1 from public.places x where x.handle = r.new_handle)
       or exists (select 1 from public.users u where u.handle = r.new_handle)
       or exists (select 1 from public.place_handle_aliases a where a.handle = r.new_handle)
       or exists (select 1 from public.protected_handles h
                   where h.normalized_handle = r.new_handle and h.active) then
      raise notice 'creator_bare_handles: kept % (% is taken)', r.old_handle, r.new_handle;
      v_skipped := v_skipped + 1;
      continue;
    end if;

    update public.places set handle = r.new_handle, updated_at = now() where id = r.id;
    insert into public.place_handle_aliases (handle, place_id, retired_by)
    values (r.old_handle, r.id, null)
    on conflict (handle) do nothing;
    v_moved := v_moved + 1;
  end loop;

  raise notice 'creator_bare_handles: moved %, kept %', v_moved, v_skipped;
end;
$$;

-- ============================================================================
-- ROLLBACK
--   Restore places_single_token_handle_guard and rename_community_handle from
--   20261228120004 / 20261231120002. To undo the backfill for a place:
--     delete from public.place_handle_aliases where place_id = <id> and handle = '<x>-community';
--     update public.places set handle = '<x>-community' where id = <id>;
--   (alias first: the not-live trigger refuses an alias that is a live handle.)
-- ============================================================================
