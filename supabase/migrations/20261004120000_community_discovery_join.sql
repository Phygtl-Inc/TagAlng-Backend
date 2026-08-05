-- ============================================================================
-- COMMUNITY DISCOVERY + JOIN  (see docs/LANA_COMMUNITIES_LOOK_FRONTEND.md)
--
-- WHAT CHANGES, AND WHY
--
--   Until now a community could only enter a user's profile three ways, all of
--   them starting from something the user themselves said or tapped:
--     chat_extraction  -- they mentioned a place, Lana asked which spot it is
--     profile_add      -- they added it in the Communities panel
--     invite_confirmed -- they redeemed an invite and self-confirmed their own
--   There was no way to SEE a community that already exists nearby and join it.
--   This migration adds the read (discover_communities_near) and the provenance
--   the product asked for: which of those paths actually produced each row.
--
--   PROVENANCE IS NOW TWO FACTS, deliberately:
--     source        -- how the community first ENTERED the system (unchanged
--                      semantics; gains 'community_join')
--     confirmed_via -- the action that turned it into a REAL community
--                      (confirmed + grounded)
--   They differ exactly in the interesting case: a place the user mentioned in
--   chat (source='chat_extraction') that they later confirmed by tapping Join on
--   the discovery panel (confirmed_via='community_join'). One column could not
--   answer both "where did this come from" and "what closed it".
--
-- WHAT THIS DOES NOT DO
--   No member identities are exposed by discovery. The RPC returns a place, a
--   count and a distance -- who is there stays members-only (§F, enforced in
--   app/community_surface.py). A place whose only members have blocked the
--   caller (or whom the caller blocked) is not returned at all: the count is
--   computed over visible members and zero-count places are dropped.
--
-- DEPLOY ORDER: this migration, then the worker (the worker calls the RPC and
-- writes confirmed_via).
--
-- See the ROLLBACK section at the foot of this file.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. source gains 'community_join'
-- ----------------------------------------------------------------------------
alter table public.circle_affiliations
  drop constraint if exists circle_affiliations_source_check;

alter table public.circle_affiliations
  add constraint circle_affiliations_source_check
  check (source in ('chat_extraction', 'invite_confirmed', 'profile_add', 'community_join'));

comment on column public.circle_affiliations.source is
  'How this community first entered the system: chat_extraction (mentioned in '
  'conversation), profile_add (added in the Communities panel), invite_confirmed '
  '(redeemed an invite and self-confirmed), community_join (found an existing '
  'nearby community in Lana and joined it). Never overwritten — see confirmed_via '
  'for the action that made it real.';


-- ----------------------------------------------------------------------------
-- 2. confirmed_via — the action that made it a real community
--
-- Null for a row that is still a candidate (suggested / ungrounded). Stamped
-- once, when the row becomes confirmed + grounded.
-- ----------------------------------------------------------------------------
alter table public.circle_affiliations
  add column if not exists confirmed_via text;

alter table public.circle_affiliations
  drop constraint if exists circle_affiliations_confirmed_via_check;

alter table public.circle_affiliations
  add constraint circle_affiliations_confirmed_via_check
  check (confirmed_via is null or confirmed_via in
    ('grounding_ask', 'profile_add', 'invite_self_confirm', 'community_join'));

comment on column public.circle_affiliations.confirmed_via is
  'The action that turned this candidate into a real community: grounding_ask '
  '(answered Lana''s "which spot is it?"), profile_add, invite_self_confirm, '
  'community_join (tapped Join on a community they found in Lana). Null while the '
  'row is still an ungrounded candidate. Distinct from source, which records where '
  'the community first came from.';

-- Backfill: historical rows predate the column, so derive the best honest value
-- from source rather than leaving them null (null would read as "never confirmed",
-- which is false for these rows). A chat-captured row that is confirmed today can
-- only have been closed by the grounding ask — that was the sole path.
update public.circle_affiliations
   set confirmed_via = case source
         when 'profile_add'      then 'profile_add'
         when 'invite_confirmed' then 'invite_self_confirm'
         else 'grounding_ask'
       end
 where confirmed_via is null
   and status = 'confirmed'
   and place_ref is not null;

