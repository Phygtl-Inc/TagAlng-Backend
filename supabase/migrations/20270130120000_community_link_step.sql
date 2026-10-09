-- The community link is chosen as the LAST step of creating a community, before it exists
--
-- WHY (Asjid, 2026-10-06, with a mockup): "Last step to your community — choose a unique
-- link", prefilled and required, then the ready card shows it, then Share publishes. Until
-- now the link could only be claimed AFTER publishing (claim_community_handle needs a place
-- row), as an optional button that was easy to miss.
--
-- WHAT
--   check_community_handle_for(user, handle, name, google_place_id): may this user have
--   this link for the community they are about to create? Shape, availability (places,
--   protected, live holds, member handles, retired aliases) and — when the community's
--   place row already exists (a real Google place someone grounded, or a name-only
--   community published before) — whether it is theirs to claim at all. Suggestions come
--   from the community's NAME, compact first ("rosettasbakery"), so a prefill always exists.
--   check_community_handle(...) is the authenticated wrapper for the PWA's live check.
--
-- Nothing is reserved here: the claim happens at publish (claim_community_handle_for), and
-- a link taken in between falls back to the first suggestion there. The caller must be
-- signed in with a confirmed email, the same proof the claim needs.

create or replace function public._community_handle_name_suggestions(p_name text)
returns text[]
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v_dash    text := public.normalize_place_handle(p_name);
  v_compact text := replace(coalesce(v_dash, ''), '-', '');
  v_out     text[] := '{}';
  v_try     text;
  i         int;
begin
  foreach v_try in array array[v_compact, v_dash] loop
    exit when array_length(v_out, 1) >= 3;
    if v_try is not null and v_try <> ''
       and length(v_try) <= 48
       and public._place_handle_shape_error(v_try, false) is null
       and public._place_handle_unavailable(v_try) is null
       and not v_try = any(v_out) then
      v_out := v_out || v_try;
    end if;
  end loop;
  if v_compact <> '' then
    for i in 2..9 loop
      exit when array_length(v_out, 1) >= 3;
      v_try := v_compact || i::text;
      if length(v_try) <= 48 and public._place_handle_unavailable(v_try) is null
         and not v_try = any(v_out) then
        v_out := v_out || v_try;
      end if;
    end loop;
  end if;
  return v_out;
end;
$$;

revoke all on function public._community_handle_name_suggestions(text)
  from public, anon, authenticated;

create or replace function public.check_community_handle_for(
  p_user_id         uuid,
  p_handle          text,
  p_name            text default null,
  p_google_place_id text default null
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
  v       text := public.normalize_place_handle(p_handle);
  v_sugg  text[] := public._community_handle_name_suggestions(p_name);
  v_place public.places;
  v_why   text;
begin
  if p_user_id is null
     or not exists (
          select 1 from auth.users u
           where u.id = p_user_id
             and coalesce(u.is_anonymous, false) = false
             and u.email is not null
             and u.email_confirmed_at is not null) then
    return jsonb_build_object('status', 'sign_in_required');
  end if;

  -- The community's place may already exist. Then the link is only theirs to choose if
  -- the claim would succeed — otherwise say so now, not after they picked one.
  if coalesce(btrim(p_google_place_id), '') <> '' then
    select * into v_place from public.places p where p.google_place_id = btrim(p_google_place_id);
    if v_place.id is not null then
      if v_place.handle is not null then
        return jsonb_build_object('status', 'already_has_handle', 'handle', v_place.handle);
      end if;
      v_why := public._community_handle_claim_status(p_user_id, v_place.id);
      if v_why is not null then
        return jsonb_build_object('status', 'not_eligible', 'reason', v_why);
      end if;
    end if;
  end if;

  if v is null or btrim(coalesce(p_handle, '')) = '' then
    return jsonb_build_object(
      'status', 'invalid', 'reason', 'empty', 'suggestions', to_jsonb(v_sugg));
  end if;
  v_why := public._place_handle_shape_error(v, false);
  if v_why is not null then
    return jsonb_build_object(
      'status', 'invalid', 'reason', v_why, 'normalizedHandle', v,
      'suggestions', to_jsonb(v_sugg));
  end if;
  v_why := public._place_handle_unavailable(v);
  if v_why is not null then
    return jsonb_build_object(
      'status', 'unavailable', 'reason', v_why, 'normalizedHandle', v,
      'suggestions', to_jsonb(array(select s from unnest(v_sugg) s where s <> v)));
  end if;
  return jsonb_build_object('status', 'available', 'normalizedHandle', v);
end;
$$;

revoke all on function public.check_community_handle_for(uuid, text, text, text)
  from public, anon, authenticated;
grant execute on function public.check_community_handle_for(uuid, text, text, text)
  to service_role;

create or replace function public.check_community_handle(
  p_handle          text,
  p_name            text default null,
  p_google_place_id text default null
)
returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select public.check_community_handle_for(auth.uid(), p_handle, p_name, p_google_place_id);
$$;

revoke all on function public.check_community_handle(text, text, text) from public, anon;
grant execute on function public.check_community_handle(text, text, text) to authenticated;

comment on function public.check_community_handle(text, text, text) is
  'Live check for the "choose your community link" step, before the community exists. '
  'Never reserves: the link is claimed at publish (claim_community_handle_for).';

-- ============================================================================
-- ROLLBACK
--   drop function public.check_community_handle(text, text, text);
--   drop function public.check_community_handle_for(uuid, text, text, text);
--   drop function public._community_handle_name_suggestions(text);
-- ============================================================================
