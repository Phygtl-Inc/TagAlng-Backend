-- Renaming a community to a one-word link mints the proof for that word
--
-- WHY (Asjid, 2026-10-09)
--   The edit sheet refused get.lana.help/podcaster for the "Podcasters" community with
--   "One word is reserved for verified places". A bare word needs proof of the name
--   (_place_handle_proven), and proof is tied to one exact string. claim_community_handle
--   (20270108120000) mints that proof in one step for an operator or starter with a
--   confirmed email, but rename_community_handle only looked for proof that already
--   existed. So the same person who could claim a bare word could never change to another
--   one: every bare rename failed at places_single_token_handle_guard.
--
-- WHAT
--   _handle_email_confirmed(user): signed in, not anonymous, email confirmed. The same test
--     _community_handle_claim_status applies; the email is the proof.
--   rename_community_handle: a bare target with no proof yet, renamed by an operator with a
--     confirmed email, writes the same two rows claim_community_handle writes (a consumed
--     in_app reservation + a verified reservation_email claim) before the update, so the
--     guard's Proof B passes. Without a confirmed email it raises handle_needs_confirmed_email.
--   check_community_link: says available where the rename would now succeed, and
--     sign_in_required where it would not.
--
-- UNCHANGED
--   The old link's proof is kept: the old link stays alive as an alias, and its proof keeps
--   the retired word out of anyone else's hands. One rename, protected words, member
--   handles, taken / held / retired words: all refused exactly as before, by the same checks
--   and by the guard. Chapter links have no single-word rule and are untouched.

-- ── 1 · the email test ──────────────────────────────────────────────────────────

