-- Test places on production, without polluting production
--
-- WHY (2026-10-01)
--
--   Five of the fourteen live handles are test rows:
--
--     test-7                 Asjid Test
--     test7-community        run with asjid
--     asjidtest5-community   Running with asjid
--     asjid-test-6           Running with asjid
--     mrbeast-community      MrBeast
--
--   They sit in discovery next to Etiqueta do Reino, which is our actual pilot
--   creator. They also inflate every count we might show someone.
--
--   The obvious answer is "don't test on prod", and the obvious answer is wrong here:
--   the team needs to exercise real flows against real data, and a separate
--   environment is a bigger lift than the problem deserves right now.
--
--   So: one boolean. A test place still exists, still resolves from a direct link,
--   and is visible to anyone who holds that link. It simply does not appear in
--   anything that lists or counts places for the public.
--
--   TRANSPARENT ON PURPOSE. resolve_place_handle returns isTest so the client can say
--   so out loud. A test place that is indistinguishable from a real one is how test
--   data ends up in a screenshot in a pitch deck.

alter table public.places
  add column if not exists is_test boolean not null default false;

comment on column public.places.is_test is
  'Test or demo data. Still resolves from a direct link and still visible to whoever '
  'holds it, but excluded from discovery and from any public listing or count. Flip it '
  'rather than deleting: these rows are useful, they just should not be found by '
  'accident.';

create index if not exists places_is_test_idx on public.places (is_test) where is_test;

-- ── mark what is already there ──────────────────────────────────────────────
--
-- Matched by name AND handle so a real community called "Testa" or a creator whose
-- handle contains "test" is not caught. Conservative: it is cheap to flag one more
-- later, and expensive to hide a real creator.

update public.places
   set is_test = true, updated_at = now()
 where handle in (
   'test-7', 'test7-community', 'asjidtest5-community', 'asjid-test-6', 'mrbeast-community'
 );

-- ── toggling ────────────────────────────────────────────────────────────────

create or replace function public.set_place_test_flag(p_place_id uuid, p_is_test boolean)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
declare v_uid uuid := auth.uid();
begin
  if v_uid is null then
    raise exception 'not_authenticated';
  end if;
  if not public.is_community_operator(p_place_id, v_uid) then
    raise exception 'not_operator';
  end if;

  update public.places
     set is_test = coalesce(p_is_test, false), updated_at = now()
   where id = p_place_id;

  return coalesce(p_is_test, false);
end;
$$;

revoke all on function public.set_place_test_flag(uuid, boolean) from public, anon;
grant execute on function public.set_place_test_flag(uuid, boolean) to authenticated, service_role;

-- ── keep test places out of discovery ───────────────────────────────────────
--
-- Only change from the version in 20261231120003: `and not p.is_test`. Everything
-- else is byte-identical, so the diff is the filter.

create or replace function public.discover_communities(
  p_user_id         uuid,
  p_query_embedding extensions.vector(768),
  p_min_similarity  real    default 0.55,
  p_limit           int     default 10,
  p_creator_only    boolean default false,
  p_include_mine    boolean default false
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
  with visible as (
    select vm.place_ref, vm.user_id from public.visible_place_members(p_user_id) vm
  ),
  counted as (
    select v.place_ref as pid,
           count(distinct v.user_id)::int as members,
           bool_or(v.user_id = p_user_id) as mine
    from visible v group by v.place_ref
  ),
  by_claim as (
    select distinct on (v.place_ref)
      v.place_ref as pid, c.label as lbl, 'member_claim'::text as kind,
      (1 - (c.embedding <=> p_query_embedding))::real as sim
    from visible v
    join public.user_identity_claims c
      on c.user_id = v.user_id
     and c.dismissed_at is null and c.transient = false
     and c.disclosure = 'public' and c.subject_kind = 'self'
     and c.embedding is not null
    order by v.place_ref, c.embedding <=> p_query_embedding
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
    where not exists (select 1 from by_claim c where c.pid = b.pid)
  )
  select
    m.pid, p.name, p.place_type, p.hq_city, p.hq_lat, p.hq_lng,
    coalesce(c.members, 0), coalesce(c.mine, false),
    m.lbl, m.kind, m.sim,
    coalesce(c.members, 0) <= 1 as is_new
  from merged m
  join public.places p on p.id = m.pid
  left join counted c  on c.pid = m.pid
  where m.sim >= p_min_similarity
    and (not p_creator_only or p.place_type = 'creator')
    and (p_include_mine or not coalesce(c.mine, false))
    and p.governance_state <> 'suspended'
    -- Test data does not belong next to the pilot creator in a discovery list.
    and not p.is_test
  order by (m.kind = 'member_claim') desc, m.sim desc, coalesce(c.members,0) desc, p.name
  limit greatest(1, least(coalesce(p_limit, 10), 50));
$$;

revoke all on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  from public, anon;
grant execute on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  to authenticated, service_role;

-- ── resolve still works, but says what it is ────────────────────────────────

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
  v_creator record;
begin
  select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
         p.governance_state, p.blurb, p.hq_city, p.claimed_by, p.is_test
    into v_place
    from public.places p
   where p.handle = v_in
     and p.governance_state = 'operator_verified';

  if v_place.id is null then
    select p.id, p.handle, p.name, p.place_type, p.zip, p.first_action,
           p.governance_state, p.blurb, p.hq_city, p.claimed_by, p.is_test
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
    select u.id, u.nickname, u.profile_photo_url
      into v_creator
      from public.place_managers m
      join public.users u on u.id = m.user_id
     where m.place_id = v_place.id
       and m.role = 'operator'
       and m.removed_at is null
     order by m.created_at asc
     limit 1;
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
    -- Said out loud on purpose. A test place that looks real is how test data ends
    -- up in a screenshot somebody shows an investor.
    'isTest',           coalesce(v_place.is_test, false),
    'creator',          case
                          when v_creator.id is null then null
                          else jsonb_build_object(
                                 'displayName', v_creator.nickname,
                                 'avatarUrl',   v_creator.profile_photo_url)
                        end);
end;
$$;

-- ============================================================================
-- ROLLBACK
--   drop function if exists public.set_place_test_flag(uuid, boolean);
--   alter table public.places drop column if exists is_test;
--   -- discover_communities and resolve_place_handle: restore from 20261231120001/3.
-- ============================================================================
