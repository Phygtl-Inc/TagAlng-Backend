-- Community operators, test places, and claim methods
--
-- Replaces Tommaso's #174 (and the claim-method half of #173). Same diagnosis, all of it
-- confirmed; the fixes below differ where #174 would have broken something live.
--
-- WHAT IS WRONG TODAY (reported 2026-10-01)
--
--   1. NOBODY CAN EDIT A CREATOR COMMUNITY. lana-help's activation writes places.claimed_by
--      and never place_managers, so is_community_operator() is false for every creator who
--      activated after 20261228120003's one-time backfill. Etiqueta do Reino is one of them.
--   2. TEST PLACES SIT IN DISCOVERY next to the pilot creator and inflate counts.
--   3. CREATOR CLAIMS RECORD THEIR METHOD IN PROSE. Activation writes
--      verification_method = null with review_notes "email-confirmed owner of the bound
--      handle reservation". No allowed value describes that mechanism.
--
-- WHERE THIS DIFFERS FROM #174, AND WHY
--
--   · The verification_method CHECK keeps every existing value. #174 rewrote the list and
--     dropped manual_review / admin_approval / platform_oauth, which lana-help writes on
--     every location claim (claim-handler.cjs METHOD_BY_UI). That would have failed the
--     migration on any existing row, or every future manual claim.
--   · is_test is filtered where discovery actually runs: discover_communities_near and
--     discover_communities_semantic. Nothing calls discover_communities (#172) today; it
--     gets the same predicates so it is right when something does.
--   · The same three functions also drop chapters (parent_place_ref is not null). #176
--     flagged the leak on discover_communities only; the live path is _near, because a
--     chapter must have lat/lng. Doing it here means #176 never re-emits these functions.
--   · Only claims whose notes say what happened are backfilled. #174 also stamped every
--     other null as manual_founder, which invents a method — #173 argued against exactly
--     that, and it is right.
--   · The two audit views are service_role only. A plain view in public runs with its
--     owner's rights and is granted to anon by default privileges, so #174/#173's views
--     would have published claimed_by user ids to the internet.
--   · The "-community" strip is NOT here. places_single_token_handle_guard (20261228120004,
--     "option C") rejects a hyphen-free handle unless the community holds a verified
--     external identity with that username, so #174's strip aborts on its first row. Lifting
--     that is a policy decision, not a fix — see the PR.

-- ── 1 · test places ─────────────────────────────────────────────────────────

alter table public.places
  add column if not exists is_test boolean not null default false;

comment on column public.places.is_test is
  'Test or demo data. Still resolves from a direct link, but excluded from discovery and '
  'from any public listing or count. Flip it rather than deleting.';

create index if not exists places_is_test_idx on public.places (id) where is_test;

-- Exact handles, never a pattern: a real creator whose handle contains "test" must not
-- disappear because a regex matched it.
update public.places
   set is_test = true, updated_at = now()
 where handle in ('test-7', 'test7-community', 'asjidtest5-community', 'asjid-test-6',
                  'mrbeast-community')
   and not is_test;

-- service_role only: marking a live community as test hides it from discovery, which
-- should never be one tap away for an operator.
create or replace function public.set_place_test_flag(p_place_id uuid, p_is_test boolean)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
begin
  update public.places
     set is_test = coalesce(p_is_test, false), updated_at = now()
   where id = p_place_id;
  if not found then
    raise exception 'place_not_found';
  end if;
  return coalesce(p_is_test, false);
end;
$$;

revoke all on function public.set_place_test_flag(uuid, boolean) from public, anon, authenticated;
grant execute on function public.set_place_test_flag(uuid, boolean) to service_role;

-- ── 2 · discovery skips test places and chapters ────────────────────────────
--
-- Each body below is the current one with comments trimmed, logic unchanged, plus the two
-- predicates marked ADDED.

-- discover_communities_near · body from 20261228120001
create or replace function public.discover_communities_near(
  p_user_id       uuid,
  p_radius_meters double precision default 8000,
  p_limit         integer default 20,
  p_locale        text default 'en'::text,
  p_query         text default null::text,
  p_lat           double precision default null::double precision,
  p_lng           double precision default null::double precision
)
returns table(
  place_id uuid, name text, address text, place_type text, zip text,
  lat double precision, lng double precision, member_count integer,
  member_types text[], distance_meters double precision, distance_text text,
  is_member boolean
)
language plpgsql
stable
security definer
set search_path to 'pg_catalog', 'public', 'extensions'
as $function$
declare
  v_origin extensions.geography;
  v_zip5   text;
begin
  if p_user_id is null then
    raise exception 'user_id_required' using errcode = 'P0001';
  end if;

  if p_lat is not null and p_lng is not null
     and p_lat between -90 and 90
     and p_lng between -180 and 180 then
    v_origin := extensions.st_setsrid(
                  extensions.st_makepoint(p_lng, p_lat), 4326
                )::extensions.geography;
  else
    select o.origin, o.zip5 into v_origin, v_zip5
    from public.user_origin_point(p_user_id) o;
  end if;

  return query
  with visible_members as (
    select vm.place_ref, vm.user_id, vm.circle_type
    from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select
      vm.place_ref                                        as pid,
      count(distinct vm.user_id)::int                     as members,
      array_agg(distinct vm.circle_type)                  as types,
      bool_or(vm.user_id = p_user_id)                     as mine
    from visible_members vm
    group by vm.place_ref
  ),
  located as (
    select
      c.pid, c.members, c.types, c.mine,
      p.name, p.address, p.place_type, p.zip,
      p.lat, p.lng,
      case
        when v_origin is null or p.lat is null or p.lng is null then null
        else extensions.st_distance(
               v_origin,
               extensions.st_setsrid(extensions.st_makepoint(p.lng, p.lat), 4326)::extensions.geography
             )
      end as meters
    from counted c
    join public.places p on p.id = c.pid
    where c.members > 0
      and (p_query is null or p_query = '' or p.name ilike '%' || p_query || '%')
      and not p.is_test                    -- ADDED
      and p.parent_place_ref is null       -- ADDED: a chapter is found from its parent
  )
  select
    l.pid,
    l.name,
    l.address,
    l.place_type,
    l.zip,
    l.lat,
    l.lng,
    l.members,
    l.types,
    l.meters,
    public.humanize_distance_text(l.meters, p_locale),
    l.mine
  from located l
  where
    case
      when v_origin is not null then
        (l.meters is not null and l.meters <= p_radius_meters)
        or (l.meters is null and v_zip5 is not null and l.zip = v_zip5
            and l.address is not null)
      else v_zip5 is not null and l.zip = v_zip5 and l.address is not null
    end
  order by l.members desc, coalesce(l.meters, 1e9) asc, l.name asc
  limit greatest(coalesce(p_limit, 20), 1);
end;
$function$;

-- discover_communities_semantic · body from 20261225120000
create or replace function public.discover_communities_semantic(
  p_user_id          uuid,
  p_query_embedding  extensions.vector(768),
  p_min_similarity   real default 0.55,
  p_limit            int  default 5,
  p_creator_only     boolean default true
)
returns table (
  place_id        uuid,
  name            text,
  place_type      text,
  hq_city         text,
  hq_lat          double precision,
  hq_lng          double precision,
  member_count    int,
  is_member       boolean,
  matched_label   text,
  similarity      real
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with visible as (
    select vm.place_ref, vm.user_id
    from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select
      v.place_ref                      as pid,
      count(distinct v.user_id)::int   as members,
      bool_or(v.user_id = p_user_id)   as mine
    from visible v
    group by v.place_ref
  ),
  scored as (
    select
      v.place_ref                                        as pid,
      c.label                                            as label,
      (1 - (c.embedding <=> p_query_embedding))::real    as sim
    from visible v
    join public.user_identity_claims c
      on c.user_id = v.user_id
     and c.dismissed_at is null
     and c.transient = false
     and c.disclosure = 'public'
     and c.subject_kind = 'self'
     and c.embedding is not null
  ),
  best as (
    select distinct on (s.pid) s.pid, s.label, s.sim
    from scored s
    where s.sim >= p_min_similarity
    order by s.pid, s.sim desc
  )
  select
    b.pid,
    p.name,
    p.place_type,
    p.hq_city,
    p.hq_lat,
    p.hq_lng,
    c.members,
    coalesce(c.mine, false),
    b.label,
    b.sim
  from best b
  join counted c            on c.pid = b.pid
  join public.places p      on p.id  = b.pid
  where c.members > 0
    and (not p_creator_only or p.place_type = 'creator')
    and not p.is_test                      -- ADDED
    and p.parent_place_ref is null         -- ADDED
  order by b.sim desc, c.members desc, p.name asc
  limit greatest(1, least(coalesce(p_limit, 5), 20));
$$;

-- discover_communities · body from 20261231120003 (#172). No caller yet.
create or replace function public.discover_communities(
  p_user_id         uuid                    default null,
  p_query_embedding extensions.vector(768)  default null,
  p_min_similarity  real                    default 0.55,
  p_limit           int                     default 10,
  p_creator_only    boolean                 default false,
  p_include_mine    boolean                 default false
)
returns table (
  place_id      uuid,
  name          text,
  place_type    text,
  hq_city       text,
  hq_lat        double precision,
  hq_lng        double precision,
  members       int,
  is_mine       boolean,
  match_label   text,
  match_kind    text,
  similarity    real,
  is_new        boolean
)
language sql
stable
security definer
set search_path = pg_catalog, public, extensions
as $$
  with me as (
    select coalesce(auth.uid(), p_user_id) as uid
  ),
  visible as (
    select vm.place_ref, vm.user_id
    from me, public.visible_place_members(me.uid) vm
  ),
  counted as (
    select v.place_ref as pid,
           count(distinct v.user_id)::int as members,
           bool_or(v.user_id = (select uid from me)) as mine
    from visible v group by v.place_ref
  ),
  by_claim as (
    select distinct on (s.pid) s.pid, s.lbl, s.kind, s.sim
    from (
      select v.place_ref as pid, c.label as lbl, 'member_claim'::text as kind,
             (1 - (c.embedding <=> p_query_embedding))::real as sim
      from visible v
      join public.user_identity_claims c
        on c.user_id = v.user_id
       and c.dismissed_at is null and c.transient = false
       and c.disclosure = 'public' and c.subject_kind = 'self'
       and c.embedding is not null
    ) s
    where s.sim >= p_min_similarity
    order by s.pid, s.sim desc
  ),
  by_blurb as (
    select p.id as pid, p.blurb as lbl, 'blurb'::text as kind,
           (1 - (p.blurb_embedding <=> p_query_embedding))::real as sim
    from public.places p
    where p.blurb_embedding is not null
      and p.governance_state in ('community_started','operator_verified')
  ),
  merged as (
    select * from by_claim
    union all
    select * from by_blurb b
    where b.sim >= p_min_similarity
      and not exists (select 1 from by_claim c where c.pid = b.pid)
  )
  select
    m.pid, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
    coalesce(c.members, 0), coalesce(c.mine, false),
    m.lbl, m.kind, m.sim,
    coalesce(c.members, 0) <= 1 as is_new
  from merged m
  join public.places p on p.id = m.pid
  left join counted c  on c.pid = m.pid
  where p_query_embedding is not null
    and (not p_creator_only or p.place_type = 'creator')
    and (p_include_mine or not coalesce(c.mine, false))
    and p.governance_state <> 'suspended'
    and not p.is_test                      -- ADDED
    and p.parent_place_ref is null         -- ADDED
  order by (m.kind = 'member_claim') desc, m.sim desc, coalesce(c.members,0) desc, p.name
  limit greatest(1, least(coalesce(p_limit, 10), 50));
$$;

-- create or replace keeps existing grants; restated so this file reads complete.
revoke all on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  from public, anon, authenticated;
grant execute on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  to authenticated, service_role;

-- ── 3 · resolve_place_handle says when a place is test data ─────────────────
--
-- Body from 20261231120001 (#172) plus `is_test` in both selects and 'isTest' in the
-- payload. tagalng-pwa #99 reads `r.isTest === true`.

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
  v_creator_name   text;
  v_creator_avatar text;
begin
  select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
         p.governance_state, p.blurb, p.hq_city, p.is_test
    into v_place
    from public.places p
   where p.handle = v_in
     and p.governance_state = 'operator_verified';

  if v_place.id is null then
    select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
           p.governance_state, p.blurb, p.hq_city, p.is_test
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

  if v_place.place_type = 'creator' then
    select
      coalesce(
        (select f.value from public.place_features f
          where f.place_id = v_place.id and f.key = 'creator_name'
            and nullif(btrim(coalesce(f.value, '')), '') is not null
          order by f.confidence desc nulls last, f.created_at desc limit 1),
        op.nickname) as display_name,
      coalesce(
        (select f.value from public.place_features f
          where f.place_id = v_place.id and f.key = 'creator_avatar_url'
            and nullif(btrim(coalesce(f.value, '')), '') is not null
          order by f.confidence desc nulls last, f.created_at desc limit 1),
        op.profile_photo_url) as avatar_url
      into v_creator_name, v_creator_avatar
      from (select 1) one
      left join lateral (
        select u.nickname, u.profile_photo_url
          from public.place_managers m
          join public.users u on u.id = m.user_id
         where m.place_id = v_place.id
           and m.role = 'operator'
           and m.removed_at is null
         order by m.created_at asc
         limit 1) op on true;
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
    'viaAlias',         v_alias,
    'isTest',           coalesce(v_place.is_test, false),   -- ADDED
    'creator',          case
                          when v_creator_name is null
                           and v_creator_avatar is null then null
                          else jsonb_build_object(
                                 'displayName', v_creator_name,
                                 'avatarUrl',   v_creator_avatar)
                        end);
end;
$$;

-- ── 4 · a method for creator self-activation ────────────────────────────────
--
-- Every value from 20261228120004 kept, one added. domain_email does not fit: it means a
-- business domain, and this is a personal mailbox that owns the handle reservation.

alter table public.place_claims
  drop constraint if exists place_claims_verification_method_check;

alter table public.place_claims
  add constraint place_claims_verification_method_check check (
    verification_method is null or verification_method in (
      'domain_email',
      'profile_backlink',
      'platform_oauth',
      'admin_approval',
      'manual_review',
      'manual_founder',
      'reservation_email'  -- confirmed email owner of the bound handle reservation
    )
  );

-- Only rows whose own notes state the mechanism. Anything else stays null and visible in
-- claims_missing_method until someone establishes what was actually checked.
update public.place_claims
   set verification_method = 'reservation_email', updated_at = now()
 where status = 'verified'
   and verification_method is null
   and review_notes like 'Self-verified at creator activation: email-confirmed owner of the bound handle reservation.%';

-- ── 5 · every claimed place has an operator ─────────────────────────────────

insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
select p.id, p.claimed_by, 'operator', 'backfill_claimed_by', p.claimed_at, p.claimed_by
from public.places p
where p.claimed_by is not null
on conflict (place_id, user_id) do nothing;

-- A trigger rather than a lana-help change: claimed_by is written from more than one path
-- (lana-help activation, the claim RPCs), and a caller cannot forget a trigger. It never
-- revives a removed manager — unique(place_id, user_id) makes that insert a no-op, which
-- is right: an operator who was removed should not come back because a pointer moved.
create or replace function public.places_sync_operator()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
begin
  if new.claimed_by is not null
     and (tg_op = 'INSERT' or new.claimed_by is distinct from old.claimed_by) then
    insert into public.place_managers (place_id, user_id, role, verification_method, verified_at, added_by)
    values (new.id, new.claimed_by, 'operator', 'claimed_by_sync',
            coalesce(new.claimed_at, now()), new.claimed_by)
    on conflict (place_id, user_id) do nothing;
  end if;
  return new;
end;
$$;

revoke all on function public.places_sync_operator() from public, anon, authenticated;

drop trigger if exists places_sync_operator_trg on public.places;
create trigger places_sync_operator_trg
  after insert or update of claimed_by on public.places
  for each row execute function public.places_sync_operator();

comment on function public.places_sync_operator() is
  'Keeps place_managers in step with the legacy places.claimed_by pointer. lana-help '
  'activation writes only claimed_by, which left creator communities with no operator and '
  'no way to open their own settings.';

-- ── 6 · audit views · service_role only ─────────────────────────────────────

create or replace view public.places_without_operator
with (security_invoker = true) as
select p.id as place_id, p.name, p.handle, p.place_type, p.governance_state, p.claimed_by
from public.places p
where p.handle is not null
  and not exists (
    select 1 from public.place_managers m
     where m.place_id = p.id and m.removed_at is null);

comment on view public.places_without_operator is
  'Published places nobody can administer. Should be empty.';

create or replace view public.claims_missing_method
with (security_invoker = true) as
select c.id as claim_id, p.id as place_id, p.name as place_name, p.handle, p.is_test,
       c.resolved_at, c.review_notes
from public.place_claims c
join public.places p on p.id = c.place_id
where c.status = 'verified'
  and c.verification_method is null;

comment on view public.claims_missing_method is
  'Verified claims with no recorded method. Should trend to zero; once it holds only rows '
  'someone has signed off, place_claims can take a NOT VALID verified-has-method check.';

revoke all on public.places_without_operator, public.claims_missing_method
  from public, anon, authenticated;
grant select on public.places_without_operator, public.claims_missing_method to service_role;

-- ============================================================================
-- ROLLBACK
--   drop view if exists public.claims_missing_method, public.places_without_operator;
--   drop trigger if exists places_sync_operator_trg on public.places;
--   drop function if exists public.places_sync_operator();
--   -- place_claims CHECK: restore the 20261228120004 list (fails while any row uses
--   --   reservation_email; null those first).
--   -- discover_communities_near / _semantic / discover_communities / resolve_place_handle:
--   --   restore from 20261228120001 / 20261225120000 / 20261231120003 / 20261231120001.
--   drop function if exists public.set_place_test_flag(uuid, boolean);
--   alter table public.places drop column if exists is_test;
--   Backfilled place_managers rows describe who actually owns each place; keep them.
-- ============================================================================