create or replace function public._handle_email_confirmed(p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select p_user_id is not null
     and exists (
           select 1 from auth.users u
            where u.id = p_user_id
              and coalesce(u.is_anonymous, false) = false
              and u.email is not null
              and u.email_confirmed_at is not null);
$$;

revoke all on function public._handle_email_confirmed(uuid) from public, anon, authenticated;

-- ── 2 · rename_community_handle · body from 20270107120000, proof minting ADDED ──

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
  v_type text;
  v_res uuid;
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;
  if not public.is_community_operator(p_place_id, v_uid) then
    raise exception 'not_operator';
  end if;

  -- Locked, so two taps cannot both spend the one rename.
  select p.handle, p.handle_renamed_at, p.governance_state, p.place_type
    into v_old, v_renamed, v_state, v_type
    from public.places p where p.id = p_place_id
     for update;

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

  -- A bare word with no proof yet: the operator's confirmed email is the proof, exactly as
  -- claim_community_handle_for mints it. A member's handle is refused first, so no proof is
  -- written for a word the guard would reject anyway.
  if position('-' in v_new) = 0 and not public._place_handle_proven(p_place_id, v_new) then
    if exists (select 1 from public.users u where u.handle = v_new) then
      raise exception 'handle_taken_by_user';
    end if;
    if not public._handle_email_confirmed(v_uid) then
      raise exception 'handle_needs_confirmed_email'
        using hint = 'Confirm your email to use a one-word link, or add a second word.';
    end if;

    insert into public.place_handle_reservations (
      normalized_handle, token_hash, user_id, place_id, status, source,
      requested_place_type, expires_at
    ) values (
      v_new, encode(sha256(convert_to(gen_random_uuid()::text, 'UTF8')), 'hex'),
      v_uid, p_place_id, 'consumed', 'in_app', v_type, now()
    )
    returning id into v_res;

    insert into public.place_claims (
      place_id, reservation_id, requested_by, role_title, status, verification_method,
      review_notes, submitted_at, resolved_at
    ) values (
      p_place_id, v_res, v_uid, 'Operator', 'verified', 'reservation_email',
      'In-app link rename by the community''s operator, signed in with a confirmed email.',
      now(), now()
    );
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
  'Change a community''s handle, once. The old handle becomes an alias and keeps resolving. '
  'A bare (hyphen-free) target with no proof yet gets one minted from the operator''s '
  'confirmed email (as claim_community_handle does); places_single_token_handle_guard still '
  'has the last word.';

-- ── 3 · check_community_link · body from 20270131120000, bare-word branch CHANGED ─

create or replace function public.check_community_link(p_place_id uuid, p_handle text)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_uid    uuid := auth.uid();
  v        text := public.normalize_place_handle(p_handle);
  v_p      public.places%rowtype;
  v_parent public.places%rowtype;
  v_kind   text := 'community';
  v_why    text;
begin
  select * into v_p from public.places where id = p_place_id;
  if v_p.id is not null and v_p.parent_place_ref is not null then
    select * into v_parent from public.places where id = v_p.parent_place_ref;
    if v_parent.handle is not null then
      v_kind := 'chapter';
    end if;
  end if;

  if v_uid is null or v_p.id is null
     or not (public.is_community_operator(v_p.id, v_uid)
             or (v_parent.id is not null
                 and public.is_community_operator(v_parent.id, v_uid))) then
    return jsonb_build_object('status', 'not_operator', 'kind', v_kind);
  end if;

  if v is null or v !~ '^[a-z0-9]+(-[a-z0-9]+)*$' then
    return jsonb_build_object('status', 'invalid', 'reason', 'malformed',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  if length(v) < 3 then
    return jsonb_build_object('status', 'invalid', 'reason', 'too_short',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  if length(v) > 48 then
    return jsonb_build_object('status', 'invalid', 'reason', 'too_long',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;

  if v_kind = 'chapter' then
    if v = v_p.chapter_handle then
      return jsonb_build_object('status', 'unchanged', 'normalizedHandle', v, 'kind', v_kind);
    end if;
    if v_p.chapter_handle_renamed_at is not null then
      return jsonb_build_object('status', 'rename_used', 'normalizedHandle', v, 'kind', v_kind);
    end if;
    v_why := public._chapter_handle_unavailable(v_parent.id, v, v_p.id);
    if v_why is not null then
      return jsonb_build_object('status', 'unavailable', 'reason', v_why,
                                'normalizedHandle', v, 'kind', v_kind);
    end if;
    return jsonb_build_object('status', 'available', 'normalizedHandle', v, 'kind', v_kind);
  end if;

  -- A community's global handle: rename_community_handle (20270107120000) in order, then
  -- what places_single_token_handle_guard would refuse on its update.
  if v = v_p.handle then
    return jsonb_build_object('status', 'unchanged', 'normalizedHandle', v, 'kind', v_kind);
  end if;
  if v_p.handle_renamed_at is not null then
    return jsonb_build_object('status', 'rename_used', 'normalizedHandle', v, 'kind', v_kind);
  end if;
  v_why := case
    when exists (select 1 from public.protected_handles h
                  where h.normalized_handle = v and h.active) then 'protected'
    when exists (select 1 from public.places p where p.handle = v) then 'taken'
    when exists (select 1 from public.place_handle_reservations r
                  where r.normalized_handle = v
                    and r.status in ('active', 'bound')
                    and r.expires_at > now()
                    and r.user_id is distinct from v_uid
                    and r.place_id is distinct from v_p.id) then 'held'
    when exists (select 1 from public.place_handle_aliases a where a.handle = v) then 'retired'
    when position('-' in v) = 0
     and exists (select 1 from public.users u where u.handle = v) then 'member'
  end;
  if v_why is not null then
    return jsonb_build_object('status', 'unavailable', 'reason', v_why,
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  -- A bare word needs proof of the name. The rename mints that proof for an operator with
  -- a confirmed email, so only someone without one is refused here.
  if position('-' in v) = 0
     and not public._place_handle_proven(v_p.id, v)
     and not public._handle_email_confirmed(v_uid) then
    return jsonb_build_object('status', 'invalid', 'reason', 'sign_in_required',
                              'normalizedHandle', v, 'kind', v_kind);
  end if;
  return jsonb_build_object('status', 'available', 'normalizedHandle', v, 'kind', v_kind);
end;
$$;

revoke all on function public.check_community_link(uuid, text) from public, anon, authenticated;
grant execute on function public.check_community_link(uuid, text) to authenticated, service_role;

comment on function public.check_community_link(uuid, text) is
  'Live check for the community edit sheet''s link field. Dispatches to the chapter link '
  '(within its parent) or the global handle, deciding exactly as rename_chapter_handle / '
  'rename_community_handle would. Never writes.';

-- DOWN
--   Restore rename_community_handle from 20270107120000 and check_community_link from
--   20270131120000; drop function public._handle_email_confirmed(uuid). Proof rows minted by
--   renames stay: they prove words the communities now hold.
