-- Lana · a confirmed email completes the claim ──────────────────────────────────────
-- Product decision (2026-08-25, Asjid): the email path is self-serve. A claimant who
-- proves control of an inbox by entering a one-time code gets the handle published
-- immediately, with no reviewer in between. approve_place_claim stays for every other
-- method, which still has no mechanism to check it.
--
-- Stated plainly because it is a real trade: a confirmed inbox proves the person reads
-- that mailbox, NOT that they speak for the place. Anyone with any working email can
-- publish a handle for any location this way. The check that would close the gap is
-- comparing the address against the location's own published domain (CTO spec §9.8's
-- "matching work-domain email"), which is deliberately out of scope for now.
--
-- The trust boundary is the caller: this is service-role only. Our API calls it after,
-- and only after, Supabase has verified the one-time code. It is not reachable by a
-- signed-in user, so a claimant cannot invoke it to publish their own claim.

create or replace function public.complete_place_claim_by_email(p_token_hash text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
  r      public.place_handle_reservations;
  c      public.place_claims;
  v_gov  text;
  v      text;
  v_err  text;
begin
  select * into r
    from public.place_handle_reservations
   where token_hash = p_token_hash
     and status in ('active', 'bound')
     and expires_at > now()
   for update;

  if r.id is null then
    return jsonb_build_object('status', 'expired');
  end if;
  if r.user_id is null then
    return jsonb_build_object('status', 'not_signed_in');
  end if;
  if r.place_id is null then
    return jsonb_build_object('status', 'no_place');
  end if;

  select governance_state into v_gov
    from public.places where id = r.place_id for update;
  if v_gov = 'operator_verified' then
    return jsonb_build_object('status', 'already_claimed');
  end if;

  select * into c
    from public.place_claims
   where place_id = r.place_id
     and requested_by = r.user_id
     and status in ('draft', 'pending_verification', 'needs_more_info')
   order by created_at desc
   limit 1
   for update;

  if c.id is null then
    return jsonb_build_object('status', 'no_claim');
  end if;

  -- The reserved string is published only if it can be; otherwise the locality-derived
  -- handle, so "safeway" becomes safeway-foster-city rather than failing here.
  v := public.normalize_place_handle(coalesce(
         case when public._place_handle_shape_error(r.normalized_handle) is null
              then r.normalized_handle end,
         public.suggest_place_handle(r.place_id, r.normalized_handle)));

  v_err := public._place_handle_shape_error(v);
  if v_err is not null then
    return jsonb_build_object('status', 'bad_handle', 'reason', v_err);
  end if;
  if exists (select 1 from public.places where handle = v and id <> r.place_id)
     or exists (select 1 from public.protected_handles
                 where normalized_handle = v and active) then
    return jsonb_build_object('status', 'bad_handle', 'reason', 'taken');
  end if;

  update public.places
     set governance_state = 'operator_verified',
         handle           = v,
         verified_at      = now(),
         claimed_by       = r.user_id,
         claimed_at       = now(),
         source           = 'owner_claimed'
   where id = r.place_id;

  update public.place_claims
     set status              = 'verified',
         verification_method = 'domain_email',
         resolved_at         = now(),
         review_notes        = coalesce(review_notes, 'self-serve: one-time code confirmed')
   where id = c.id;

  update public.place_handle_reservations
     set status = 'consumed' where id = r.id;

  return jsonb_build_object(
    'status', 'verified', 'handle', v, 'placeId', r.place_id, 'claimId', c.id);
end;
$$;

comment on function public.complete_place_claim_by_email(text) is
  'Publishes a handle once the claimant has confirmed an email code. Service-role only: '
  'the API calls it after Supabase verifies the code, so a claimant cannot self-publish. '
  'Does NOT check the address against the location domain — see the header note.';

-- Service-role only. No grant to authenticated: that would let a claimant publish.
revoke execute on function public.complete_place_claim_by_email(text)
  from public, anon, authenticated;

-- ── the place may be picked before anyone has signed in ─────────────────────────
-- The claim flow now asks for an email at the verify step, not before it, so choosing the
-- building happens while the visitor is still anonymous. Screen 2 therefore cannot demand
-- auth.uid(): it is reached through our server, with the service role, holding a valid
-- reservation token. A signed-in caller still gets attributed as created_by; an anonymous
-- one leaves it null rather than being refused.
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