create index if not exists circle_affiliations_confirmed_via_idx
  on public.circle_affiliations (confirmed_via)
  where confirmed_via is not null;


-- ----------------------------------------------------------------------------
-- 3. discover_communities_near(...) — communities you could join
--
-- Candidate set: places with at least one confirmed, non-dismissed, VISIBLE
-- member within p_radius_meters of the caller's coarse point (block centroid,
-- else ZIP centroid — user_origin_point, 20260921120000).
--
-- Falls back to ZIP equality when the caller has no resolvable point, so the 23
-- prod users with no home_block_id still get a list instead of an empty one.
--
-- Precision is block-level by design (see 20260921120000's header): the distance
-- is between two coarse points, never a street-level measure.
--
-- is_member is returned rather than filtering the caller's own places out — the
-- panel wants to show "you're in this one" instead of silently hiding it.
-- ----------------------------------------------------------------------------
create or replace function public.discover_communities_near(
  p_user_id        uuid,
  p_radius_meters  double precision default 8000,
  p_limit          int default 20,
  p_locale         text default 'en',
  p_query          text default null
) returns table (
  place_id         uuid,
  name             text,
  address          text,
  place_type       text,
  zip              text,
  member_count     int,
  member_types     text[],
  distance_meters  double precision,
  distance_text    text,
  is_member        boolean
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

  select o.origin, o.zip5 into v_origin, v_zip5
  from public.user_origin_point(p_user_id) o;

  return query
  with visible_members as (
    -- Confirmed members the caller is allowed to be counted alongside. The block
    -- filter is applied to the COUNT, not just to the display: a place kept alive
    -- only by someone the caller blocked must not surface at all.
    select a.place_ref, a.user_id, a.circle_type
    from public.circle_affiliations a
    where a.status = 'confirmed'
      and a.dismissed_at is null
      and a.place_ref is not null
      and (a.user_id = p_user_id or not public.lana_is_blocked(p_user_id, a.user_id))
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
  )
  select
    l.pid,
    l.name,
    l.address,
    l.place_type,
    l.zip,
    l.members,
    l.types,
    l.meters,
    public.humanize_distance_text(l.meters, p_locale),
    l.mine
  from located l
  where
    case
      -- With a point: everything inside the radius (a place with no coordinates
      -- can still qualify on ZIP equality, which is how imported rows behave).
      when v_origin is not null then
        l.meters is not null and l.meters <= p_radius_meters
        or (l.meters is null and v_zip5 is not null and l.zip = v_zip5)
      -- No point at all: ZIP equality is the only honest scope left.
      else v_zip5 is not null and l.zip = v_zip5
    end
  order by l.members desc, coalesce(l.meters, 1e9) asc, l.name asc
  limit greatest(coalesce(p_limit, 20), 1);
end;
$function$;

comment on function public.discover_communities_near(uuid, double precision, int, text, text) is
  'Communities near the caller that already have at least one visible confirmed '
  'member: place, member count, coarse distance, and is_member. Returns NO member '
  'identities (§F) — who is there is members-only, served by the worker. Places '
  'whose visible member count is zero (blocks) are omitted entirely.';

grant execute on function public.discover_communities_near(uuid, double precision, int, text, text)
  to service_role;


-- ============================================================================
-- ROLLBACK
--   drop function if exists public.discover_communities_near(uuid, double precision, int, text, text);
--   drop index if exists public.circle_affiliations_confirmed_via_idx;
--   alter table public.circle_affiliations drop constraint if exists circle_affiliations_confirmed_via_check;
--   alter table public.circle_affiliations drop column if exists confirmed_via;
--   -- source: only safe to narrow again once no 'community_join' rows remain.
--   -- delete from public.circle_affiliations where source = 'community_join';
--   alter table public.circle_affiliations drop constraint if exists circle_affiliations_source_check;
--   alter table public.circle_affiliations add constraint circle_affiliations_source_check
--     check (source in ('chat_extraction','invite_confirmed','profile_add'));
-- ============================================================================
