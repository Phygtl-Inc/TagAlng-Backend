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
--   They sit in discovery next to Etiqueta do Reino, which is our actual pilot creator.
--   They also inflate every count we might show someone.
--
--   The obvious answer is "don't test on prod", and the obvious answer is wrong here:
--   the team needs to exercise real flows against real data, and a separate environment
--   is a bigger lift than the problem deserves right now.
--
--   So: one boolean. A test place still exists, still resolves from a direct link, and
--   is visible to anyone who holds that link. It simply does not appear in anything that
--   lists or counts places for the public.
--
-- REVISION NOTE (please read before reviewing)
--
--   The first version of this file re-emitted discover_communities AND
--   resolve_place_handle using the bodies from PR #170. Production runs the #172
--   versions, which are different and correct, so applying that would have silently
--   reverted four of Asjid's fixes — the unassigned-record error on every non-creator
--   handle, the creator block source, the threshold ordering that made a one-member
--   community return zero rows, and the p_user_id default.
--
--   This version instead takes #172's discover_communities VERBATIM and adds exactly one
--   predicate. resolve_place_handle is not touched at all — see the note at the bottom.

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
-- Matched by exact handle, not a pattern. A real community called "Testa", or a creator
-- whose handle happens to contain "test", would be caught by a regex. Cheap to flag one
-- more later; expensive to hide a real creator.

update public.places
   set is_test = true, updated_at = now()
 where handle in (
   'test-7', 'test7-community', 'asjidtest5-community', 'asjid-test-6', 'mrbeast-community'
 );

-- ── toggling ────────────────────────────────────────────────────────────────
--
-- service_role only, deliberately. Marking a live community as test hides it from
-- discovery, which is a quiet way to break somebody's launch — that should not be one
-- mis-click away for an operator. Open to being argued down to operator-with-confirmation.

create or replace function public.set_place_test_flag(p_place_id uuid, p_is_test boolean)
returns boolean
language plpgsql
volatile
security definer
set search_path = pg_catalog, public
as $$
begin
  if not exists (select 1 from public.places where id = p_place_id) then
    raise exception 'place_not_found';
  end if;

  update public.places
     set is_test = coalesce(p_is_test, false), updated_at = now()
   where id = p_place_id;

  return coalesce(p_is_test, false);
end;
$$;

revoke all on function public.set_place_test_flag(uuid, boolean)
  from public, anon, authenticated;
grant execute on function public.set_place_test_flag(uuid, boolean) to service_role;

-- ── keep test places out of discovery ───────────────────────────────────────
--
-- #172's body, unchanged except for ONE added predicate in the final WHERE:
--
--     and not p.is_test
--
-- Everything else — the pre-merge thresholding on both arms, the `me` CTE resolving
-- auth.uid() over p_user_id, the defaults, the grants — is Asjid's and is reproduced
-- verbatim. If anything here has drifted from 20261231120003, that is a mistake on my
-- part and his version wins.

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
    -- THE ONLY ADDED LINE. Test data does not belong next to the pilot creator.
    and not p.is_test
  order by (m.kind = 'member_claim') desc, m.sim desc, coalesce(c.members,0) desc, p.name
  limit greatest(1, least(coalesce(p_limit, 10), 50));
$$;

revoke all on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  from public, anon, authenticated;
grant execute on function public.discover_communities(uuid, extensions.vector, real, int, boolean, boolean)
  to authenticated, service_role;

-- ============================================================================
-- NOT DONE HERE · resolve_place_handle
--
--   The client needs to know a place is test data so the page can say so — a test place
--   indistinguishable from a real one is how test data ends up in a screenshot in a
--   pitch deck.
--
--   That means one line in resolve_place_handle's jsonb_build_object:
--
--       'isTest', coalesce(v_is_test, false),
--
--   @asjid9 — deliberately leaving this to you rather than re-emitting a function whose
--   current body you wrote and I have already got wrong once today. tagalng-pwa #99
--   reads it as `r.isTest === true`, so it degrades to false if the field is absent and
--   nothing breaks while this is outstanding.
--
-- ROLLBACK
--   drop function if exists public.set_place_test_flag(uuid, boolean);
--   alter table public.places drop column if exists is_test;
--   -- discover_communities: restore verbatim from 20261231120003.
-- ============================================================================
