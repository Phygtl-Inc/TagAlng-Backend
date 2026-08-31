-- Lana · choosing the building no longer needs a session ────────────────────────────
-- The claim flow asks for an email at the verify step, not before it, so Screen 2 happens
-- while the visitor is still anonymous. resolve_or_create_place refused that.
--
-- This lives in its own migration on purpose. The same change was appended to
-- 20261110120000 after that version had already been pushed, and Supabase only runs
-- versions absent from schema_migrations — so the edit silently did nothing. Twice now
-- (20261108120000's handle-derivation fix went the same way). Never amend an applied
-- migration; add one.
--
-- The reachability argument: this is only callable by the service role, from our server,
-- holding a valid reservation token. A signed-in caller is still recorded as created_by;
-- an anonymous one leaves it null rather than being turned away. Opening a claim still
-- requires an account, because a claim records who is claiming.
create or replace function public.resolve_or_create_place(
  p_google_place_id text,
  p_name            text,
  p_address         text default null,
  p_lat             double precision default null,
  p_lng             double precision default null,
  p_zip             text default null,
  p_sector          text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  v_gpid text := btrim(coalesce(p_google_place_id, ''));
  v_type text;
  v_id   uuid;
  v_new  boolean;
  pl     record;
begin
  if v_gpid = '' then
    raise exception 'resolve_or_create_place requires a google place id';
  end if;
  if btrim(coalesce(p_name, '')) = '' then
    raise exception 'resolve_or_create_place requires a name';
  end if;

  -- The website's six sectors map onto our narrower vocabulary; the operator's real intent
  -- lands in places.first_action, which is free text.
  v_type := case lower(coalesce(p_sector, ''))
              when 'church'          then 'faith'
              when 'faith'           then 'faith'
              when 'gym'             then 'fitness'
              when 'fitness'         then 'fitness'
              when 'school'          then 'school'
              when 'residential'     then 'neighborhood'
              when 'neighborhood'    then 'neighborhood'
              when 'community_space' then 'other'
              when 'supermarket'     then 'other'
              else null
            end;

  -- Insert first and let the unique index arbitrate, so two callers converge on one row
  -- instead of one of them erroring.
  insert into public.places (
    google_place_id, name, address, lat, lng, zip, place_type, source, created_by
  ) values (
    v_gpid, btrim(p_name), nullif(btrim(coalesce(p_address, '')), ''),
    p_lat, p_lng, nullif(btrim(coalesce(p_zip, '')), ''), v_type,
    'operator_submitted', auth.uid()
  )
  on conflict (google_place_id) do nothing
  returning id into v_id;

  v_new := v_id is not null;

  select p.id, p.name, p.governance_state
    into pl
    from public.places p
   where p.google_place_id = v_gpid;

  return jsonb_build_object(
    'placeId',         pl.id,
    'displayName',     pl.name,
    'governanceState', pl.governance_state,
    'created',         v_new,
    'matchState',      case
                         when v_new then 'created'
                         when pl.governance_state = 'operator_verified'
                           then 'existing_operator_verified'
                         when pl.governance_state = 'claim_pending'
                           then 'existing_claim_pending'
                         else 'existing_community_started'
                       end,
    'nextAction',      case
                         when pl.governance_state = 'operator_verified'
                           then 'already_claimed'
                         when v_new then 'create_claim'
                         else 'claim_existing'
                       end,
    'suggestedHandle', public.suggest_place_handle(pl.id));
end;
$$;

revoke execute on function public.resolve_or_create_place(
  text, text, text, double precision, double precision, text, text)
  from public, anon, authenticated;
