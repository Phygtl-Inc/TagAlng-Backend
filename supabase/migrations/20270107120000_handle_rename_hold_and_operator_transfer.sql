-- Handle rename respects live reservations · claimed_by transfer retires the old operator
--
-- Found in the 2026-10-05 QA pass over the Oct 1–5 changes.
--
-- 1 · RENAME COULD TAKE A HANDLE SOMEONE ELSE IS RESERVING
--   20270106120000 fixed rename_community_handle's cast error (`if <text>`) by replacing
--   `public._place_handle_taken(v_new)` with a bare `exists (… places where handle = v_new)`.
--   _place_handle_taken also answered 'held' for a live place_handle_reservations row, and
--   that check went with it. An operator could rename onto a word another creator was
--   part-way through reserving on lana.help, whose activation then failed on the unique
--   handle. The hold is honoured again — unless the hold is the caller's own, or already
--   bound to this very place.
--
-- 2 · MOVING claimed_by LEFT THE PREVIOUS CLAIMANT AS OPERATOR
--   places_sync_operator (20270104120000) only ever inserts. When claimed_by moves to a new
--   person, the old claimant kept an operator row and with it settings and the one rename.
--   The old row is now retired — but only a row that claimed_by itself created
--   ('claimed_by_sync' / 'backfill_claimed_by'). An operator added any other way was a
--   deliberate grant and is not this trigger's to take away. When claimed_by moves BACK,
--   that same kind of row is revived, so the owner is never left without one.

-- ── 1 · rename_community_handle ─────────────────────────────────────────────────

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
  -- Someone else's live hold on the word. Reported as handle_taken: to the person
  -- renaming it is simply not available, and callers already handle that code.
  if exists (select 1 from public.place_handle_reservations r
              where r.normalized_handle = v_new
                and r.status in ('active', 'bound')
                and r.expires_at > now()
                and r.user_id is distinct from v_uid
                and r.place_id is distinct from p_place_id) then
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

-- ── 2 · places_sync_operator ────────────────────────────────────────────────────

create or replace function public.places_sync_operator()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'UPDATE'
     and old.claimed_by is not null
     and old.claimed_by is distinct from new.claimed_by then
    update public.place_managers m
       set removed_at = now()
     where m.place_id = new.id
       and m.user_id = old.claimed_by
       and m.role = 'operator'
       and m.removed_at is null
       and m.verification_method in ('claimed_by_sync', 'backfill_claimed_by');
  end if;
  if new.claimed_by is not null
     and (tg_op = 'INSERT' or new.claimed_by is distinct from old.claimed_by) then
    -- A claimant whose earlier claimed_by row was retired by a transfer AWAY gets it back
    -- when ownership returns: claimed_by moving to them is the grant. Without this the
    -- on-conflict no-op left the owner with no operator row at all (A→B→A, QA 2026-10-05).
    -- Only a row claimed_by made is revived; a manual grant someone removed stays removed.
    insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
    values (new.id, new.claimed_by, 'operator', 'claimed_by_sync',
            coalesce(new.claimed_at, now()), new.claimed_by)
    on conflict (place_id, user_id) do update
       set removed_at = null, role = 'operator'
     where public.place_managers.removed_at is not null
       and public.place_managers.verification_method in ('claimed_by_sync', 'backfill_claimed_by');
  end if;
  return new;
end;
$$;

revoke all on function public.places_sync_operator() from public, anon, authenticated;
